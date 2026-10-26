"""Two-phase, durable CSV review batches layered on existing pipelines."""
import csv
import io
import json
import uuid
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from ..company_policy import CompanyPolicy, company_key
from .storage import Store, PROTECTED, atomic_write, digest, now, save_json
from .integration import jobs, canonical_url, resume_for, ReadinessError
from .destinations import scope
from .browser import platform
from .sessions import Session, scrape_session, generate_one

FIELDS = 'batch_id job_id company position location jobright_url application_url application_domain ats_platform resume_path resume_status company_policy application_status review_decision review_notes reviewed_at submitted_at confirmation_id failure_reason'.split()
EDITABLE = {'review_decision', 'review_notes'}
DECISIONS = {'PENDING', 'APPROVED', 'REJECTED', 'NEEDS_CORRECTION'}


def safe(value):
    value = str(value or '')
    return "'" + value if value.lstrip().startswith(('=', '+', '-', '@')) or value.startswith(('\t','\r','\n')) else value


def uid():
    return now()[:19].replace(':','').replace('-','') + '-' + uuid.uuid4().hex[:10]


class Batches:
    def __init__(self, root):
        self.root = Path(root)
        self.store = Store(self.root / 'data/applications')
        self.db = self.store.db
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS review_batches (id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS review_batch_jobs (batch_id TEXT, job_id TEXT, payload TEXT NOT NULL, PRIMARY KEY(batch_id,job_id));
            CREATE TABLE IF NOT EXISTS destination_approvals (batch_id TEXT, job_id TEXT, hash TEXT, payload TEXT NOT NULL, PRIMARY KEY(batch_id,job_id));
        ''')

    def close(self):
        self.store.close()

    def folder(self, batch_id):
        if not batch_id or Path(batch_id).name != batch_id or batch_id in ('.','..'):
            raise ValueError('Invalid batch ID')
        return self.root / 'data/applications/review_batches' / batch_id

    def meta(self, batch_id):
        self.folder(batch_id)
        row = self.db.execute('SELECT payload FROM review_batches WHERE id=?', (batch_id,)).fetchone()
        if not row:
            raise ValueError('Unknown review batch')
        return json.loads(row[0])

    def set_meta(self, meta):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO review_batches VALUES (?,?)', (meta['batch_id'], json.dumps(meta)))

    def rows(self, batch_id):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM review_batch_jobs WHERE batch_id=? ORDER BY rowid', (batch_id,))]

    def put(self, row):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO review_batch_jobs VALUES (?,?,?)', (row['batch_id'], row['job_id'], json.dumps(row)))

    def invalidate(self, row, reason):
        row.update(review_decision='NEEDS_CORRECTION', failure_reason=reason, application_status='NEEDS_REVIEW')
        with self.db:
            self.db.execute('DELETE FROM destination_approvals WHERE batch_id=? AND job_id=?', (row['batch_id'], row['job_id']))
        self.put(row)

    def create(self, selected, batch_id=None, **extra):
        batch_id = batch_id or uid()
        meta = dict(batch_id=batch_id, created_at=now(), status='PREPARING', csv_hash='', **extra)
        with self.db:
            self.db.execute('INSERT INTO review_batches VALUES (?,?)', (batch_id, json.dumps(meta)))
            for job in selected:
                policy = CompanyPolicy.load(self.root).evaluate(job['company'], job['id'])
                row = dict.fromkeys(FIELDS, '')
                row.update(batch_id=batch_id, job_id=job['id'], company=job['company'], position=job['title'],
                    location=job.get('location',''), jobright_url=job.get('jobright_url',''), resume_status='WAITING_FOR_RESUME',
                    company_policy=policy['company_policy'], application_status='PREPARING', review_decision='PENDING',
                    _job=dict(job), _identity_review=job.get('identity_review',False))
                self.db.execute('INSERT INTO review_batch_jobs VALUES (?,?,?)', (batch_id, job['id'], json.dumps(row)))
        self.export(batch_id)
        return batch_id

    def capture(self, batch_id):
        path = self.folder(batch_id) / 'application_review.csv'
        raw = path.read_bytes()
        archive = self.folder(batch_id) / 'imports' / (uid() + '.csv')
        atomic_write(archive, raw.decode('utf-8-sig'))
        return raw

    def import_csv(self, batch_id):
        """Archive first, compare every generated cell, then commit repeatable decisions."""
        self.meta(batch_id)
        raw = self.capture(batch_id)
        reader = csv.DictReader(io.StringIO(raw.decode('utf-8-sig'), newline=''))
        if reader.fieldnames != FIELDS:
            raise ValueError('Review CSV columns changed')
        incoming = list(reader)
        expected = {r['job_id']: r for r in self.rows(batch_id)}
        identifiers = [r.get('job_id') for r in incoming]
        if len(identifiers) != len(set(identifiers)) or set(identifiers) != set(expected):
            # No partial authorization from a malformed membership set.
            for row in expected.values():
                self.invalidate(row, 'CSV_MEMBERSHIP_CHANGED')
            raise ValueError('Review CSV job membership changed; original rows retained in database')
        with self.db:
            for item in incoming:
                row = expected[item['job_id']]
                # Compare against the last exported view, not newer DB state. Resume updates
                # may intentionally be newer while a user is editing an old view.
                emitted = row.get('_exported', {k:safe(row.get(k,'')) for k in FIELDS})
                changed = [k for k in FIELDS if k not in EDITABLE and item.get(k) != emitted.get(k,'')]
                decision = item.get('review_decision','').strip().upper()
                material = ('batch_id','job_id','company','position','application_url')
                if decision == 'APPROVED' and any(emitted.get(k,'') != safe(row.get(k,'')) for k in material):
                    self.invalidate(row, 'DESTINATION_CHANGED_SINCE_EXPORTED_REVIEW')
                    continue
                if changed or decision not in DECISIONS:
                    self.invalidate(row, 'IMMUTABLE_CSV_FIELDS_CHANGED: ' + ','.join(changed) if changed else 'INVALID_REVIEW_DECISION')
                    continue
                row.update(review_decision=decision, review_notes=item.get('review_notes',''), reviewed_at=now() if decision != 'PENDING' else '')
                if decision == 'APPROVED':
                    try:
                        destination = scope(row['application_url'])
                        normalized = CompanyPolicy.load(self.root).evaluate(row['company'], row['job_id'])['canonical_company']
                        snapshot = dict(batch_id=batch_id, job_id=row['job_id'], company=company_key(normalized),
                            position=row['position'], application_url=canonical_url(row['application_url']), destination=destination)
                        sha = digest(json.dumps(snapshot,sort_keys=True).encode())
                        self.db.execute('INSERT OR REPLACE INTO destination_approvals VALUES (?,?,?,?)', (batch_id,row['job_id'],sha,json.dumps(snapshot)))
                    except ValueError:
                        self.invalidate(row, 'URL_REVIEW_REQUIRED')
                        continue
                else:
                    self.db.execute('DELETE FROM destination_approvals WHERE batch_id=? AND job_id=?', (batch_id,row['job_id']))
                self.put(row)
        return digest(raw)

    def export(self, batch_id, expected_hash=None):
        meta = self.meta(batch_id)
        path = self.folder(batch_id) / 'application_review.csv'
        # Never overwrite an externally edited CSV during background resume generation.
        # Keep it intact until explicit import; current statuses remain in SQLite.
        if path.exists():
            actual = digest(path.read_bytes())
            if actual != (expected_hash or meta['csv_hash']):
                return False
            self.capture(batch_id)
        out = io.StringIO(newline='')
        writer = csv.DictWriter(out, FIELDS)
        writer.writeheader()
        rows = self.rows(batch_id)
        for row in rows:
            view = {k:safe(row.get(k,'')) for k in FIELDS}
            writer.writerow(view)
            row['_exported'] = view
        # Recheck after rendering to detect edits during rendering.
        if path.exists() and digest(path.read_bytes()) != actual:
            return False
        atomic_write(path, out.getvalue())
        for row in rows:
            self.put(row)
        meta['csv_hash'] = digest(path.read_bytes())
        self.set_meta(meta)
        return True

    def sync_history(self, batch_id, processed=None):
        for row in self.rows(batch_id):
            history = self.store.get(row['job_id'])
            if history and (history['status'] in PROTECTED or processed is None or row['job_id'] in processed):
                row.update(application_status=history['status'],submitted_at=history.get('submitted_at',''),confirmation_id=history.get('confirmation_id',''),failure_reason=history.get('review_reason') or history.get('error_message',''))
                self.put(row)

    def approval(self, row, job):
        record = self.db.execute('SELECT hash,payload FROM destination_approvals WHERE batch_id=? AND job_id=?', (row['batch_id'],row['job_id'])).fetchone()
        if not record or row['review_decision'] != 'APPROVED':
            raise ValueError('Destination is not approved')
        snapshot = json.loads(record[1])
        expected = dict(batch_id=row['batch_id'],job_id=job['id'],
            company=company_key(CompanyPolicy.load(self.root).evaluate(job['company'],job['id'])['canonical_company']),
            position=job['title'],application_url=canonical_url(job.get('source_url','')),destination=scope(job.get('source_url','')))
        if snapshot != expected or digest(json.dumps(snapshot,sort_keys=True).encode()) != record[0]:
            raise ValueError('Approved destination or employer identity changed')
        return dict(snapshot, approval_hash=record[0])


def resolve_destination(job, headed=False):
    """Read-only destination discovery, no applicant profile, credentials or form actions."""
    from playwright.sync_api import sync_playwright
    from ..content import original_posting_url
    candidate = job.get('source_url','')
    if candidate:
        try:
            scope(candidate)
        except ValueError:
            candidate = ''
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headed)
        context = browser.new_context(service_workers='block')
        # Destination discovery may issue only GET/HEAD requests.
        context.route('**/*', lambda r: r.continue_() if r.request.method in ('GET','HEAD') else r.abort())
        try:
            page = context.new_page()
            def visit(url):
                response=page.goto(url,wait_until='domcontentloaded',timeout=30000)
                if response and response.status >= 400:
                    raise ValueError('Destination returned an HTTP error')
            visit(candidate or job['jobright_url'])
            if platform(page.url) == 'jobright':
                candidate = original_posting_url(page.content())
                if not candidate:
                    return '', dict(reason='URL_REVIEW_REQUIRED')
                visit(candidate)
            # Only unambiguous supported external Apply links; never click controls.
            links = page.get_by_role('link',name=__import__('re').compile(r'^apply(?: now| for (?:this )?(?:job|position))?$', __import__('re').I))
            targets = set()
            from urllib.parse import urljoin
            for href in links.evaluate_all('(nodes)=>nodes.map(n=>n.href)'):
                try:
                    targets.add(scope(urljoin(page.url,href))['url'])
                except ValueError:
                    pass
            if len(targets) == 1 and canonical_url(page.url) not in targets:
                visit(next(iter(targets)))
            if page.locator('body').count() == 0:
                raise ValueError('Destination has no document')
            final = scope(page.url)['url']
            return final, dict(resolved_from=job.get('source_url',''),redirect_destination=final, resolved_at=now())
        finally:
            context.close()
            browser.close()


def session_report(batches, batch_id, phase, rows, **extra):
    stamp=uid()
    approved=[r for r in rows if r['review_decision']=='APPROVED']
    pending=[r for r in rows if r['review_decision']=='PENDING']
    rejected=[r for r in rows if r['review_decision']=='REJECTED']
    referrals=[r for r in rows if r['company_policy']=='REFERRAL_REQUIRED']
    def view(r):
        h=batches.store.get(r['job_id']) or {}
        return dict({k:r.get(k,'') for k in FIELDS},current_step=h.get('current_step','Not attempted this session'),
            last_observed_url=h.get('last_observed_url',''),confirmation_evidence=h.get('confirmation_evidence',''))
    counted=approved if phase=='APPLICATION' else rows
    summary=dict(session_id=stamp,batch_id=batch_id,phase=phase,created_at=now(),
        counts=dict(Counter(r.get('application_status','') for r in counted)),approved=len(approved),pending=len(pending),rejected=len(rejected),
        authorized_application_candidates=sum(r['company_policy']=='AUTO_APPLY' for r in approved),referral_required=len(referrals),
        csv_location=str(batches.folder(batch_id)/'application_review.csv'),jobs=[view(r) for r in rows],
        approved_application_results=[view(r) for r in approved],pending_approval=[view(r) for r in pending],rejected_jobs=[view(r) for r in rejected],
        preparation_issues=[view(r) for r in rows if r['resume_status']!='READY_FOR_APPLICATION'],**extra)
    folder=batches.folder(batch_id)/'sessions'
    save_json(folder/(stamp+'.json'),summary)
    lines=[f'# {phase} session','',*[f'{k}: {v}' for k,v in summary.items() if k not in ('jobs','approved_application_results','pending_approval','rejected_jobs','preparation_issues')]]
    groups=[('Approved Application Results',approved),('Pending Approval',pending),('Rejected',rejected),('Referral Required',referrals),
            ('Preparation Issues',[r for r in rows if r['resume_status']!='READY_FOR_APPLICATION'])] if phase=='APPLICATION' else [('Prepared jobs',rows)]
    for title,values in groups:
        lines+=['','## '+title,'','| Company | Position | Application URL | Status | Current step | Reason / confirmation |','|---|---|---|---|---|---|']
        for r in values:
            v=view(r)
            status=r['resume_status'] if title=='Preparation Issues' else 'PENDING approval' if title=='Pending Approval' else r['application_status']
            reason=(v['confirmation_evidence'] or r['failure_reason']) if title!='Pending Approval' else 'Not authorized; preparation status: '+r['resume_status']
            cells=[r['company'],r['position'],r['application_url'],status,v['current_step'] if title=='Approved Application Results' else 'Not attempted',reason]
            lines.append('| '+' | '.join(str(x).replace('|','\\|').replace('\n',' ') for x in cells)+' |')
    text='\n'.join(lines)+'\n'
    atomic_write(folder/(stamp+'.md'),text)
    save_json(batches.root/'data/applications/last_session.json',summary)
    atomic_write(batches.root/'data/applications/last_session.md',text)
    print(f'[{phase}] Batch {batch_id}; approved={len(approved)}; pending={len(pending)}; rejected={len(rejected)}; authorized candidates={summary["authorized_application_candidates"]}',flush=True)
    print('Review CSV: '+summary['csv_location'],flush=True)
    return summary


def prepare(root, agent, profile, config, limit=5, headed=False, run_id=None,
            scraper=scrape_session, generator=generate_one, resolver=resolve_destination):
    batches=Batches(root)
    session=Session(root,config,limit,run_id)
    batch_id=session.state['run_id']
    failure=''
    generated=0
    try:
        if session.state['stage'] in ('scraping','fetching'):
            scraper(session,headed)
            session.state['stage']='resumes'
            session.checkpoint()
        current={j['id']:j for j in jobs(root)}
        exists=batches.db.execute('SELECT 1 FROM review_batches WHERE id=?',(batch_id,)).fetchone()
        if not exists:
            batches.create([dict(current.get(j['id'],j), identity_review=j.get('identity_review',False)) for j in session.state['selected']], batch_id, run_id=batch_id)
        print('Review CSV: '+str(batches.folder(batch_id)/'application_review.csv'),flush=True)
        # Publish URLs for all rows before expensive generation begins.
        for row in batches.rows(batch_id):
            if row.get('_resolution_attempted'):
                continue
            job=current.get(row['job_id'],row['_job'])
            try:
                url,evidence=resolver(job,headed)
                dest=scope(url)
                row.update(application_url=dest['url'],application_domain=urlsplit(url).hostname,ats_platform=platform(url),_resolution=evidence)
                from .sessions import keys
                aliases = keys(dict(job,source_url=dest['url']))
                duplicate = next((entry for identifier,entry in session.ledger.records.items()
                    if identifier != job['id'] and aliases.intersection(entry['keys'])), None)
                if duplicate:
                    session.state['skipped'].append(dict(job,reason='resolved_destination_duplicate',matched_job_id=duplicate['job']['id']))
                    session.state['previous_run_duplicates'] += 1
                    session.state['selected'] = [j for j in session.state['selected'] if j['id'] != job['id']]
                    session.checkpoint()
                    with batches.db:
                        batches.db.execute('DELETE FROM review_batch_jobs WHERE batch_id=? AND job_id=?',(batch_id,job['id']))
                    batches.export(batch_id)
                    continue
                reservation = session.ledger.records[job['id']]
                reservation['keys'] = sorted(set(reservation['keys']) | aliases)
                reservation['job']['application_url'] = dest['url']
                session.ledger.save()
            except Exception as error:
                row.update(application_status='URL_REVIEW_REQUIRED',failure_reason='URL_REVIEW_REQUIRED: '+type(error).__name__)
            row['_resolution_attempted']=True
            batches.put(row)
            batches.export(batch_id)
        for row in batches.rows(batch_id):
            job=current.get(row['job_id'],row['_job'])
            policy=CompanyPolicy.load(root).evaluate(job['company'],job['id'])['company_policy']
            row['company_policy']=policy
            if policy=='EXCLUDED':
                row.update(application_status='SKIPPED',failure_reason='USER_EXCLUDED_COMPANY')
            else:
                try:
                    try:
                        artifact=resume_for(root,agent,job,approval_mode='automatic')
                    except ReadinessError as error:
                        if error.status!='WAITING_FOR_RESUME':
                            raise
                        generator(root,agent,job)
                        generated+=1
                        artifact=resume_for(root,agent,job,approval_mode='automatic')
                    row.update(resume_path=artifact['path'],resume_status='READY_FOR_APPLICATION',_resume_sha=artifact['sha256'])
                    row['application_status']='REFERRAL_REQUIRED' if policy=='REFERRAL_REQUIRED' else 'READY_FOR_REVIEW' if row['application_url'] else 'URL_REVIEW_REQUIRED'
                    if policy=='REFERRAL_REQUIRED':
                        prior=batches.store.get(job['id'])
                        if not prior or prior['status'] not in PROTECTED:
                            batches.store.update(job['id'],company=job['company'],position=job['title'],job_url=job.get('jobright_url',''),application_url=row['application_url'],resume_path=artifact['path'],company_policy=policy,status='NEEDS_REVIEW',referral_status='NOT_REQUESTED',review_reason='REFERRAL_PENDING')
                except Exception as error:
                    row.update(resume_status='RESUME_FAILED',application_status='RESUME_FAILED',failure_reason=type(error).__name__)
            batches.put(row)
            batches.export(batch_id)
        meta=batches.meta(batch_id)
        meta['status']='READY_FOR_REVIEW'
        batches.set_meta(meta)
        session.state.update(run_status='COMPLETED',stage='review',end_time=now())
        session.checkpoint()
        return batch_id
    except BaseException as error:
        failure=type(error).__name__
        session.state.update(run_status='INTERRUPTED',end_time=now())
        session.checkpoint()
        raise
    finally:
        if not batches.db.execute('SELECT 1 FROM review_batches WHERE id=?',(batch_id,)).fetchone():
            batches.create(session.state['selected'],batch_id,run_id=batch_id)
        if batches.db.execute('SELECT 1 FROM review_batches WHERE id=?',(batch_id,)).fetchone():
            rows=batches.rows(batch_id)
            s=session.state
            session_report(batches,batch_id,'PREPARATION',rows,failure_reason=failure,fresh_jobs_found=s['new_jobs_found'],duplicates_skipped=s['historical_duplicates']+s['previous_run_duplicates']+s['current_run_duplicates'],excluded_jobs=s['excluded_jobs'],resumes_generated=generated,resumes_ready=sum(r['resume_status']=='READY_FOR_APPLICATION' for r in rows),application_links_resolved=sum(bool(r['application_url']) for r in rows),links_requiring_review=sum(not r['application_url'] for r in rows))
        batches.close()


def apply_approved(root, agent, profile, config, batch_id, headed=False, processor=None, context=None):
    from .runner import process
    processor=processor or process
    batches=Batches(root)
    imported=None
    outcomes=[]
    failure=''
    submitted=0
    try:
        imported=batches.import_csv(batch_id)
        if not any(r['review_decision']=='APPROVED' for r in batches.rows(batch_id)):
            print('No jobs are approved; zero applications submitted.')
        current={j['id']:j for j in jobs(root)}
        processed=0
        for row in batches.rows(batch_id):
            if row['review_decision']!='APPROVED':
                continue
            prior=batches.store.get(row['job_id'])
            if prior and prior['status'] in PROTECTED:
                continue
            job=current.get(row['job_id'])
            try:
                if not job:
                    raise ValueError('Stored job missing')
                policy=CompanyPolicy.load(root).evaluate(job['company'],job['id'])['company_policy']
                row['company_policy']=policy
                if policy!='AUTO_APPLY':
                    row.update(application_status='REFERRAL_REQUIRED' if policy=='REFERRAL_REQUIRED' else 'SKIPPED',failure_reason='Company policy blocks automatic application')
                    batches.put(row)
                    continue
                if row.get('_identity_review'):
                    raise ValueError('Duplicate identity requires review')
                # A resolved URL is bound to the original stored source. No CSV edit
                # can silently rewrite scraper metadata or authorize a new URL.
                original=row['_job']
                if any(job.get(k,'')!=original.get(k,'') for k in ('company','title','source_url','jobright_url')):
                    raise ValueError('Stored job identity or source changed')
                approved_job=dict(job,source_url=row['application_url'])
                approval=batches.approval(row,approved_job)
                artifact=resume_for(root,agent,job,approval_mode='automatic')
                if artifact['path']!=row['resume_path'] or artifact['sha256']!=row.get('_resume_sha'):
                    raise ValueError('Resume association or artifact changed')
                if processed>=config['MAX_APPLICATIONS_PER_RUN']:
                    continue
                per_job=dict(config,RESUME_APPROVAL_MODE='automatic',REVIEWED_DESTINATION=approval)
                result=processor(root,agent,profile,per_job,mode='dry-run' if config['DRY_RUN'] else 'apply',job_ids={job['id']},limit=1,headed=headed,resume_review=True,context=context)
                submitted+=result.get('verified_submissions_this_run',0)
                processed+=1
                if result.get('stopped'):
                    outcomes.append(row['job_id'])
                    break
            except (ValueError,ReadinessError,OSError,KeyError) as error:
                batches.invalidate(row,str(error))
            except Exception as error:
                row.update(application_status='FAILED',failure_reason=type(error).__name__)
                batches.put(row)
            else:
                outcomes.append(row['job_id'])
        batches.sync_history(batch_id, processed=set(outcomes))
    except BaseException as error:
        failure=type(error).__name__
        raise
    finally:
        if imported:
            batches.export(batch_id,expected_hash=imported)
        rows=batches.rows(batch_id)
        history=[batches.store.get(r['job_id']) for r in rows]
        session_report(batches,batch_id,'APPLICATION',rows,failure_reason=failure,verified_submissions=submitted,
            already_applied=max(0,sum(bool(h) and h['status'] in ('APPLIED','ALREADY_APPLIED','HISTORICAL_MANUALLY_APPLIED') for h in history)-submitted),
            authentication_blockers=sum(bool(h) and h['job_id'] in outcomes and bool(h.get('authentication_error')) for h in history),
            existing_authentication_blockers=sum(bool(h) and h['job_id'] not in outcomes and bool(h.get('authentication_error')) for h in history),
            unsupported_forms=sum(r['application_status']=='NOT_SUPPORTED' and r['review_decision']=='APPROVED' for r in rows),submission_unconfirmed=sum(r['application_status']=='SUBMISSION_UNCONFIRMED' and r['review_decision']=='APPROVED' for r in rows),submission_failures=sum(r['application_status']=='FAILED' and r['review_decision']=='APPROVED' for r in rows),attempted_job_ids=outcomes)
        batches.close()


def migrate_existing(root, agent, profile, config, job_id):
    """Explicit intake of an existing job, without changing historical protections."""
    job=next((j for j in jobs(root) if j['id']==job_id),None)
    if job is None:
        raise ValueError('Unknown stored job')
    batches=Batches(root)
    try:
        # Idempotent migration: revisiting an existing batch never creates new approval.
        existing=batches.db.execute('SELECT batch_id FROM review_batch_jobs WHERE job_id=? ORDER BY rowid LIMIT 1',(job_id,)).fetchone()
        if existing:
            return existing[0]
        artifact=resume_for(root,agent,job,approval_mode='automatic')
        batch_id=batches.create([job],migration=True)
        row=batches.rows(batch_id)[0]
        try:
            dest=scope(job.get('source_url',''))
            row.update(application_url=dest['url'],application_domain=urlsplit(dest['url']).hostname,ats_platform=platform(dest['url']))
        except ValueError:
            row.update(application_status='URL_REVIEW_REQUIRED',failure_reason='URL_REVIEW_REQUIRED')
        row.update(resume_path=artifact['path'],resume_status='READY_FOR_APPLICATION',_resume_sha=artifact['sha256'],_resolution_attempted=True)
        row['application_status']='READY_FOR_REVIEW' if row['application_url'] else 'URL_REVIEW_REQUIRED'
        batches.put(row)
        meta=batches.meta(batch_id)
        meta['status']='READY_FOR_REVIEW'
        batches.set_meta(meta)
        batches.sync_history(batch_id)
        batches.export(batch_id)
        session_report(batches,batch_id,'PREPARATION',batches.rows(batch_id),migration=True,fresh_jobs_found=0,duplicates_skipped=0,excluded_jobs=0,resumes_generated=0,resumes_ready=1,application_links_resolved=int(bool(row['application_url'])),links_requiring_review=int(not row['application_url']))
        return batch_id
    finally:
        batches.close()


def correct_destination(root, batch_id, job_id, url, headed=False, resolver=resolve_destination):
    """Explicit correction validates the new destination and publishes a PENDING snapshot."""
    batches=Batches(root)
    try:
        imported=batches.import_csv(batch_id)
        row=next(r for r in batches.rows(batch_id) if r['job_id']==job_id)
        prior=batches.store.get(job_id)
        if prior and prior['status'] in PROTECTED:
            raise ValueError('Protected application cannot be redirected')
        resolved,evidence=resolver(dict(row['_job'],source_url=scope(url)['url']),headed)
        dest=scope(resolved)
        batches.invalidate(row,'Destination corrected; review the new snapshot')
        row.update(application_url=dest['url'],application_domain=urlsplit(dest['url']).hostname,ats_platform=platform(dest['url']),
                   review_decision='PENDING',reviewed_at='',application_status='READY_FOR_REVIEW',_resolution=evidence)
        batches.put(row)
        batches.export(batch_id,expected_hash=imported)
    finally:
        batches.close()

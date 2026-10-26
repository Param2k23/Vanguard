"""Durable discovery ledger and explicitly scoped full-pipeline sessions (UTC)."""
import csv
import io
import json
import subprocess
import sys
import time
import uuid
from collections import Counter
from pathlib import Path

from ..company_policy import CompanyPolicy
from .integration import jobs, identity, canonical_url, normalize, resume_for, ReadinessError
from .storage import Store, now, save_json, atomic_write, PROTECTED

DEFAULTS = dict(DEFAULT_NEW_JOB_LIMIT=10, MAX_SCRAPE_LISTINGS=100,
                MAX_SCRAPE_PAGES=25, MAX_SCRAPE_DURATION_SECONDS=900,
                MAX_DUPLICATE_ONLY_PAGES=3)


def keys(job):
    result = {'id:' + job['id']} if job.get('id') else set()
    for field in ('source_url', 'jobright_url', 'application_url', 'job_url'):
        if job.get(field):
            try:
                result.add('url:' + identity(job[field]))
            except ValueError:
                pass
    return result


def external(job):
    return {k for k in keys(job) if k.startswith('url:') and 'jobright.ai' not in k and not k.startswith('url:jobright:')}


def baseline_jobs(root):
    """Include archived resume-only jobs even when no longer in jobs.csv."""
    rows = jobs(root)
    known = {j['id'] for j in rows}
    path = root / 'data/resumes/state.json'
    state = json.loads(path.read_text()) if path.exists() else {}
    for entry in state.values():
        identifier = entry.get('job_id')
        if not identifier or identifier in known:
            continue
        metadata_path = Path(entry.get('folder', '')) / 'job.json'
        metadata = json.loads(metadata_path.read_text()) if metadata_path.is_file() else {}
        url = metadata.get('job_url', '')
        rows.append(dict(id=identifier, company=metadata.get('company',''), title=metadata.get('role',''),
                         source_url='' if 'jobright.ai/' in url else url,
                         jobright_url=url if 'jobright.ai/' in url else ''))
        known.add(identifier)
    return rows


class Ledger:
    def __init__(self, root):
        self.root = root
        self.directory = root / 'data/applications'
        self.path = self.directory / 'discovery_history.json'
        self.baseline_path = self.directory / 'historical_baseline.json'
        # Immutable baseline is committed BEFORE any scraper or browser starts.
        if not self.baseline_path.exists():
            stamp = now()
            records = [dict(job=j, keys=sorted(keys(j)), disposition='HISTORICAL_MANUALLY_APPLIED',
                            baseline_created_at=stamp, reason='User reports prior manual application; no employer confirmation') for j in baseline_jobs(root)]
            save_json(self.baseline_path, dict(created_at=stamp, records=records))
        baseline = json.loads(self.baseline_path.read_text())
        self.records = json.loads(self.path.read_text()) if self.path.exists() else {}
        for entry in baseline['records']:
            self.records.setdefault(entry['job']['id'], entry)
        store = Store(self.directory)
        try:
            for row in store.rows():
                job = dict(id=row['job_id'], company=row['company'], title=row['position'],
                           source_url=row['application_url'], jobright_url=row['job_url'])
                self.records.setdefault(job['id'], dict(job=job, keys=sorted(keys(job)), disposition=row['status']))
            for entry in baseline['records']:
                j = entry['job']
                if store.get(j['id']) is None:
                    store.update(j['id'], status='HISTORICAL_MANUALLY_APPLIED', company=j['company'],
                                 position=j['title'], application_url=j.get('source_url',''), job_url=j.get('jobright_url',''),
                                 review_reason=entry['reason'])
        finally:
            store.close()
        for job in jobs(root):
            self.records.setdefault(job['id'], dict(job=job, keys=sorted(keys(job)), disposition='PREVIOUSLY_STORED'))
        for entry in self.records.values():
            entry['keys'] = sorted(set(entry['keys']) | keys(entry['job']))
        self.save()

    def save(self):
        save_json(self.path, self.records)

    def match(self, job):
        aliases = keys(job)
        uncertain = None
        for entry in self.records.values():
            old = entry['job']
            if aliases.intersection(entry['keys']):
                return entry, False
            if normalize(job.get('company','')) == normalize(old.get('company','')) and normalize(job.get('title','')) == normalize(old.get('title','')):
                if not external(job) or not external(old):
                    uncertain = entry
        return uncertain, bool(uncertain)


class Session:
    def __init__(self, root, config, limit=None, run_id=None):
        self.root = root
        self.config = dict(DEFAULTS, **config)
        self.ledger = Ledger(root)
        self.policy = CompanyPolicy.load(root)
        self.started = time.monotonic()
        self.last_new = False
        self.page_new = 0
        self.duplicate_pages = 0
        if run_id:
            if Path(run_id).name != run_id or not run_id:
                raise ValueError('Invalid run ID')
            self.folder = root / 'data/applications/runs' / run_id
            self.state = json.loads((self.folder / 'run_summary.json').read_text())
            if self.state['run_status'] == 'COMPLETED':
                raise ValueError('Completed runs cannot be resumed; start a new fresh run')
        else:
            run_id = now()[:19].replace('-','').replace(':','').replace('T','-') + '-' + uuid.uuid4().hex[:8]
            self.folder = root / 'data/applications/runs' / run_id
            self.state = dict(run_id=run_id, start_time=now(), end_time=None,
                              requested_job_limit=limit or self.config['DEFAULT_NEW_JOB_LIMIT'],
                              number_of_listings_scanned=0, new_jobs_found=0, historical_duplicates=0,
                              previous_run_duplicates=0, current_run_duplicates=0, excluded_jobs=0,
                              referral_jobs=0, selected=[], excluded=[], skipped=[], outcomes={},
                              resume_results={}, resume_generation_successes=0, resume_generation_failures=0,
                              application_attempts=0, verified_applications=0, needs_review=0,
                              failed_applications=0, submission_unconfirmed=0,
                              run_status='RUNNING', failure_reason='', stage='scraping', scrape_limit_reached=False,
                              report_path=str(self.folder / 'run_report.md'))
        # Recover a crash between committing the ledger and session queue.
        for entry in self.ledger.records.values():
            if entry.get('run_id') == run_id:
                group = 'excluded' if entry.get('policy') == 'EXCLUDED' else 'selected'
                if not any(j['id'] == entry['job']['id'] for j in self.state[group]):
                    self.state[group].append(entry['job'])
        self.state['excluded_jobs'] = len(self.state['excluded'])
        self.state['referral_jobs'] = sum(self.policy.evaluate(j['company'], j['id'])['company_policy'] == 'REFERRAL_REQUIRED' for j in self.state['selected'])
        # Restore any ledger reservation whose scraper CSV write was interrupted.
        from .. import pipeline as pipeline_module
        stored = jobs(root)
        existing = {j['id'] for j in stored}
        missing = [j for j in self.state['selected'] + self.state['excluded'] if j['id'] not in existing]
        if missing:
            fields = pipeline_module.FIELDS
            out = io.StringIO()
            writer = csv.DictWriter(out, fields, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(stored + missing)
            atomic_write(root / 'data/jobs.csv', out.getvalue())
        self.checkpoint()

    def checkpoint(self):
        self.state['new_jobs_found'] = len(self.state['selected'])
        save_json(self.folder / 'run_summary.json', self.state)
        rows = self.state['selected']
        out = io.StringIO()
        fields = ['id','company','title','source_url','jobright_url','run_id']
        writer = csv.DictWriter(out, fields, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(dict(j, run_id=self.state['run_id']) for j in rows)
        atomic_write(self.folder / 'selected_jobs.csv', out.getvalue())

    def bounded(self):
        reached = (self.state['number_of_listings_scanned'] >= self.config['MAX_SCRAPE_LISTINGS'] or
                   time.monotonic() - self.started >= self.config['MAX_SCRAPE_DURATION_SECONDS'] or
                   self.duplicate_pages >= self.config['MAX_DUPLICATE_ONLY_PAGES'])
        self.state['scrape_limit_reached'] |= reached
        return reached

    def page(self, number):
        if number >= self.config['MAX_SCRAPE_PAGES']:
            self.state['scrape_limit_reached'] = True
            return False
        return not self.bounded()

    def scan(self):
        if self.bounded():
            return False
        self.state['number_of_listings_scanned'] += 1
        self.checkpoint()
        return True

    def end_page(self):
        self.duplicate_pages = 0 if self.page_new else self.duplicate_pages + 1
        self.page_new = 0
        self.checkpoint()

    def accept(self, job):
        self.last_new = False
        entry, uncertain = self.ledger.match(job)
        if entry and not uncertain:
            category = ('historical_duplicates' if entry['disposition'] == 'HISTORICAL_MANUALLY_APPLIED' else
                        'current_run_duplicates' if entry.get('run_id') == self.state['run_id'] else 'previous_run_duplicates')
            self.state[category] += 1
            self.state['skipped'].append(dict(job, reason=category, matched_job_id=entry['job']['id']))
            self.checkpoint()
            return False
        policy = self.policy.evaluate(job['company'], job['id'])['company_policy']
        if uncertain:
            job['identity_review'] = True
        # Ledger first: after a crash, a reserved job can never become fresh again.
        self.ledger.records[job['id']] = dict(job=dict(job), keys=sorted(keys(job)),
            disposition='IDENTITY_REVIEW' if uncertain else 'DISCOVERED', run_id=self.state['run_id'], policy=policy)
        self.ledger.save()
        self.last_new = True
        self.page_new += 1
        group = 'excluded' if policy == 'EXCLUDED' else 'selected'
        self.state[group].append(dict(job))
        self.state['excluded_jobs'] += int(policy == 'EXCLUDED')
        self.state['referral_jobs'] += int(policy == 'REFERRAL_REQUIRED')
        self.checkpoint()
        print(f'[DEDUP] New {job["id"]}; selected {len(self.state["selected"])}; policy {policy}', flush=True)
        # identity_review is sidecar metadata, never add columns to scraper CSV.
        job.pop('identity_review', None)
        return policy != 'EXCLUDED'


def scrape_session(session, headed):
    import yaml
    from playwright.sync_api import sync_playwright
    from ..pipeline import Pipeline
    config = yaml.safe_load((session.root / 'config.yaml').read_text())
    config['max_scroll_attempts'] = min(config['max_scroll_attempts'], session.config['MAX_SCRAPE_PAGES'] - 1)
    with sync_playwright() as pw:
        context = pw.chromium.launch_persistent_context(str(session.root / 'browser-profile'), headless=not headed)
        try:
            pipeline = Pipeline(context.new_page(), config)
            print('[SCRAPER] Opening authenticated Jobright feed', flush=True)
            if session.state['stage'] == 'scraping' and len(session.state['selected']) < session.state['requested_job_limit']:
                pipeline.collect(session.state['requested_job_limit'] - len(session.state['selected']), session=session)
            if len(session.state['selected']) < session.state['requested_job_limit']:
                session.state['scrape_limit_reached'] = True
            # Persist the queue before fetching any descriptions.
            session.state['stage'] = 'fetching'
            session.checkpoint()
            pipeline.fetch(job_ids={j['id'] for j in session.state['selected']})
        finally:
            context.close()


def generate_one(root, agent, job):
    subprocess.run([sys.executable, str(root / 'generate_resumes.py'), '--agent-dir', str(agent),
                    '--job-id', job['id']], check=True)


def write_report(session):
    s = session.state
    rows = list(s['outcomes'].values())
    counts = Counter(r['status'] for r in rows)
    s.update(needs_review=counts['NEEDS_REVIEW'] + counts['NOT_SUPPORTED'] + counts['BLOCKED_AUTH'] + counts['BLOCKED_CAPTCHA'],
             failed_applications=counts['FAILED'], submission_unconfirmed=counts['SUBMISSION_UNCONFIRMED'])
    lines = ['# Job application pipeline — session report', '', *[f'{k}: {s[k]}' for k in
             ('run_id','start_time','end_time','run_status','failure_reason','requested_job_limit','number_of_listings_scanned',
              'new_jobs_found','historical_duplicates','previous_run_duplicates','current_run_duplicates','scrape_limit_reached',
              'excluded_jobs','referral_jobs','resume_generation_successes','resume_generation_failures','application_attempts',
              'verified_applications','needs_review','failed_applications','submission_unconfirmed')], '',
             'Limit counts new unique non-excluded jobs before referral routing. Referral jobs count; exclusions and known jobs do not.',
             f'Shortfall: {max(0, s["requested_job_limit"] - s["new_jobs_found"])}',
             f'Mode: {s.get("mode", "DRY RUN")}', 'Times use UTC. Manual historical exclusions are not confirmed submissions.']
    groups = [('Successfully submitted applications', [r for r in rows if r['status']=='APPLIED']),
              ('Referral-required jobs', [r for r in rows if r.get('company_policy')=='REFERRAL_REQUIRED']),
              ('Excluded companies', [r for r in rows if r.get('company_policy')=='EXCLUDED']),
              ('Resume generation failures', [r for r in rows if r['status']=='RESUME_FAILED']),
              ('Authentication failures', [r for r in rows if r['status'] in ('BLOCKED_AUTH','BLOCKED_CAPTCHA')]),
              ('Applications needing review', [r for r in rows if r['status'] in ('NEEDS_REVIEW','NOT_SUPPORTED','SUBMISSION_UNCONFIRMED')]),
              ('All session outcomes', rows), ('Previously known jobs skipped', s['skipped'])]
    lines += ['', '## Account management', '', *[f'{key}: {value}' for key, value in s.get('account_management', {}).items()]]
    fields = ['company','position','application_url','job_id','resume_path','company_policy','status','submitted_at','confirmation_evidence','confirmation_id','review_reason','error_message']
    for title, values in groups:
        lines += ['', '## ' + title, '', '| ' + ' | '.join(fields) + ' |', '|' + '|'.join('---' for _ in fields) + '|']
        for row in values:
            row = dict(row)
            row.setdefault('position', row.get('title',''))
            row.setdefault('job_id', row.get('id',''))
            row.setdefault('application_url', row.get('source_url') or row.get('jobright_url',''))
            row.setdefault('review_reason', row.get('reason',''))
            lines.append('| ' + ' | '.join(str(row.get(f,'')).replace('|','\\|').replace('\n',' ') for f in fields) + ' |')
    text = '\n'.join(lines) + '\n'
    atomic_write(session.folder / 'run_report.md', text)
    session.checkpoint()
    atomic_write(session.root / 'data/applications/latest_run_report.md', text)
    save_json(session.root / 'data/applications/last_session.json', s)
    atomic_write(session.root / 'data/applications/last_session.md', text)
    print('[REPORT] ' + str(session.folder / 'run_report.md'), flush=True)
    print('\n'.join(lines[:26]), flush=True)
    print('Account management: ' + json.dumps(s.get('account_management', {})), flush=True)
    print('Final status: ' + ('Completed with review items' if s['run_status'] == 'COMPLETED' and (s['needs_review'] or s['resume_generation_failures'] or s['failed_applications'] or s['submission_unconfirmed']) else s['run_status']), flush=True)


def full(root, agent, profile, config, limit=None, headed=False, run_id=None,
         scraper=scrape_session, generator=generate_one, processor=None):
    from .runner import process
    processor = processor or process
    session = Session(root, config, limit, run_id)
    s = session.state
    s['mode'] = 'DRY RUN' if config['DRY_RUN'] else 'LIVE (submission gated)'
    s['run_status'] = 'RUNNING'
    s['end_time'] = None
    print(f'[RUN] {s["run_id"]} — {s["mode"]}', flush=True)
    try:
        if s['stage'] in ('scraping', 'fetching'):
            scraper(session, headed)
            session.bounded()
            s['stage'] = 'resumes'
            session.checkpoint()
        # Refresh metadata from actual scraper storage, while keeping explicit selection.
        current = {j['id']: j for j in jobs(root)}
        store = Store(root / 'data/applications')
        try:
            for selected in s['excluded'] + s['selected']:
                identifier = selected['id']
                if identifier in s['outcomes']:
                    continue
                job = current.get(identifier, selected)
                policy = session.policy.evaluate(job['company'], identifier)
                prior = store.get(identifier)
                if prior and prior['status'] in PROTECTED:
                    s['outcomes'][identifier] = prior
                    session.checkpoint()
                    continue
                if policy['company_policy'] == 'EXCLUDED' or selected.get('identity_review'):
                    s['outcomes'][identifier] = store.update(identifier, company=job['company'], position=job['title'],
                        application_url=job.get('source_url') or job.get('jobright_url',''),
                        company_policy=policy['company_policy'], status='SKIPPED' if policy['company_policy']=='EXCLUDED' else 'NEEDS_REVIEW',
                        review_reason='USER_EXCLUDED_COMPANY' if policy['company_policy']=='EXCLUDED' else 'DUPLICATE_IDENTITY_UNCERTAIN')
                    if selected.get('identity_review'):
                        store.review(identifier, 'Duplicate identity', 'DUPLICATE_IDENTITY_UNCERTAIN')
                    session.checkpoint()
                    continue
                print(f'[RESUME] Preparing {identifier}', flush=True)
                try:
                    generator(root, agent, job)
                    artifact = resume_for(root, agent, job, profile.get('resume_approvals', {}), approval_mode=config.get('RESUME_APPROVAL_MODE', 'automatic'))
                    s['resume_results'][identifier] = 'completed'
                    s['resume_generation_successes'] = sum(v == 'completed' for v in s['resume_results'].values())
                except (Exception, SystemExit) as error:
                    s['resume_results'][identifier] = 'failed'
                    s['resume_generation_failures'] = sum(v == 'failed' for v in s['resume_results'].values())
                    s['outcomes'][identifier] = store.update(identifier, company=job['company'], position=job['title'],
                        company_policy=policy['company_policy'], status='RESUME_FAILED', error_message=type(error).__name__, review_reason='Resume generation or association validation failed')
                    session.checkpoint()
                    continue
                # Save the boundary before browser activity; resume regenerators reuse
                # completed exact-JD state. Protected tracker records win on recovery.
                session.checkpoint()
                s['application_attempts'] = sum(int(r.get('attempt_count') or 0) for r in store.rows() if r['job_id'] in {j['id'] for j in s['selected']})
                if s['application_attempts'] >= config.get('MAX_APPLICATIONS_PER_RUN', 5) and policy['company_policy'] == 'AUTO_APPLY':
                    s['outcomes'][identifier] = store.update(identifier, company=job['company'], position=job['title'], resume_path=artifact['path'], status='NEEDS_REVIEW', review_reason='Per-run application attempt limit reached', company_policy=policy['company_policy'])
                    session.checkpoint()
                    continue
                print(f'[APPLICATION] Processing {identifier}', flush=True)
                result = processor(root, agent, profile, config, mode='dry-run' if config['DRY_RUN'] else 'apply',
                                   job_ids={identifier}, limit=1, headed=headed)
                row = store.get(identifier)
                if row:
                    s['outcomes'][identifier] = row
                s['verified_applications'] += result['verified_submissions_this_run']
                s['application_attempts'] = sum(int(r.get('attempt_count') or 0) for r in store.rows() if r['job_id'] in {j['id'] for j in s['selected']})
                session.checkpoint()
                if result.get('stopped'):
                    raise KeyboardInterrupt
        finally:
            ids = {j['id'] for j in s['selected']}
            accounts = [a for a in store.accounts() if ids.intersection(json.loads(a.get('associated_job_ids') or '[]'))]
            s['account_management'] = dict(new_accounts_created=sum(a.get('created_at','') >= s['start_time'] for a in accounts), existing_account_logins=sum(a.get('last_login_at','') >= s['start_time'] for a in accounts), authentication_blockers=sum(a.get('account_status') in ('VERIFICATION_REQUIRED','LOGIN_FAILED','PASSWORD_RECOVERY_REQUIRED','ACCOUNT_CREATION_FAILED') for a in accounts))
            store.close()
        s['verified_applications'] = sum(r.get('status') == 'APPLIED' and bool(r.get('confirmation_evidence')) for r in s['outcomes'].values())
        s['run_status'] = 'COMPLETED'
    except BaseException as error:
        s['run_status'] = 'INTERRUPTED' if isinstance(error, (KeyboardInterrupt, SystemExit)) else 'FAILED'
        s['failure_reason'] = type(error).__name__
        raise
    finally:
        s['end_time'] = now()
        write_report(session)
    return s


def show_reports(root, last=None, run_id=None):
    folder = root / 'data/applications/runs'
    paths = sorted(folder.glob('*/run_summary.json'), key=lambda p: json.loads(p.read_text())['start_time'], reverse=True)
    if run_id:
        if Path(run_id).name != run_id:
            raise ValueError('Invalid run ID')
        print((folder / run_id / 'run_report.md').read_text())
    elif last == 1:
        if paths:
            print(paths[0].with_name('run_report.md').read_text())
        else:
            print('No sessions recorded.')
    else:
        for path in paths[:last]:
            state = json.loads(path.read_text())
            print(f'{state["run_id"]} {state["run_status"]} new={state["new_jobs_found"]} verified={state["verified_applications"]}')

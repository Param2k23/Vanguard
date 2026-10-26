import json
import signal
import traceback
from collections import Counter
from pathlib import Path

from ..company_policy import CompanyPolicy
from .accounts import AccountManager
from .answers import generate_application_answer
from .browser import Blocked, NativeFormAdapter, navigate
from .integration import ReadinessError, application_url, identity, jobs, resume_for
from .storage import PROTECTED, Store, now, save_json, atomic_write


def report(store, rows, directory, mode, run_started_at=None):
    stamp = now()
    counts = dict(Counter(row['status'] for row in rows))
    summary = dict(run_at=stamp, mode=mode, total_jobs_evaluated=len(rows), counts=counts,
                   verified_submissions_this_run=sum(r.get('_submitted_this_run', False) for r in rows),
                   applications=rows)
    policy_rows = [r for r in rows if r['status'] not in PROTECTED]
    summary['overall'] = dict(jobs_evaluated=len(rows), successfully_applied=summary['verified_submissions_this_run'],
        already_applied=sum(r['status'] in ('APPLIED','ALREADY_APPLIED') and not r.get('_submitted_this_run') for r in rows),
        referral_required=sum(r.get('company_policy') == 'REFERRAL_REQUIRED' for r in policy_rows),
        excluded=sum(r.get('company_policy') == 'EXCLUDED' for r in policy_rows),
        other_needs_review=sum(r['status'] == 'NEEDS_REVIEW' and r.get('company_policy') != 'REFERRAL_REQUIRED' for r in rows),
        failed=counts.get('FAILED',0), submission_unconfirmed=counts.get('SUBMISSION_UNCONFIRMED',0))
    ids = {r['job_id'] for r in rows}
    accounts = [a for a in store.accounts() if ids.intersection(json.loads(a.get('associated_job_ids') or '[]'))]
    summary['accounts'] = accounts
    summary['account_management'] = dict(
        new_accounts_created=sum(bool(run_started_at) and a.get('created_at','') >= run_started_at for a in accounts),
        existing_accounts_accessed=sum(bool(run_started_at) and a.get('last_login_at','') >= run_started_at and a['account_status'] in ('EXISTING_ACCOUNT','LOGGED_IN') for a in accounts),
        registration_failures=sum(a['account_status']=='ACCOUNT_CREATION_FAILED' for a in accounts),
        login_failures=sum(a['account_status'] in ('LOGIN_FAILED','PASSWORD_RECOVERY_REQUIRED') for a in accounts),
        verification_required=sum(a['account_status']=='VERIFICATION_REQUIRED' for a in accounts),
        vault_errors=sum('VAULT' in a.get('last_error','') for a in accounts))
    folder = directory / 'reports'
    name = stamp.replace(':', '-').replace('+', '_')
    save_json(folder / f'{name}.json', summary)
    lines = ['# Application Run Summary', '', f'Run date: {stamp}', f'Mode: {mode}',
             f'Total jobs evaluated: {len(rows)}',
             f'Verified submissions this run: {summary["verified_submissions_this_run"]}', '',
             '## Status counts', '', *[f'- {key}: {value}' for key, value in sorted(counts.items())]]
    for title, statuses in [('Successfully submitted', {'APPLIED'}), ('Needs review', {'NEEDS_REVIEW','BLOCKED_AUTH','BLOCKED_CAPTCHA','SUBMISSION_UNCONFIRMED','NOT_SUPPORTED','WAITING_FOR_RESUME'}), ('Failures', {'FAILED'}), ('Other outcomes', set(counts) - {'APPLIED','NEEDS_REVIEW','BLOCKED_AUTH','BLOCKED_CAPTCHA','SUBMISSION_UNCONFIRMED','NOT_SUPPORTED','WAITING_FOR_RESUME','FAILED'})]:
        lines += ['', '## ' + title, '', '| Company | Position | Status | Evidence / reason |', '|---|---|---|---|']
        for row in rows:
            if row['status'] in statuses:
                cells = [row.get('company',''), row.get('position',''), row['status'], row.get('confirmation_evidence') or row.get('review_reason') or row.get('error_message') or row.get('notes','')]
                lines.append('| ' + ' | '.join(str(v).replace('|','\\|').replace('\n',' ') for v in cells) + ' |')
    def table(title, columns, values):
        lines.extend(['', '## ' + title, '', '| ' + ' | '.join(columns) + ' |', '|' + '|'.join('---' for _ in columns) + '|'])
        for values_row in values:
            lines.append('| ' + ' | '.join(str(v).replace('|','\\|').replace('\n',' ') for v in values_row) + ' |')
    table('Successfully applied', ['Company','Position','Resume','Submitted at','Confirmation'],
          [[r.get(c,'') for c in ('company','position','resume_path','submitted_at','confirmation_evidence')] for r in rows if r['status']=='APPLIED'])
    table('Referral attention required', ['Company','Position','Job URL','Resume','Referral status','Contact','Notes'],
          [[r.get(c,'') for c in ('company','position','job_url','resume_path','referral_status','referral_contact','referral_notes')] for r in policy_rows if r.get('company_policy')=='REFERRAL_REQUIRED'])
    table('Permanently excluded jobs', ['Company','Position','Reason'],
          [[r.get(c,'') for c in ('company','position','review_reason')] for r in policy_rows if r.get('company_policy')=='EXCLUDED'])
    table('Account management', ['Metric','Count'], summary['account_management'].items())
    table('Other application review cases', ['Company','Position','Reason'],
          [[r.get(c,'') for c in ('company','position','review_reason')] for r in rows if r['status']=='NEEDS_REVIEW' and r.get('company_policy')!='REFERRAL_REQUIRED'])
    table('Overall summary', ['Metric','Count'], summary['overall'].items())
    lines += ['', 'Account creation/access counts describe this run only; failures/verification/vault counts describe associated stored account states.', 'Counts describe this run’s evaluated records. Existing APPLIED records are history, not new submissions.', 'Only controlled local fixture submissions are used during development.']
    atomic_write(folder / f'{name}.md', '\n'.join(lines) + '\n')
    save_json(directory / 'latest_summary.json', summary)
    atomic_write(directory / 'latest_report.md', '\n'.join(lines) + '\n')
    save_json(directory / 'last_session.json', summary)
    atomic_write(directory / 'last_session.md', '\n'.join(lines) + '\n')
    store.export()
    print(f'Evaluated {len(rows)}; verified new submissions {summary["verified_submissions_this_run"]}; ' + ', '.join(f'{k}={v}' for k,v in sorted(counts.items())))
    return summary


def process(root, agent, profile, config, mode='inspect', job_id=None, company=None, limit=None,
            headed=False, retry=False, resume_review=False, only_ready=False, context=None, job_ids=None):
    config = dict(config)
    if mode in ('inspect', 'dry-run', 'watch'):
        config['DRY_RUN'] = True
    directory = root / 'data/applications'
    company_policy = CompanyPolicy.load(root)
    store = Store(directory)
    from .resume_migration import migrate_approval_reviews
    migrate_approval_reviews(root, agent, profile, config, store, job_ids={job_id} if job_id else job_ids)
    run_started_at = now()
    stop = [False]
    old_handler = signal.getsignal(signal.SIGINT)
    old_terminate = signal.getsignal(signal.SIGTERM)
    def interrupt(sig, frame):
        stop[0] = True
        print('Stopping after the current job; progress is being persisted.')
    signal.signal(signal.SIGINT, interrupt)
    signal.signal(signal.SIGTERM, interrupt)
    baseline_path = directory / 'historical_baseline.json'
    historical = json.loads(baseline_path.read_text())['records'] if baseline_path.exists() else []
    evaluated = []
    attempted = 0
    owned_context = None
    pw = None
    try:
        selected = [j for j in jobs(root) if (job_ids is None or j['id'] in job_ids) and (not job_id or j['id'] == job_id) and (not company or company.lower() in j['company'].lower())]
        maximum = min(limit or config['MAX_APPLICATIONS_PER_RUN'], config['MAX_APPLICATIONS_PER_RUN'])
        for job in selected:
            if stop[0] or attempted >= maximum or (mode == 'inspect' and len(evaluated) >= maximum):
                break
            prior = store.get(job['id'])
            from .sessions import keys
            historical_match = any(keys(job).intersection(set(entry['keys']) | keys(entry['job'])) for entry in historical)
            if historical_match:
                row = prior if prior and prior['status'] in PROTECTED else store.update(job['id'], status='HISTORICAL_MANUALLY_APPLIED', company=job['company'], position=job['title'], review_reason='Historical manual application exclusion; no confirmation claimed')
                evaluated.append(row)
                continue
            policy = company_policy.evaluate(job['company'], job['id'])
            updates = dict(company_policy=policy['company_policy'], application_method=policy['application_method'], policy_evaluated_at=now())
            if prior and prior['status'] in PROTECTED:
                evaluated.append(store.update(job['id'], **updates))
                continue
            if policy['company_policy'] != 'AUTO_APPLY':
                updates.update(company=job['company'], position=job['title'], job_url=job.get('jobright_url',''), application_url=job.get('source_url') or job.get('jobright_url',''),
                               status='SKIPPED' if policy['company_policy'] == 'EXCLUDED' else 'NEEDS_REVIEW', review_reason=policy['review_reason'])
                if policy['company_policy'] == 'REFERRAL_REQUIRED':
                    updates['referral_status'] = (prior or {}).get('referral_status') or 'NOT_REQUESTED'
                    try:
                        artifact = resume_for(root, agent, job, profile.get('resume_approvals', {}), approval_mode=config.get('RESUME_APPROVAL_MODE', 'automatic'))
                        updates.update(resume_path=artifact['path'], resume_version=artifact['sha256'])
                    except ReadinessError as error:
                        updates.update(error.details)
                        updates['notes'] = 'Referral retained; resume readiness: ' + str(error)
                    except (OSError, ValueError, KeyError):
                        updates['notes'] = 'Referral retained; resume metadata requires review'
                row = store.update(job['id'], **updates)
                if policy['company_policy'] != 'EXCLUDED':
                    store.review(job['id'], 'Referral attention' if policy['company_policy'] == 'REFERRAL_REQUIRED' else 'Employer identity', policy['review_reason'], application_url=row['application_url'])
                else:
                    with store.db:
                        store.db.execute('UPDATE reviews SET resolved=1 WHERE job_id=?', (job['id'],))
                evaluated.append(row)
                continue
            store.update(job['id'], **updates)
            if mode == 'retry' and (not prior or prior['status'] != 'FAILED'):
                continue
            if prior and prior['status'] in {'NEEDS_REVIEW','BLOCKED_AUTH','BLOCKED_CAPTCHA','JOB_CLOSED','NOT_SUPPORTED'} and mode != 'inspect' and not resume_review:
                evaluated.append(store.get(job['id']))
                continue
            row = store.update(job['id'], company=job['company'], position=job['title'], location=job.get('location',''), job_url=job.get('jobright_url',''))
            try:
                reviewed = config.get('REVIEWED_DESTINATION')
                approved_job = job
                if reviewed:
                    from .review_batches import Batches
                    batches = Batches(root)
                    try:
                        reviewed_row = next(r for r in batches.rows(reviewed['batch_id']) if r['job_id'] == job['id'])
                        if any(job.get(k, '') != reviewed_row['_job'].get(k, '') for k in ('company','title','source_url','jobright_url')):
                            raise ReadinessError('NEEDS_REVIEW', 'Stored destination identity changed')
                        approved_job = dict(job, source_url=reviewed_row['application_url'])
                        validated = batches.approval(reviewed_row, approved_job)
                        if validated != reviewed:
                            raise ReadinessError('NEEDS_REVIEW', 'Approval snapshot changed')
                        destination = validated['destination']
                        config['APPROVED_FORM_ORIGINS'] = [destination['origin']]
                        config['APPROVED_SUBMIT_ORIGINS'] = [destination['origin']]
                    finally:
                        batches.close()
                url = application_url(approved_job)
                if any(('url:' + identity(url)) in (set(entry['keys']) | keys(entry['job'])) for entry in historical):
                    row = store.update(job['id'], status='HISTORICAL_MANUALLY_APPLIED', review_reason='Historical application destination; no resubmission')
                    evaluated.append(row)
                    continue
                duplicate = store.duplicate(identity(url), job['id'])
                if duplicate:
                    status = 'SUBMISSION_UNCONFIRMED' if duplicate['status'] == 'SUBMISSION_UNCONFIRMED' else 'ALREADY_APPLIED'
                    row = store.update(job['id'], identity=identity(url), status=status, application_url=url, review_reason='Existing requisition record: ' + duplicate['job_id'], confirmation_evidence=duplicate.get('confirmation_evidence',''))
                    evaluated.append(row)
                    continue
                resume = resume_for(root, agent, job, profile.get('resume_approvals', {}), approval_mode=config.get('RESUME_APPROVAL_MODE', 'automatic'))
                if mode == 'watch' and prior and prior['status'] == 'READY_TO_APPLY' and prior.get('prepared_profile_version') == profile['version'] and prior.get('resume_version') == resume['sha256'] and prior.get('notes', '').startswith('Dry run:'):
                    continue
                job = approved_job
                row = store.update(job['id'], identity=identity(url), application_url=url, resume_path=resume['path'], resume_version=resume['sha256'], resume_status='READY_FOR_APPLICATION', resume_acceptance_mode=config.get('RESUME_APPROVAL_MODE', 'automatic'), status='READY_TO_APPLY', review_reason='', error_message='')
                if mode == 'inspect':
                    evaluated.append(row)
                    continue
                daily = sum(1 for r in store.rows() if r.get('submitted_at','').startswith(now()[:10]) or r['status'] == 'SUBMISSION_UNCONFIRMED')
                if daily >= config['MAX_APPLICATIONS_PER_DAY']:
                    row = store.update(job['id'], status='SKIPPED', notes='Conservative daily submission/uncertainty limit reached')
                    evaluated.append(row)
                    continue
                attempted += 1
                if context is None:
                    if pw is None:
                        from playwright.sync_api import sync_playwright
                        pw = sync_playwright().start()
                        # A separate persistent profile avoids collision with the scraper.
                        owned_context = pw.chromium.launch_persistent_context(str(root / 'browser-profile-applications'), headless=not headed, service_workers='block')
                    active_context = owned_context
                else:
                    active_context = context
                row = store.update(job['id'], status='IN_PROGRESS', first_attempt_at=row.get('first_attempt_at') or now(), last_attempt_at=now(), attempt_count=int(row.get('attempt_count') or 0) + 1)
                page = active_context.new_page()
                page.set_default_timeout(10000)
                adapter = None
                rejected_destinations = None
                journey = None
                opened_pages=set(active_context.pages)
                try:
                    journey = None
                    if reviewed:
                        from .journey import ApplicationJourney
                        journey = ApplicationJourney(page,job,config,store)
                        journey.install()
                        config['APPLICATION_JOURNEY'] = journey
                        journey.step='Opening approved application URL'
                        journey.log('URL','Opening approved application URL',job['source_url'])
                    navigate(page, job, config.get('NAVIGATION_TIMEOUT_MS', 30000))
                    if journey:
                        page=journey.locate()
                        journey.step='Checking login requirement'
                        journey.log('AUTH','Checking observed login requirements',page.url)
                    AccountManager(root, config, profile, store, job).ensure_authenticated(page)
                    generator = None
                    if config.get('AI_ANSWERS_ENABLED'):
                        generator = lambda question, max_chars, max_words: generate_application_answer(question, job, resume, profile, agent, directory / job['id'] / 'answers', store, max_chars=max_chars, max_words=max_words)
                    adapter = NativeFormAdapter(page, config, profile, store, job, resume, directory / job['id'], generator)
                    outcome = adapter.run()
                    row = store.update(job['id'], prepared_profile_version=profile['version'], **outcome)
                    if outcome['status'] in ('READY_TO_APPLY', 'APPLIED'):
                        with store.db:
                            store.db.execute('UPDATE reviews SET resolved=1 WHERE job_id=?', (job['id'],))
                    if outcome['status'] == 'APPLIED':
                        row = dict(row, _submitted_this_run=True)
                except Blocked as error:
                    if store.get(job['id'])['status'] == 'SUBMISSION_UNCONFIRMED':
                        row = store.update(job['id'], review_reason='Submission may have occurred; inspect portal before any retry')
                    else:
                        row = store.update(job['id'], status=error.status, review_reason=str(error),current_step=journey.step if journey else 'Opening application',last_observed_url=__import__('job_pipeline.applications.journey',fromlist=['display_url']).display_url(page.url))
                        if journey:journey.log('STATUS',str(error),page.url)
                        if error.status == 'NEEDS_REVIEW':
                            store.review(job['id'], error.question or 'Application review', str(error), application_url=application_url(job))
                except Exception as error:
                    # Never log Playwright exception text/URLs/input values in ordinary logs.
                    status = 'SUBMISSION_UNCONFIRMED' if store.get(job['id'])['status'] == 'SUBMISSION_UNCONFIRMED' else 'FAILED'
                    row = store.update(job['id'], status=status, error_message=type(error).__name__, review_reason='Inspect local diagnostics; no submit retry' if status == 'SUBMISSION_UNCONFIRMED' else '')
                    if journey:journey.log('STATUS','Browser error at '+journey.step+': '+type(error).__name__,page.url)
                    save_json(directory / job['id'] / 'error.json', dict(error_class=type(error).__name__, frames=[dict(file=Path(f.filename).name, line=f.lineno, function=f.name) for f in traceback.extract_tb(error.__traceback__)], last_action=store.get(job['id']).get('notes')))
                finally:
                    if adapter:
                        adapter.persist_progress()
                    if journey:
                        save_json(directory / job['id'] / 'navigation.json',dict(events=journey.events,verified_identity=journey.verified))
                        journey.log('STATUS','Saved result: '+store.get(job['id'])['status'],page.url)
                        journey.close()
                    for opened in set(active_context.pages)-opened_pages:
                        opened.close()
            except ReadinessError as error:
                if only_ready and error.status == 'WAITING_FOR_RESUME':
                    continue
                row = store.update(job['id'], status=error.status, review_reason=str(error), **error.details)
                if error.status == 'NEEDS_REVIEW':
                    store.review(job['id'], 'Resume/readiness', str(error))
            except Exception as error:
                current = store.get(job['id'])
                status = current['status'] if current['status'] in PROTECTED else 'FAILED'
                row = store.update(job['id'], status=status, error_message=type(error).__name__)
            evaluated.append(row)
    finally:
        if owned_context:
            owned_context.close()
        if pw:
            pw.stop()
        signal.signal(signal.SIGINT, old_handler)
        signal.signal(signal.SIGTERM, old_terminate)
        summary = report(store, evaluated, directory, mode, run_started_at=run_started_at)
        store.close()
    summary['stopped'] = stop[0]
    return summary

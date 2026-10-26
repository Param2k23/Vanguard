#!/usr/bin/env python3
"""Pipeline 3: inspect, fill, submit and track verified job-specific applications."""
import argparse
import fcntl
import json
import os
import subprocess
import sys
from pathlib import Path

from job_pipeline.applications.integration import jobs, resume_for, ReadinessError
from job_pipeline.applications.profile import load_profile
from job_pipeline.applications.runner import process, report
from job_pipeline.applications.storage import Store, digest, now, save_json

ROOT = Path(__file__).resolve().parent


def configuration(path):
    config = json.loads((ROOT / 'application_config.json').read_text())
    if path.exists():
        config.update(json.loads(path.read_text()))
    for key in ('AUTO_SUBMIT_ENABLED','DRY_RUN','REVIEW_REQUIRED_FOR_UNCERTAIN_FIELDS','EEO_AUTOFILL_ENABLED','AI_ANSWERS_ENABLED','ACCOUNT_CREATION_ENABLED','ACCOUNT_LOGIN_ENABLED','ACCOUNT_CREATION_DRY_RUN','SECURE_CREDENTIAL_STORAGE'):
        if type(config[key]) is not bool:
            raise ValueError(key + ' must be a JSON boolean')
    for key in ('MAX_APPLICATIONS_PER_RUN','MAX_APPLICATIONS_PER_DAY','MAX_STEPS','NAVIGATION_TIMEOUT_MS','MAX_AUTH_ATTEMPTS','AUTH_RESULT_TIMEOUT_MS'):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(key + ' must be a positive integer')
    for key in ('DEFAULT_NEW_JOB_LIMIT','MAX_SCRAPE_LISTINGS','MAX_SCRAPE_PAGES','MAX_SCRAPE_DURATION_SECONDS','MAX_DUPLICATE_ONLY_PAGES'):
        if type(config[key]) is not int or config[key] < 1:
            raise ValueError(key + ' must be a positive integer')
    if config.get('RESUME_APPROVAL_MODE') not in ('automatic', 'manual'):
        raise ValueError('RESUME_APPROVAL_MODE must be automatic or manual')
    from job_pipeline.applications.browser import origin
    from job_pipeline.applications.integration import canonical_url
    for key in ('APPROVED_FORM_ORIGINS', 'APPROVED_SUBMIT_ORIGINS'):
        if not isinstance(config[key], list) or any(not isinstance(value, str) or canonical_url(value) != origin(value) + '/' for value in config[key]):
            raise ValueError(key + ' must be a list of exact HTTP(S) origins without paths')
        config[key] = [origin(value) for value in config[key]]
    if config['MAX_AUTH_ATTEMPTS'] > 2 or config['DEFAULT_COMPANY_POLICY'] != 'AUTO_APPLY':
        raise ValueError('Authentication attempts must be 1..2 and default company policy AUTO_APPLY')
    if not isinstance(config['ACCOUNT_PORTALS'], list):
        raise ValueError('ACCOUNT_PORTALS must be an array')
    for portal in config['ACCOUNT_PORTALS']:
        if not isinstance(portal, dict) or not all(isinstance(portal.get(k), str) and portal[k].strip() for k in ('company','origin','tenant','login_url','registration_url','authenticated_selector')):
            raise ValueError('Each approved account portal needs company, origin, tenant, login_url, registration_url and authenticated_selector')
        if canonical_url(portal['origin']) != origin(portal['origin']) + '/':
            raise ValueError('Account portal origin must have no path')
        portal['origin'] = origin(portal['origin'])
    # Uncertainty always blocks even if this preference is disabled.
    return config


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('mode', choices=['inspect','dry-run','apply','review','retry','report','full','watch','login','approve-resume','migrate-resume-reviews','resolve','reconcile','accounts','referrals','referral-update','vault-init','vault-get','vault-import','prepare','batches','apply-approved','migrate-review-batch','correct-review-url'])
    p.add_argument('--agent-dir', type=Path, default=ROOT.parent / 'job-agent')
    p.add_argument('--profile', type=Path, default=ROOT / 'applicant_profile.json')
    p.add_argument('--config', type=Path, default=ROOT / 'application_config.local.json')
    p.add_argument('--fresh', action='store_true', help='Full runs always scrape fresh Jobright listings')
    p.add_argument('--resume-run', help='Resume an interrupted full session with its original run ID')
    p.add_argument('--last', nargs='?', const=1, type=int, help='Report latest session, or N sessions')
    p.add_argument('--run-id', help='Report one session')
    p.add_argument('--batch-id')
    p.add_argument('--status', choices=['pending','ready','all'], default='all', help='Review batch filter')
    p.add_argument('--job-id')
    p.add_argument('--company')
    p.add_argument('--limit', type=int)
    browser = p.add_mutually_exclusive_group()
    browser.add_argument('--headed', action='store_true')
    browser.add_argument('--headless', action='store_true')
    p.add_argument('--resume-review', action='store_true', help='Explicitly revisit review/auth/unsupported cases after resolving blockers')
    p.add_argument('--only-completed-resumes', action='store_true')
    p.add_argument('--dry-run', action='store_true')
    submit = p.add_mutually_exclusive_group()
    submit.add_argument('--enable-auto-submit', action='store_true', help='Requires production-enabled local config too')
    submit.add_argument('--disable-auto-submit', action='store_true')
    p.add_argument('--enable-account-creation', action='store_true', help='Requires persistent account creation dry-run=false and application dry-run=false')
    p.add_argument('--disable-account-login', action='store_true')
    p.add_argument('--account-id')
    p.add_argument('--include-unverified', action='store_true', help='Allow deliberate retrieval of an encrypted pending credential for manual recovery')
    p.add_argument('--referral-status', choices=['NOT_REQUESTED','REFERRAL_REQUESTED','REFERRAL_RECEIVED','REFERRAL_UNAVAILABLE','READY_FOR_MANUAL_APPLICATION'])
    p.add_argument('--referral-file', type=Path, help='JSON containing referral_contact/referral_notes; no automatic outreach')
    p.add_argument('--review-id')
    p.add_argument('--confirmation-file', type=Path, help='JSON containing manually verified confirmation_evidence, confirmation_id, submitted_at')
    p.add_argument('--answer-file', type=Path, help='JSON scalar containing a user-verified answer; avoids sensitive CLI arguments')
    p.add_argument('--sha256', help='Exact reviewed PDF hash for approve-resume')
    p.add_argument('--url', default='https://jobright.ai/', help='Manual login URL')
    p.add_argument('--interval', type=int, default=30, help='Integrated watch poll interval seconds')
    args = p.parse_args(argv)
    if args.limit is not None and args.limit < 1 or args.interval < 1:
        p.error('Limits/interval must be positive')
    root = ROOT
    directory = root / 'data/applications'
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Kernel lock releases after crash; never delete stale lock files manually.
    with (directory / 'run.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            p.error('Another Pipeline 3 command is active')
        os.umask(0o077)
        config = configuration(args.config)
        if args.mode in ('dry-run','inspect') or args.dry_run:
            config['DRY_RUN'] = True
        if not args.enable_account_creation:
            config['ACCOUNT_CREATION_DRY_RUN'] = True
        if args.disable_account_login:
            config['ACCOUNT_LOGIN_ENABLED'] = False
        if args.disable_auto_submit:
            config['AUTO_SUBMIT_ENABLED'] = False
        # Production requires both persistent opt-in and a per-run switch.
        if not args.enable_auto_submit:
            config['AUTO_SUBMIT_ENABLED'] = False
        if args.mode == 'login':
            from playwright.sync_api import sync_playwright
            from job_pipeline.applications.integration import canonical_url
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(str(root / 'browser-profile-applications'), headless=False)
                try:
                    context.new_page().goto(canonical_url(args.url))
                    input('Authenticate manually, then press Enter to save session: ')
                finally:
                    context.close()
            return 0
        if args.mode in ('vault-init','vault-get','vault-import'):
            from getpass import getpass
            from job_pipeline.applications.vault import CredentialVault
            passphrase = os.environ.get('JOB_PORTAL_VAULT_PASSPHRASE') or getpass('Vault master passphrase: ')
            vault = CredentialVault(root / 'data/credentials/vault.json', passphrase)
            if args.mode == 'vault-init':
                if not os.environ.get('JOB_PORTAL_VAULT_PASSPHRASE') and getpass('Confirm master passphrase: ') != passphrase:
                    p.error('Master passphrases differ')
                vault.initialize()
                print('Encrypted vault initialized. Master passphrase is not saved.')
                return 0
            if not args.account_id:
                p.error('This operation requires --account-id from accounts output')
            account_store = Store(directory)
            try:
                account = account_store.account(args.account_id)
                if not account:
                    p.error('Account not found')
                reference = account.get('credential_reference') or 'portal:' + args.account_id
                if args.mode == 'vault-get':
                    if not sys.stdout.isatty():
                        p.error('Password retrieval requires a private interactive terminal; redirection is refused')
                    credential = vault.get(reference)
                    if not credential:
                        p.error('No encrypted credential available')
                    if not credential.get('verified') and not args.include_unverified:
                        p.error('Credential acceptance is unverified; inspect account status or explicitly use --include-unverified for manual recovery')
                    print(credential['password'])
                else:
                    password = getpass('Existing portal password (not echoed): ')
                    if not password or getpass('Confirm portal password: ') != password:
                        p.error('Portal passwords empty or different')
                    from job_pipeline.applications.identity import CONFIRMED_EMAIL
                    vault.put(reference, dict(password=password, email=CONFIRMED_EMAIL, verified=False, trusted_for_login=True, updated_at=now()))
                    account_store.update_account(args.account_id, credential_reference=reference, password_type='EXISTING_SAVED', verification_status='USER_PROVIDED', account_status='NEEDS_REVIEW', auth_blocked=False, last_error='', notes='User imported existing credential; verify through bounded login')
                    print('Credential encrypted; no login attempted.')
            finally:
                account_store.close()
            return 0
        if args.mode in ('accounts','referrals','referral-update'):
            status_store = Store(directory)
            try:
                if args.mode == 'accounts':
                    for account in status_store.accounts():
                        print(json.dumps({k: account.get(k,'') for k in ('account_id','company','portal_domain','account_status','password_type','verification_status','last_error')}, ensure_ascii=False))
                elif args.mode == 'referrals':
                    for row in status_store.rows():
                        if row['company_policy'] == 'REFERRAL_REQUIRED' and row['status'] not in ('APPLIED','ALREADY_APPLIED','SUBMISSION_UNCONFIRMED'):
                            print(json.dumps({k: row.get(k,'') for k in ('job_id','company','position','job_url','resume_path','referral_status','referral_contact','referral_notes')}, ensure_ascii=False))
                else:
                    row = status_store.get(args.job_id)
                    if not row or row['company_policy'] != 'REFERRAL_REQUIRED' or not args.referral_status:
                        p.error('referral-update requires a referral job ID and --referral-status')
                    changes = dict(referral_status=args.referral_status)
                    if args.referral_file:
                        data = json.loads(args.referral_file.read_text())
                        for key in ('referral_contact','referral_notes'):
                            if key in data:
                                if not isinstance(data[key], str):
                                    p.error('Referral contact/notes must be strings')
                                changes[key] = data[key]
                    status_store.update(args.job_id, **changes)
                    print('Referral information saved; automatic application remains blocked.')
            finally:
                status_store.close()
            return 0
        if args.mode == 'batches':
            from job_pipeline.applications.review_batches import Batches
            batches = Batches(root)
            try:
                for record in batches.db.execute('SELECT payload FROM review_batches ORDER BY rowid DESC'):
                    meta = json.loads(record[0])
                    rows = batches.rows(meta['batch_id'])
                    pending = sum(r['review_decision'] in ('PENDING','NEEDS_CORRECTION') for r in rows)
                    if args.status == 'pending' and not pending or args.status == 'ready' and meta['status'] != 'READY_FOR_REVIEW':
                        continue
                    print(f"{meta['batch_id']} {meta['status']} jobs={len(rows)} pending={pending} {batches.folder(meta['batch_id']) / 'application_review.csv'}")
            finally:
                batches.close()
            return 0
        if args.mode == 'report' and args.last == 1 and (directory / 'last_session.md').exists():
            print((directory / 'last_session.md').read_text())
            return 0
        if args.mode == 'report' and (args.last is not None or args.run_id):
            from job_pipeline.applications.sessions import show_reports
            if args.last is not None and args.last < 1:
                p.error('--last must be positive')
            show_reports(root, args.last, args.run_id)
            return 0
        if not args.profile.exists():
            p.error('Missing private profile: copy applicant_profile.example.json to applicant_profile.json and fill verified facts')
        profile = load_profile(args.profile)
        if args.mode in ('prepare','apply-approved','migrate-review-batch','correct-review-url'):
            from job_pipeline.applications.review_batches import prepare, apply_approved, migrate_existing
            if args.mode == 'prepare':
                prepare(root, args.agent_dir.resolve(), profile, config, args.limit or 5, args.headed, args.resume_run)
            elif args.mode == 'apply-approved':
                if not args.batch_id:
                    p.error('apply-approved requires --batch-id')
                apply_approved(root, args.agent_dir.resolve(), profile, config, args.batch_id, args.headed)
            elif args.mode == 'correct-review-url':
                if not args.batch_id or not args.job_id:
                    p.error('correct-review-url requires --batch-id, --job-id and --url')
                from job_pipeline.applications.review_batches import correct_destination
                correct_destination(root,args.batch_id,args.job_id,args.url,args.headed)
            else:
                if not args.job_id:
                    p.error('migrate-review-batch requires --job-id')
                print(migrate_existing(root, args.agent_dir.resolve(), profile, config, args.job_id))
            return 0
        if args.mode == 'migrate-resume-reviews':
            from job_pipeline.applications.resume_migration import migrate_approval_reviews
            store = Store(directory)
            try:
                print(json.dumps(migrate_approval_reviews(root, args.agent_dir.resolve(), profile, config, store), indent=2))
            finally:
                store.close()
            return 0
        if args.mode == 'approve-resume':
            if not args.job_id or not args.sha256:
                p.error('approve-resume requires --job-id and --sha256 after reviewing PDF/report facts')
            job = next((j for j in jobs(root) if j['id'] == args.job_id), None)
            if job is None:
                p.error('Unknown job ID')
            # Validate all associations before persisting approval.
            resume_for(root, args.agent_dir.resolve(), job, {args.job_id: args.sha256}, approval_mode='manual')
            profile.pop('version', None)
            profile.setdefault('resume_approvals', {})[args.job_id] = args.sha256
            save_json(args.profile, profile)
            print('Approved exact factual resume hash; no application opened.')
            return 0
        if args.mode in ('review','report','resolve','reconcile'):
            store = Store(directory)
            try:
                if args.mode == 'review':
                    for row in store.reviews():
                        print(json.dumps(row, ensure_ascii=False))
                elif args.mode == 'reconcile':
                    if not args.job_id or not args.confirmation_file:
                        p.error('reconcile requires --job-id and --confirmation-file after read-only portal inspection')
                    row = store.get(args.job_id)
                    if not row or row['status'] != 'SUBMISSION_UNCONFIRMED':
                        p.error('Only uncertain submissions can be reconciled')
                    confirmation = json.loads(args.confirmation_file.read_text())
                    if not all(isinstance(confirmation.get(k), str) and confirmation[k].strip() for k in ('confirmation_evidence', 'submitted_at')):
                        p.error('Confirmation evidence and submitted timestamp required')
                    from datetime import datetime
                    if datetime.fromisoformat(confirmation['submitted_at']).tzinfo is None:
                        p.error('submitted_at must include a timezone')
                    store.update(args.job_id, status='APPLIED', confirmation_evidence=confirmation['confirmation_evidence'], confirmation_id=confirmation.get('confirmation_id', ''), submitted_at=confirmation['submitted_at'], notes='User verified confirmation through read-only portal inspection')
                    print('Submission reconciled from user-verified evidence; no browser actions taken.')
                elif args.mode == 'resolve':
                    if not args.review_id or not args.answer_file:
                        p.error('resolve requires --review-id and --answer-file')
                    row = store.db.execute('SELECT * FROM reviews WHERE id=? AND resolved=0', (args.review_id,)).fetchone()
                    if not row:
                        p.error('Review item not found')
                    answer = json.loads(args.answer_file.read_text())
                    if type(answer) not in (str, int, float, bool) or answer == '':
                        p.error('Answer must be a nonempty JSON scalar')
                    key = digest(json.dumps([row['job_id'], row['question'], profile['version']], ensure_ascii=False).encode())
                    store.cache('verified:' + key, dict(answer=answer, verification_status='user_verified', profile_version=profile['version'], updated_at=now()))
                    with store.db:
                        store.db.execute('UPDATE reviews SET resolved=1 WHERE id=?', (args.review_id,))
                    print('Verified answer saved for exact job/question/profile. Resume using --resume-review.')
                else:
                    report(store, store.rows(), directory, 'history')
            finally:
                store.close()
            return 0
        if args.mode == 'full':
            from job_pipeline.applications.sessions import full
            summary = full(root, args.agent_dir.resolve(), profile, config, args.limit, args.headed, args.resume_run)
            return 1 if summary['failed_applications'] or summary['resume_generation_failures'] else 0
        def run():
            return process(root, args.agent_dir.resolve(), load_profile(args.profile), config, mode='dry-run' if args.mode == 'full' else args.mode,
                           job_id=args.job_id, company=args.company, limit=args.limit, headed=args.headed,
                           resume_review=args.resume_review, only_ready=args.only_completed_resumes)
        if args.mode == 'watch':
            import time
            config['DRY_RUN'] = True  # Unattended integrated polling cannot turn on submissions.
            try:
                while True:
                    if run().get('stopped'):
                        return 0
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                return 0
        summary = run()
        return 1 if summary['counts'].get('FAILED') else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        print('Pipeline 3 stopped:', type(error).__name__, file=sys.stderr)
        raise SystemExit(1)

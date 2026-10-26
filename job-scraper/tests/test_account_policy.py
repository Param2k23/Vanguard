"""Company policy and encrypted-account automation; network tests use loopback only."""
import csv
import html
import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import generate_resumes
from job_pipeline.company_policy import CompanyPolicy, classify_scraped_jobs
from job_pipeline.applications.accounts import AccountManager, account_identity, legacy_password, random_password
from job_pipeline.applications.identity import CONFIRMED_EMAIL
from job_pipeline.applications.runner import process
from job_pipeline.applications.storage import Store, digest, save_json
from job_pipeline.applications.vault import CredentialVault, VaultError
from test_applications import ROOT, setup_data

MASTER = 'Synthetic test vault passphrase 4827'
SUFFIX = 'Fixture_73'


class PolicyVaultTests(unittest.TestCase):
    def test_company_cases_and_no_substring_matching(self):
        policy = CompanyPolicy.load()
        for name in ['Amazon','Amazon.com','Amazon Web Services','Google','Google LLC','Microsoft','Adobe','NVIDIA','Cisco','Bloomberg','Bloomberg L.P.','Freddie Mac','FredieMac','FreddieMac','Federal Home Loan Mortgage Corporation','Arch Group','Arch Mortgage Insurance']:
            self.assertEqual(policy.evaluate(name)['review_reason'],'REFERRAL_PENDING',name)
        for name in ['TikTok','TikTok Inc.','IBM','International Business Machines Corporation']:
            self.assertEqual(policy.evaluate(name)['company_policy'],'EXCLUDED',name)
        for name in ['Amazonian Labs','IBM Research Partners Independent','ByteDance','Google Cloud Consulting Partners','Acme TikTok Tools','Normal Employer']:
            self.assertEqual(policy.evaluate(name)['company_policy'],'AUTO_APPLY',name)
        for name in ['AWS','Alphabet','Arch MI']:
            self.assertEqual(policy.evaluate(name)['company_policy'],'IDENTITY_REVIEW')
            self.assertEqual(policy.evaluate(name,identity_verified=True)['company_policy'],'REFERRAL_REQUIRED')

    def test_exclusion_wins_and_verified_alias_config(self):
        config=json.loads(json.dumps(CompanyPolicy.load().config))
        config['excluded_companies'].extend(['Amazon','Amazon Web Services'])
        config['matching_rules']['verified_employer_jobs']={'j':'Amazon'}
        policy=CompanyPolicy(config)
        self.assertEqual(policy.evaluate('Amazon')['company_policy'],'EXCLUDED')
        self.assertEqual(policy.evaluate('Amazon Web Services')['company_policy'],'EXCLUDED')
        self.assertEqual(policy.evaluate('AWS','j')['company_policy'],'EXCLUDED')

    def test_vault_round_trip_wrong_key_and_no_plaintext(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'vault.json'
            vault=CredentialVault(path,MASTER);vault.initialize()
            password=random_password()
            vault.put('portal:fixture',dict(password=password,verified=True))
            self.assertEqual(CredentialVault(path,MASTER).get('portal:fixture')['password'],password)
            self.assertNotIn(password,path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777,0o600)
            with self.assertRaises(VaultError):CredentialVault(path,'Another synthetic passphrase').get('portal:fixture')
            path.write_text('corrupted')
            with self.assertRaises(VaultError):vault.get('portal:fixture')

    def test_password_generation_and_scope(self):
        self.assertEqual(legacy_password('7 ! Example Company',SUFFIX),'E'+SUFFIX)
        with self.assertRaises(VaultError):legacy_password('123',SUFFIX)
        passwords={random_password(24,32) for _ in range(50)}
        self.assertEqual(len(passwords),50)
        for value in passwords:
            self.assertTrue(24<=len(value)<=32)
            self.assertTrue(any(x.isupper() for x in value));self.assertTrue(any(x.islower() for x in value))
            self.assertTrue(any(x.isdigit() for x in value));self.assertTrue(any(not x.isalnum() for x in value))
        p=dict(origin='https://ats.example',tenant='a')
        self.assertNotEqual(account_identity('Acme',p),account_identity('Other Employer',p))
        self.assertNotEqual(account_identity('Acme',p),account_identity('Acme',dict(p,tenant='b')))

    def test_policy_precedes_resume_vault_and_browser_and_preserves_history(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);agent,job,pdf,profile=setup_data(root)
            for company,status,reason in [('Amazon','NEEDS_REVIEW','REFERRAL_PENDING'),('IBM','SKIPPED','USER_EXCLUDED_COMPANY')]:
                job['company']=company
                with (root/'data/jobs.csv').open('w',newline='') as stream:
                    writer=csv.DictWriter(stream,fieldnames=list(job));writer.writeheader();writer.writerow(job)
                config=json.loads((ROOT/'application_config.json').read_text())
                with patch('job_pipeline.applications.runner.AccountManager',side_effect=AssertionError('Policy must stop before auth')):
                    summary=process(root,agent,profile,config,mode='apply')
                row=summary['applications'][0]
                self.assertEqual(row['status'],status);self.assertEqual(row['review_reason'],reason)
            store=Store(root/'data/applications')
            store.update('one',status='APPLIED',confirmation_evidence='Verified previous application',submitted_at='2026-10-21T00:00:00+00:00',attempt_count=3)
            store.close()
            summary=process(root,agent,profile,config)
            self.assertEqual(summary['applications'][0]['status'],'APPLIED')
            self.assertEqual(summary['applications'][0]['attempt_count'],3)
            with (root/'data/applications/job_applications.csv').open() as stream:
                row=next(csv.DictReader(stream));self.assertEqual(row['company_policy'],'EXCLUDED');self.assertEqual(row['status'],'APPLIED')

    def test_resume_selection_keeps_referrals_and_skips_excluded(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);agent,job,pdf,profile=setup_data(root)
            for name in ('AGENTS.md','base/system_prompt.md','scripts/new_job.py','scripts/verify_resume.py'):
                path=agent/name;path.parent.mkdir(parents=True,exist_ok=True);path.touch()
            rows=[dict(job,id='referral',company='Amazon'),dict(job,id='excluded',company='IBM')]
            with (root/'data/jobs.csv').open('w',newline='') as stream:
                writer=csv.DictWriter(stream,fieldnames=list(job));writer.writeheader();writer.writerows(rows)
            output=io.StringIO()
            with patch.object(generate_resumes,'ROOT',root),patch('sys.argv',['generate_resumes.py','--agent-dir',str(agent),'--dry-run']),redirect_stdout(output):
                self.assertEqual(generate_resumes.main(),0)
            self.assertIn('1 pending resume',output.getvalue());self.assertIn('referral:',output.getvalue())
            self.assertNotIn('excluded: IBM',output.getvalue())
            classify_scraped_jobs(root,rows)
            classifications=json.loads((root/'data/company_policy_classifications.json').read_text())
            self.assertEqual(classifications['referral']['company_policy'],'REFERRAL_REQUIRED')
            self.assertEqual(classifications['excluded']['company_policy'],'EXCLUDED')


class AuthFixture(BaseHTTPRequestHandler):
    scenario='new'
    users={}
    auth_posts=[]
    applications=[]
    def log_message(self,*args): pass
    def respond(self,body,status=200,cookie=None):
        self.send_response(status);self.send_header('Content-Type','text/html')
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers();self.wfile.write(body.encode())
    def redirect(self,target,cookie=None):
        self.send_response(303);self.send_header('Location',target)
        if cookie:self.send_header('Set-Cookie',cookie)
        self.end_headers()
    def auth_html(self,tenant,registration,error=''):
        minimum=24 if self.scenario=='strict' else 8
        heading='Create account' if registration else 'Sign in'
        second='<label>Confirm password<input type="password" required autocomplete="new-password"></label>' if registration else ''
        existing='<p>Email already registered. Please sign in.</p>' if self.scenario=='existing' and registration else ''
        challenge='<div class="g-recaptcha"></div>' if self.scenario=='captcha' else ''
        return f'''<h1>{heading}</h1><p>Example Company</p>{existing}{challenge}
        <form method="post" action="/{tenant}/{'register' if registration else 'login'}">
        <label>Email address<input name="email" type="email" required></label>
        <label>Password<input name="password" type="password" minlength="{minimum}" maxlength="64" required></label>
        {second}<button type="submit">{heading}</button></form><div role="alert">{html.escape(error)}</div>'''
    def do_GET(self):
        parts=urlsplit(self.path).path.strip('/').split('/');tenant=parts[0];action=parts[1] if len(parts)>1 else ''
        cookies=SimpleCookie(self.headers.get('Cookie',''))
        if action=='jobs':
            if f'session_{tenant}' not in cookies:
                self.redirect(f'/{tenant}/'+('login' if self.scenario in ('login','mfa','failed_login') else 'register'));return
            body=(ROOT/'tests/fixtures/application.html').read_text()
            self.respond(f'<div data-authenticated="true" data-applicant-email="{CONFIRMED_EMAIL}">Signed in</div>'+body);return
        self.respond(self.auth_html(tenant,action=='register'))
    def do_POST(self):
        body=self.rfile.read(int(self.headers['Content-Length']))
        if self.path.startswith('/submit'):
            self.applications.append(body)
            if b'Example.pdf' in body and CONFIRMED_EMAIL.encode() in body:
                self.respond('Application submitted. Confirmation ID: LOCAL-AUTH-42')
            else:self.respond('Missing upload or contact',422)
            return
        parts=self.path.strip('/').split('/');tenant,action=parts[:2]
        values=parse_qs(body.decode());email=values.get('email',[''])[0];password=values.get('password',[''])[0]
        self.auth_posts.append((tenant,action,email,password))
        if action=='register':
            if self.scenario=='registration_failure':
                self.respond(self.auth_html(tenant,True,'Registration temporarily unavailable'),422);return
            if (tenant,email) in self.users:
                self.respond(self.auth_html(tenant,True,'An account with this email already exists'),409);return
            if self.scenario=='fallback' and len(password)<24:
                self.respond(self.auth_html(tenant,True,'Password must contain at least 24 characters'),422);return
            self.users[(tenant,email)]=password
            if self.scenario=='verification':
                self.respond('<h1>Verify your email</h1><p>Check your inbox for a verification link</p>');return
        elif self.users.get((tenant,email))!=password:
            self.respond(self.auth_html(tenant,False,'Invalid email or password'),401);return
        if self.scenario=='mfa':
            self.respond('<h1>Verification code</h1><input autocomplete="one-time-code" required>');return
        self.redirect(f'/{tenant}/jobs/123',cookie=f'session_{tenant}=yes; Path=/; HttpOnly; SameSite=Lax')


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS')=='1','Set RUN_BROWSER_TESTS=1 for local account workflow tests')
class AccountBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),AuthFixture)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin=f'http://127.0.0.1:{cls.server.server_port}'
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.agent,self.job,self.pdf,self.profile=setup_data(self.root,self.origin+'/tenant-a/jobs/123')
        self.context=self.browser.new_context()
        self.config=json.loads((ROOT/'application_config.json').read_text())
        self.portal=dict(company='Example Company',origin=self.origin,tenant='tenant-a',login_url='/tenant-a/login',registration_url='/tenant-a/register',authenticated_selector='[data-authenticated="true"]')
        self.config.update(DRY_RUN=False,AUTO_SUBMIT_ENABLED=True,ACCOUNT_CREATION_DRY_RUN=False,APPROVED_FORM_ORIGINS=[self.origin],APPROVED_SUBMIT_ORIGINS=[self.origin],ACCOUNT_PORTALS=[self.portal],AUTH_RESULT_TIMEOUT_MS=1500)
        self.vault=CredentialVault(self.root/'data/credentials/vault.json',MASTER);self.vault.initialize()
        save_json(self.root/'account_secrets.json',dict(LEGACY_PASSWORD_SUFFIX=SUFFIX))
        self.account_id=account_identity('Example Company',self.portal)
        self.store=Store(self.root/'data/applications');self.store.update('one',status='IN_PROGRESS')
        key=digest(json.dumps(['one','Describe your experience with Python',self.profile['version']],ensure_ascii=False).encode())
        self.store.cache('verified:'+key,dict(answer='I built a Python service for Fixture Labs.',verification_status='user_verified'))
        self.store.close()
        AuthFixture.scenario='new';AuthFixture.users={};AuthFixture.auth_posts=[];AuthFixture.applications=[]
        self.env=patch.dict(os.environ,JOB_PORTAL_VAULT_PASSPHRASE=MASTER);self.env.start()
    def tearDown(self):
        self.env.stop();self.context.close();self.temp.cleanup()
    def run_job(self,**kwargs):
        return process(self.root,self.agent,self.profile,self.config,mode='apply',context=self.context,**kwargs)
    def account(self):
        store=Store(self.root/'data/applications')
        try:return store.account(self.account_id)
        finally:store.close()
    def save_existing(self,password,**extra):
        reference='portal:'+self.account_id
        self.vault.put(reference,dict(password=password,verified=True))
        store=Store(self.root/'data/applications');store.update_account(self.account_id,credential_reference=reference,account_status='EXISTING_ACCOUNT',**extra);store.close()
    def test_new_account_returns_to_application_uploads_and_confirms(self):
        summary=self.run_job()
        self.assertEqual(summary['counts'],{'APPLIED':1},summary)
        self.assertEqual(summary['account_management']['new_accounts_created'],1)
        self.assertEqual(len(AuthFixture.auth_posts),1);self.assertEqual(len(AuthFixture.applications),1)
        account=self.account();self.assertEqual(account['account_status'],'ACCOUNT_CREATED')
        self.assertEqual(account['password_type'],'LEGACY_PATTERN')
        password=self.vault.get(account['credential_reference'])['password']
        self.assertEqual(password,'E'+SUFFIX)
        for path in (self.root/'data/applications').rglob('*'):
            if path.is_file() and path.suffix in ('.json','.csv','.md'):
                self.assertNotIn(password,path.read_text())
        self.run_job();self.assertEqual(len(AuthFixture.auth_posts),1);self.assertEqual(len(AuthFixture.applications),1)
    def test_stored_credentials_login(self):
        AuthFixture.scenario='login';password='Saved-Fake-Password-53!'
        AuthFixture.users[('tenant-a',CONFIRMED_EMAIL)]=password;self.save_existing(password)
        summary=self.run_job();self.assertEqual(summary['counts'],{'APPLIED':1},summary)
        self.assertEqual(self.account()['password_type'],'EXISTING_SAVED')
        self.assertEqual(self.account()['account_status'],'EXISTING_ACCOUNT')
        self.assertEqual(summary['account_management']['existing_accounts_accessed'],1)
    def test_existing_account_detection_uses_legacy_once(self):
        AuthFixture.scenario='existing';AuthFixture.users[('tenant-a',CONFIRMED_EMAIL)]='E'+SUFFIX
        summary=self.run_job();self.assertEqual(summary['counts'],{'APPLIED':1},summary)
        self.assertEqual([r[1] for r in AuthFixture.auth_posts],['login'])
        self.assertTrue(self.account()['legacy_login_attempted'])
    def test_existing_account_discovered_after_registration(self):
        AuthFixture.users[('tenant-a',CONFIRMED_EMAIL)]='E'+SUFFIX
        summary=self.run_job();self.assertEqual(summary['counts'],{'APPLIED':1},summary)
        self.assertEqual([r[1] for r in AuthFixture.auth_posts],['register','login'])
        self.assertFalse(summary['applications'][0]['account_created'])
    def test_invalid_legacy_password_is_not_guessed_again(self):
        AuthFixture.scenario='failed_login';AuthFixture.users[('tenant-a',CONFIRMED_EMAIL)]='A-Different-Fake-Password-6!'
        summary=self.run_job();self.assertEqual(summary['counts'],{'NEEDS_REVIEW':1},summary)
        self.assertEqual(self.account()['account_status'],'LOGIN_FAILED')
        self.run_job(resume_review=True);self.assertEqual(len(AuthFixture.auth_posts),1)
        self.assertEqual(AuthFixture.applications,[])
    def test_client_password_policy_generates_strong_fallback(self):
        AuthFixture.scenario='strict'
        summary=self.run_job();self.assertEqual(summary['counts'],{'APPLIED':1},summary)
        account=self.account();self.assertEqual(account['password_type'],'GENERATED')
        self.assertGreaterEqual(len(self.vault.get(account['credential_reference'])['password']),24)
    def test_server_policy_rejection_has_one_generated_fallback(self):
        AuthFixture.scenario='fallback'
        summary=self.run_job();self.assertEqual(summary['counts'],{'APPLIED':1},summary)
        self.assertEqual(len(AuthFixture.auth_posts),2)
        self.assertEqual(self.account()['password_type'],'GENERATED')
    def test_verification_and_mfa_stop_application(self):
        for scenario in ('verification','mfa'):
            with self.subTest(scenario=scenario):
                AuthFixture.scenario=scenario
                AuthFixture.users[('tenant-a',CONFIRMED_EMAIL)]='E'+SUFFIX
                if scenario=='verification':AuthFixture.users.clear()
                store=Store(self.root/'data/applications')
                prior=store.account(self.account_id)
                if prior:store.update_account(self.account_id,auth_blocked=False,account_status='NEEDS_REVIEW',legacy_login_attempted=False)
                store.close()
                summary=self.run_job(resume_review=True)
                self.assertEqual(summary['counts'],{'NEEDS_REVIEW':1},summary)
                self.assertEqual(self.account()['account_status'],'VERIFICATION_REQUIRED')
                self.assertEqual(AuthFixture.applications,[])
    def test_captcha_and_locked_vault_never_post_credentials(self):
        AuthFixture.scenario='captcha'
        summary=self.run_job();self.assertEqual(summary['counts'],{'NEEDS_REVIEW':1},summary)
        self.assertEqual(AuthFixture.auth_posts,[])
        AuthFixture.scenario='new'
        store=Store(self.root/'data/applications');store.update_account(self.account_id,auth_blocked=False,account_status='NEEDS_REVIEW');store.close()
        with patch.dict(os.environ,JOB_PORTAL_VAULT_PASSPHRASE='Incorrect synthetic master passphrase'):
            summary=self.run_job(resume_review=True)
        self.assertIn('VAULT',self.account()['last_error']);self.assertEqual(AuthFixture.auth_posts,[])
    def test_account_creation_dry_run_does_not_register(self):
        self.config['ACCOUNT_CREATION_DRY_RUN']=True
        summary=self.run_job();self.assertEqual(summary['counts'],{'NEEDS_REVIEW':1},summary)
        self.assertEqual(AuthFixture.auth_posts,[]);self.assertFalse(self.account()['created_at'])
    def test_registration_failure_does_not_become_application_success(self):
        AuthFixture.scenario='registration_failure'
        summary=self.run_job();self.assertEqual(summary['counts'],{'NEEDS_REVIEW':1},summary)
        self.assertEqual(self.account()['account_status'],'ACCOUNT_CREATION_FAILED')
        self.assertEqual(AuthFixture.applications,[])
    def test_session_reuse_does_not_create_second_account(self):
        self.run_job()
        page=self.context.new_page();page.goto(self.job['source_url'])
        store=Store(self.root/'data/applications')
        manager=AccountManager(self.root,self.config,self.profile,store,self.job,vault=self.vault)
        manager.ensure_authenticated(page)
        self.assertEqual(store.account(self.account_id)['account_status'],'LOGGED_IN')
        self.assertEqual(len(AuthFixture.auth_posts),1)
        store.close();page.close()
    def test_unapproved_or_unrelated_tenant_cannot_receive_credentials(self):
        self.config['ACCOUNT_PORTALS']=[dict(self.portal,company='Unrelated Employer')]
        summary=self.run_job();self.assertEqual(summary['counts'],{'NEEDS_REVIEW':1},summary)
        self.assertEqual(AuthFixture.auth_posts,[])
    def test_pending_authentication_after_crash_is_not_repeated(self):
        reference='portal:'+self.account_id
        self.vault.put(reference,dict(password='Synthetic-Pending-Password-8!',verified=False))
        store=Store(self.root/'data/applications');store.update_account(self.account_id,credential_reference=reference,auth_blocked=True,account_status='NEEDS_REVIEW');store.close()
        summary=self.run_job(resume_review=True);self.assertEqual(summary['counts'],{'NEEDS_REVIEW':1},summary)
        self.assertEqual(AuthFixture.auth_posts,[])

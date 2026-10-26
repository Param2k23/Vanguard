"""Deterministic authentication state-machine unit tests, NOT browser/E2E evidence.

The real loopback HTTP + Chromium fixtures are in test_account_policy.py.
This small DOM double exercises decisions when the sandbox forbids browser startup.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path
from bs4 import BeautifulSoup
from job_pipeline.applications.accounts import AccountManager, account_identity
from job_pipeline.applications.browser import Blocked
from job_pipeline.applications.identity import CONFIRMED_EMAIL
from job_pipeline.applications.storage import Store, save_json
from job_pipeline.applications.vault import CredentialVault
from test_account_policy import MASTER, SUFFIX
from test_applications import ROOT, setup_data


class Controls:
    def __init__(self,page,nodes):self.page,self.nodes=page,list(nodes)
    def count(self):return len(self.nodes)
    @property
    def first(self):return Controls(self.page,self.nodes[:1])
    def all(self):return [Controls(self.page,[n]) for n in self.nodes]
    def locator(self,selector):return Controls(self.page,self.nodes[0].select(selector.replace(':visible','')))
    def get_attribute(self,name):return self.nodes[0].get(name)
    def inner_text(self):return self.nodes[0].get_text(' ',strip=True)
    def all_inner_texts(self):return [n.get_text(' ',strip=True) for n in self.nodes]
    def is_visible(self):return bool(self.nodes)
    def is_checked(self):return 'checked' in self.nodes[0].attrs
    def input_value(self):return self.nodes[0].get('value','')
    def fill(self,value):self.nodes[0]['value']=value
    def valid(self,node):
        value=node.get('value','')
        if 'required' in node.attrs and not value:return False
        if len(value)<int(node.get('minlength',0)) or len(value)>int(node.get('maxlength',10000)):return False
        return True
    def evaluate(self,script):
        if 'aria-label' in script:
            node=self.nodes[0];label=node.find_parent('label')
            return node.get('aria-label') or (label.get_text(' ',strip=True) if label else node.get('name',''))
        if 'f.checkValidity' in script:return all(self.valid(n) for n in self.nodes[0].select('input'))
        if 'e.checkValidity' in script:return self.valid(self.nodes[0])
        raise AssertionError('Unexpected DOM script in unit test')
    def evaluate_all(self,script):
        if "n.value=''" in script:
            for node in self.nodes:node['value']=''
        else:raise AssertionError('Unexpected multi-element script')
    def click(self,**kwargs):self.page.submit()


class HtmlAuthDouble:
    def __init__(self,scenario='new',saved_password=None):
        self.scenario=scenario;self.saved_password=saved_password;self.authenticated=False;self.posts=[]
        self.url='http://localhost/tenant/jobs/123';self.goto(self.url)
    def render(self,content):self.soup=BeautifulSoup('<html><body>'+content+'</body></html>','html.parser')
    def form(self,register,error=''):
        self.registration=register
        minimum=24 if self.scenario=='strict' else 8
        existing='<p>Account already exists</p>' if self.scenario=='existing' and register else ''
        confirm='<label>Confirm password<input type="password" required></label>' if register else ''
        captcha='<div class="g-recaptcha"></div>' if self.scenario=='captcha' else ''
        self.render(f'''{existing}{captcha}<form method="post" action="/{'register' if register else 'login'}">
        <label>Email address<input type="email" name="email" required></label>
        <label>Password<input type="password" name="password" minlength="{minimum}" required></label>
        {confirm}<button>{'Create account' if register else 'Sign in'}</button></form><div role="alert">{error}</div>''')
    def goto(self,url,**kwargs):
        self.url=url
        if '/jobs/' in url:
            if self.authenticated:self.render(f'<div data-authenticated="true" data-applicant-email="{CONFIRMED_EMAIL}">Signed in</div><h1>Software Engineer</h1><input type="file">')
            else:
                register=self.scenario not in ('login','failure','mfa')
                self.url='http://localhost/tenant/'+('register' if register else 'login')
                self.form(register)
        else:self.form(url.endswith('register'))
    def locator(self,selector):return Controls(self,self.soup.select(selector.replace(':visible','')))
    def get_by_role(self,role,name):
        tag={'button':'button','link':'a'}[role]
        return Controls(self,[n for n in self.soup.select(tag) if name.search(n.get_text(' ',strip=True))])
    def wait_for_timeout(self,milliseconds):pass
    def submit(self):
        email=self.soup.select_one('input[name=email]').get('value','')
        password=self.soup.select_one('input[name=password]').get('value','')
        self.posts.append(dict(registration=self.registration,email=email,password=password))
        if self.registration:
            if self.saved_password is not None:
                self.form(True,'An account with this email already exists');return
            if self.scenario=='fallback' and len(password)<24:
                self.form(True,'Password must have at least 24 characters');return
            if self.scenario=='registration_failure':self.form(True,'Registration unavailable');return
            self.saved_password=password
            if self.scenario=='verification':self.render('<p>Verify your email</p>');return
        elif password != self.saved_password:
            self.form(False,'Invalid credentials');return
        if self.scenario=='mfa':self.render('<input autocomplete="one-time-code">');return
        self.authenticated=True;self.goto('http://localhost/tenant/jobs/123')


class AccountStateUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.agent,self.job,self.pdf,self.profile=setup_data(self.root,'http://localhost/tenant/jobs/123')
        self.config=json.loads((ROOT/'application_config.json').read_text())
        self.portal=dict(company='Example Company',origin='http://localhost',tenant='tenant',login_url='/tenant/login',registration_url='/tenant/register',authenticated_selector='[data-authenticated=true]')
        self.config.update(DRY_RUN=False,ACCOUNT_CREATION_DRY_RUN=False,ACCOUNT_PORTALS=[self.portal],APPROVED_FORM_ORIGINS=['http://localhost'])
        self.store=Store(self.root/'data/applications');self.store.update('one',status='IN_PROGRESS')
        self.vault=CredentialVault(self.root/'data/credentials/vault.json',MASTER);self.vault.initialize()
        save_json(self.root/'account_secrets.json',dict(LEGACY_PASSWORD_SUFFIX=SUFFIX))
        self.account_id=account_identity('Example Company',self.portal)
    def tearDown(self):self.store.close();self.temp.cleanup()
    def manager(self):return AccountManager(self.root,self.config,self.profile,self.store,self.job,vault=self.vault)
    def test_direct_application_does_not_require_vault_or_create_account_record(self):
        page=HtmlAuthDouble()
        page.render('<h1>Software Engineer</h1><label>Resume<input type="file"></label>')
        self.vault.path.unlink()
        self.manager().ensure_authenticated(page)
        self.assertFalse(self.store.get('one')['login_required'])
        self.assertEqual(self.store.accounts(),[])
        self.assertEqual(page.posts,[])

    def test_new_registration_and_session_reuse_state(self):
        page=HtmlAuthDouble();self.manager().ensure_authenticated(page)
        account=self.store.account(self.account_id)
        self.assertEqual(account['account_status'],'ACCOUNT_CREATED')
        self.assertEqual(page.posts[0]['email'],CONFIRMED_EMAIL)
        self.assertTrue(self.vault.get(account['credential_reference'])['verified'])
        self.assertTrue(page.url.endswith('/jobs/123'))
        self.assertNotEqual(self.store.get('one')['status'],'APPLIED')
        self.manager().ensure_authenticated(page)
        self.assertEqual(len(page.posts),1);self.assertEqual(self.store.account(self.account_id)['account_status'],'LOGGED_IN')
    def test_legacy_existing_account_and_no_duplicate_registration(self):
        page=HtmlAuthDouble('existing','E'+SUFFIX);self.manager().ensure_authenticated(page)
        self.assertEqual(len(page.posts),1);self.assertFalse(page.posts[0]['registration'])
        self.assertEqual(self.store.account(self.account_id)['account_status'],'EXISTING_ACCOUNT')
    def test_existing_detected_after_registration_goes_to_login(self):
        page=HtmlAuthDouble('new','E'+SUFFIX);self.manager().ensure_authenticated(page)
        self.assertEqual([p['registration'] for p in page.posts],[True,False])
        self.assertFalse(self.store.get('one')['account_created'])
    def test_saved_credentials_take_priority_over_legacy(self):
        password='Synthetic-Saved-Password-3!';reference='portal:'+self.account_id
        self.vault.put(reference,dict(password=password,verified=True))
        self.store.update_account(self.account_id,credential_reference=reference,account_status='EXISTING_ACCOUNT')
        page=HtmlAuthDouble('login',password);self.manager().ensure_authenticated(page)
        self.assertEqual(page.posts[0]['password'],password)
        self.assertEqual(self.store.account(self.account_id)['password_type'],'EXISTING_SAVED')
    def test_failed_login_is_not_repeated_after_restart(self):
        page=HtmlAuthDouble('failure','Synthetic-Different-Password-5!')
        with self.assertRaises(Blocked):self.manager().ensure_authenticated(page)
        self.assertEqual(self.store.account(self.account_id)['account_status'],'LOGIN_FAILED')
        with self.assertRaises(Blocked):self.manager().ensure_authenticated(page)
        self.assertEqual(len(page.posts),1)
    def test_client_minimum_and_server_rejection_fallback(self):
        for scenario,posts in [('strict',1),('fallback',2)]:
            with self.subTest(scenario=scenario):
                self.store.db.execute('DELETE FROM portal_accounts');self.store.db.commit()
                # Start with an empty encrypted vault for each separate account scenario.
                self.vault.path.unlink();self.vault.initialize()
                page=HtmlAuthDouble(scenario);self.manager().ensure_authenticated(page)
                self.assertEqual(len(page.posts),posts)
                self.assertGreaterEqual(len(page.posts[-1]['password']),24)
                self.assertEqual(self.store.account(self.account_id)['password_type'],'GENERATED')
    def test_verification_and_captcha_stop_without_application_success(self):
        for scenario in ['verification','captcha','mfa']:
            with self.subTest(scenario=scenario):
                self.store.db.execute('DELETE FROM portal_accounts');self.store.db.commit()
                self.vault.path.unlink();self.vault.initialize()
                page=HtmlAuthDouble(scenario,'E'+SUFFIX if scenario=='mfa' else None)
                with self.assertRaises(Blocked):self.manager().ensure_authenticated(page)
                self.assertEqual(self.store.account(self.account_id)['account_status'],'VERIFICATION_REQUIRED')
                self.assertNotEqual(self.store.get('one')['status'],'APPLIED')
    def test_dry_run_disabled_creation_and_unsupported_password_policy(self):
        self.config['ACCOUNT_CREATION_DRY_RUN']=True
        page=HtmlAuthDouble()
        with self.assertRaises(Blocked):self.manager().ensure_authenticated(page)
        self.assertEqual(page.posts,[])
        self.assertFalse(any(n.get('value') for n in page.soup.select('input[type=password]')))
    def test_registration_failure_is_not_success(self):
        page=HtmlAuthDouble('registration_failure')
        with self.assertRaises(Blocked):self.manager().ensure_authenticated(page)
        self.assertEqual(self.store.account(self.account_id)['account_status'],'ACCOUNT_CREATION_FAILED')
        self.assertEqual(len(page.posts),1)
    def test_vault_failure_and_pending_auth_crash_do_not_retry(self):
        page=HtmlAuthDouble()
        self.store.update_account(self.account_id,auth_blocked=True,account_status='NEEDS_REVIEW')
        with self.assertRaises(Blocked):self.manager().ensure_authenticated(page)
        self.assertEqual(page.posts,[])
        self.store.update_account(self.account_id,auth_blocked=False)
        self.vault.path.write_text('corrupt')
        with self.assertRaises(Blocked):self.manager().ensure_authenticated(page)
        self.assertIn('VAULT',self.store.account(self.account_id)['last_error'])
    def test_authenticated_session_for_another_email_is_rejected(self):
        page=HtmlAuthDouble()
        page.authenticated=True;page.goto(self.job['source_url'])
        page.soup.select_one('[data-authenticated]')['data-applicant-email']='other-applicant@example.test'
        with self.assertRaises(Blocked):self.manager().ensure_authenticated(page)
        self.assertEqual(page.posts,[])
        self.assertFalse(self.store.get('one')['login_successful'])

    def test_disallowed_destination_does_not_receive_secret(self):
        page=HtmlAuthDouble()
        page.soup.select_one('form')['action']='http://unrelated.example/collect'
        with self.assertRaises(Blocked):self.manager().ensure_authenticated(page)
        self.assertFalse(any(n.get('value') for n in page.soup.select('input[type=password]')))
        self.assertEqual(page.posts,[])

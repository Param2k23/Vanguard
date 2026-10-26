"""Review authorization tests; external employers are never contacted."""
import csv
import json
import os
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from job_pipeline.applications.review_batches import Batches, prepare, apply_approved, migrate_existing, FIELDS, correct_destination
from job_pipeline.applications.storage import Store, save_json, digest
from job_pipeline.applications.integration import resume_for
from job_pipeline.applications.destinations import scope, within
from test_applications import setup_data, ROOT, FixtureHandler
import test_fresh_sessions as fresh_fixtures
job = fresh_fixtures.job


def edit(root, batch, changes):
    path=root/'data/applications/review_batches'/batch/'application_review.csv'
    with path.open(newline='') as stream:
        rows=list(csv.DictReader(stream))
    for row in rows:
        row.update(changes.get(row['job_id'],{}))
    with path.open('w',newline='') as stream:
        writer=csv.DictWriter(stream,FIELDS);writer.writeheader();writer.writerows(rows)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        self.agent,self.job,self.pdf,self.profile=setup_data(self.root)
        self.config=json.loads((ROOT/'application_config.json').read_text())
        self.batch=migrate_existing(self.root,self.agent,self.profile,self.config,'one')
        self.calls=[]

    def db(self):
        return Batches(self.root)

    def rows(self):
        batches=self.db()
        try:return batches.rows(self.batch)
        finally:batches.close()

    def processor(self,root,agent,profile,config,**kwargs):
        self.calls.append(kwargs['job_ids'])
        self.assertEqual(kwargs['job_ids'],{'one'})
        self.assertEqual(config['REVIEWED_DESTINATION']['job_id'],'one')
        store=Store(root/'data/applications')
        store.update('one',company=self.job['company'],position=self.job['title'],status='READY_TO_APPLY')
        store.close()
        return {'verified_submissions_this_run':0}

    def apply(self,processor=None):
        return apply_approved(self.root,self.agent,self.profile,self.config,self.batch,processor=processor or self.processor)

    def test_initial_pending_distinct_urls_completed_resume_and_idempotent_migration(self):
        row=self.rows()[0]
        self.assertEqual(row['review_decision'],'PENDING')
        self.assertNotEqual(row['jobright_url'],row['application_url'])
        self.assertEqual(row['resume_path'],str(self.pdf))
        self.assertEqual(migrate_existing(self.root,self.agent,self.profile,self.config,'one'),self.batch)

    def test_pending_rejected_and_empty_do_not_apply(self):
        for decision in ('PENDING','REJECTED',''):
            edit(self.root,self.batch,{'one':{'review_decision':decision}})
            self.apply()
        self.assertEqual(self.calls,[])

    def test_approval_snapshot_hash_and_revisit(self):
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        self.apply()
        batches=self.db()
        record=batches.db.execute('SELECT hash,payload FROM destination_approvals').fetchone()
        self.assertEqual(record[0],digest(json.dumps(json.loads(record[1]),sort_keys=True).encode()))
        self.assertEqual(json.loads(record[1])['destination']['origin'],'https://example.test')
        batches.close()
        self.assertEqual(len(self.calls),1)

    def test_changed_url_company_and_resume_path_invalidate(self):
        for field,value in [('application_url','https://other.test/jobs/123'),('company','Other'),('resume_path','/tmp/wrong.pdf')]:
            with self.subTest(field=field):
                edit(self.root,self.batch,{'one':{'review_decision':'APPROVED',field:value}})
                self.apply()
                self.assertEqual(self.rows()[0]['review_decision'],'NEEDS_CORRECTION')
        self.assertEqual(self.calls,[])

    def test_live_source_identity_change_invalidates(self):
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        self.job['source_url']='https://other.test/jobs/123'
        with (self.root/'data/jobs.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,self.job);w.writeheader();w.writerow(self.job)
        self.apply();self.assertEqual(self.calls,[])
        self.assertEqual(self.rows()[0]['review_decision'],'NEEDS_CORRECTION')

    def test_protected_submissions_not_retried(self):
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        store=Store(self.root/'data/applications')
        store.update('one',status='APPLIED',confirmation_evidence='Application submitted.',submitted_at='2026-01-01T00:00:00+00:00');store.close()
        self.apply();self.assertEqual(self.calls,[])
        self.assertEqual(self.rows()[0]['application_status'],'APPLIED')

    def test_uncertain_not_retried(self):
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        store=Store(self.root/'data/applications');store.update('one',status='SUBMISSION_UNCONFIRMED');store.close()
        self.apply();self.assertEqual(self.calls,[])

    def test_decisions_survive_background_updates(self):
        batches=self.db();row=batches.rows(self.batch)[0]
        row.update(resume_status='WAITING_FOR_RESUME',resume_path='')
        batches.put(row);batches.export(self.batch);batches.close()
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED','review_notes':'Checked myself'}})
        batches=self.db();row=batches.rows(self.batch)[0];row.update(resume_status='READY_FOR_APPLICATION',resume_path=str(self.pdf));batches.put(row)
        self.assertFalse(batches.export(self.batch))
        batches.close()
        self.apply()
        self.assertEqual(self.rows()[0]['review_notes'],'Checked myself')
        self.assertEqual(len(self.calls),1)

    def test_approval_cannot_authorize_url_not_yet_seen_in_csv(self):
        batches=self.db();row=batches.rows(self.batch)[0]
        row['application_url']=''
        batches.put(row);batches.export(self.batch)
        batches.close()
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        batches=self.db();row=batches.rows(self.batch)[0]
        row['application_url']=self.job['source_url']
        batches.put(row)
        self.assertFalse(batches.export(self.batch))
        batches.close()
        self.apply()
        self.assertEqual(self.calls,[])
        self.assertEqual(self.rows()[0]['review_decision'],'NEEDS_CORRECTION')

    def test_import_archives_and_repeatable(self):
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        self.apply();self.apply()
        archives=list((self.root/'data/applications/review_batches'/self.batch/'imports').glob('*.csv'))
        self.assertGreaterEqual(len(archives),2)
        self.assertEqual(self.rows()[0]['review_decision'],'APPROVED')

    def test_separate_reports_and_last_session(self):
        self.apply();self.apply()
        folder=self.root/'data/applications/review_batches'/self.batch/'sessions'
        self.assertEqual(len(list(folder.glob('*.json'))),3)
        report=json.loads((self.root/'data/applications/last_session.json').read_text())
        self.assertEqual(report['verified_submissions'],0)
        self.assertEqual(report['pending'],1)

    def test_interrupt_releases_and_reimports_without_reset(self):
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        def interrupted(*args,**kwargs):raise KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):self.apply(interrupted)
        self.apply();self.assertEqual(len(self.calls),1)

    def test_formula_exports_and_unknown_tenant_scopes(self):
        self.assertFalse(within('https://jobs.lever.co/other/login',scope('https://jobs.lever.co/acme/123')))
        self.assertFalse(within('https://id.other.test/login',scope('https://acme.test/jobs/1')))
        self.assertFalse(within('https://www.myworkdaysite.com/recruiting/other/site/job',scope('https://www.myworkdaysite.com/recruiting/acme/site/job')))
        self.assertFalse(within('https://boards.greenhouse.io/embed/job_app?for=other&token=1',scope('https://boards.greenhouse.io/embed/job_app?for=acme&token=1')))
        batches=self.db();row=batches.rows(self.batch)[0];row['review_notes']=' =HYPERLINK("bad")';batches.put(row);batches.export(self.batch);batches.close()
        raw=(self.root/'data/applications/review_batches'/self.batch/'application_review.csv').read_text()
        self.assertIn("' =HYPERLINK",raw)

    def test_referral_and_exclusion_override_blocked(self):
        for company in ('IBM','TikTok','Amazon','Google','Arch Group'):
            with self.subTest(company=company):
                batches=self.db();row=batches.rows(self.batch)[0]
                row['company']=company;row['_job']['company']=company
                self.job['company']=company
                with (self.root/'data/jobs.csv').open('w',newline='') as f:
                    w=csv.DictWriter(f,self.job);w.writeheader();w.writerow(self.job)
                batches.put(row);batches.export(self.batch);batches.close()
                edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
                self.apply()
        self.assertEqual(self.calls,[])

    def test_partial_approval_then_revisit_and_continue_after_failure(self):
        from job_pipeline.applications.integration import ReadinessError
        selected=[dict(self.job,id=str(i),source_url=f'https://example.test/jobs/{i}') for i in range(3)]
        with (self.root/'data/jobs.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,selected[0]);w.writeheader();w.writerows(selected)
        batches=self.db();self.batch=batches.create(selected)
        for row in batches.rows(self.batch):
            row.update(application_url=row['_job']['source_url'],resume_path=str(self.pdf),_resume_sha='sha',resume_status='READY_FOR_APPLICATION')
            batches.put(row)
        batches.export(self.batch);batches.close()
        edit(self.root,self.batch,{'0':{'review_decision':'APPROVED'},'1':{'review_decision':'APPROVED'}})
        seen=[]
        def processor(root,agent,profile,config,**kw):
            identifier=next(iter(kw['job_ids']));seen.append(identifier)
            if identifier=='0':raise RuntimeError('Controlled failure')
            store=Store(root/'data/applications');store.update(identifier,status='APPLIED',confirmation_evidence='Application submitted.');store.close()
            return {'verified_submissions_this_run':1}
        with patch('job_pipeline.applications.review_batches.resume_for',return_value={'path':str(self.pdf),'sha256':'sha'}):
            self.apply(processor)
            self.assertEqual(seen,['0','1'])
            edit(self.root,self.batch,{'0':{'review_decision':'REJECTED'},'2':{'review_decision':'APPROVED'}})
            self.apply(processor)
        self.assertEqual(seen,['0','1','2'])

    def test_no_confirmation_cannot_be_counted_as_applied(self):
        store=Store(self.root/'data/applications')
        with self.assertRaises(ValueError):store.update('one',status='APPLIED')
        store.close()

    def test_membership_tampering_revokes_approval(self):
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        batches=self.db();batches.import_csv(self.batch);batches.close()
        edit(self.root,self.batch,{'one':{'job_id':'changed'}})
        with self.assertRaises(ValueError):self.apply()
        self.assertEqual(self.rows()[0]['review_decision'],'NEEDS_CORRECTION')

    def test_explicit_correction_publishes_pending_snapshot(self):
        correct_destination(self.root,self.batch,'one','https://example.test/jobs/124',resolver=lambda j,h:(j['source_url'],{}))
        row=self.rows()[0]
        self.assertEqual(row['application_url'],'https://example.test/jobs/124')
        self.assertEqual(row['review_decision'],'PENDING')


class PrepareTests(unittest.TestCase):
    setUp = fresh_fixtures.FreshTests.setUp
    write = fresh_fixtures.FreshTests.write
    scraper = fresh_fixtures.FreshTests.scraper
    # Use existing fresh-ledger fixtures without running inherited full tests here.
    def test_prepare_five_new_csv_before_resumes(self):
        def generate(root,agent,row):
            self.generated.append(row['id'])
            paths=list((root/'data/applications/review_batches').glob('*/application_review.csv'))
            self.assertEqual(len(paths),1)
            self.assertEqual(len(list(csv.DictReader(paths[0].open()))),5)
        artifact={'path':'fixture.pdf','sha256':'fixture-sha'}
        with patch('job_pipeline.applications.review_batches.resume_for', side_effect=[__import__('job_pipeline.applications.integration',fromlist=['ReadinessError']).ReadinessError('WAITING_FOR_RESUME','Missing'),artifact]*5):
            batch=prepare(self.root,self.agent,{},self.config,5,scraper=self.scraper([job(i) for i in range(201,206)]),generator=generate,resolver=lambda j,h:(j['source_url'],{}))
        batches=Batches(self.root)
        self.assertEqual(len(batches.rows(batch)),5)
        self.assertTrue(all(r['resume_status']=='READY_FOR_APPLICATION' and r['review_decision']=='PENDING' for r in batches.rows(batch)))
        self.assertEqual(batches.meta(batch)['status'],'READY_FOR_REVIEW')
        batches.close()
        self.assertEqual(len(self.generated),5)

    def test_resume_failure_is_reported_without_application(self):
        from job_pipeline.applications.integration import ReadinessError
        def failed(*args):raise OSError('Controlled generator failure')
        with patch('job_pipeline.applications.review_batches.resume_for',side_effect=ReadinessError('WAITING_FOR_RESUME','Missing')):
            batch=prepare(self.root,self.agent,{},self.config,5,scraper=self.scraper([job(201)]),generator=failed,resolver=lambda j,h:(j['source_url'],{}))
        batches=Batches(self.root)
        self.assertEqual(batches.rows(batch)[0]['application_status'],'RESUME_FAILED')
        self.assertEqual(batches.meta(batch)['status'],'READY_FOR_REVIEW')
        batches.close()

    def test_resolved_redirect_duplicate_is_not_prepared(self):
        with patch('job_pipeline.applications.review_batches.resume_for') as resume:
            batch=prepare(self.root,self.agent,{},self.config,5,scraper=self.scraper([job(201)]),resolver=lambda j,h:(job(1)['source_url'],{}))
        batches=Batches(self.root)
        self.assertEqual(batches.rows(batch),[])
        batches.close()
        resume.assert_not_called()
        report=json.loads((self.root/'data/applications/last_session.json').read_text())
        self.assertEqual(report['fresh_jobs_found'],0)
        self.assertEqual(report['duplicates_skipped'],1)

    def test_missing_links_excluded_and_referral_retained(self):
        with patch('job_pipeline.applications.review_batches.resume_for',return_value={'path':'fixture.pdf','sha256':'sha'}):
            batch=prepare(self.root,self.agent,{},self.config,5,scraper=self.scraper([job(201,'Google'),job(202,'IBM'),job(203)]),resolver=lambda j,h:('',{}))
        batches=Batches(self.root);rows=batches.rows(batch)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['application_status'],'REFERRAL_REQUIRED')
        self.assertEqual(rows[1]['application_status'],'URL_REVIEW_REQUIRED')
        self.assertTrue(all(not r['application_url'] for r in rows))
        batches.close()


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS')=='1','Set RUN_BROWSER_TESTS=1 for loopback submission tests')
class ReviewedBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        class ReviewHandler(FixtureHandler):
            def do_GET(self):
                if getattr(self.server,'redirect_unknown',False):
                    if self.path=='/unrelated':
                        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(b'<h1>Chief Financial Officer</h1><p>Unrelated Employer</p>')
                    else:
                        self.send_response(302)
                        self.send_header('Location',f'http://localhost:{self.server.server_port}/unrelated')
                        self.end_headers()
                else:
                    super().do_GET()
            def do_POST(self):
                if self.path == '/bounce':
                    self.rfile.read(int(self.headers.get('Content-Length',0)))
                    self.send_response(307)
                    self.send_header('Location',f'http://localhost:{self.server.server_port}/credential-leak')
                    self.end_headers()
                elif self.path == '/credential-leak':
                    self.server.forwarded.append(self.rfile.read(int(self.headers.get('Content-Length',0))))
                    self.send_response(200);self.end_headers()
                else:
                    super().do_POST()
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),ReviewHandler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin=f'http://127.0.0.1:{cls.server.server_port}'
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.agent,self.job,self.pdf,self.profile=setup_data(self.root,self.origin+'/jobs/123')
        self.config=json.loads((ROOT/'application_config.json').read_text())
        self.config.update(DRY_RUN=False,AUTO_SUBMIT_ENABLED=True)
        self.context=self.browser.new_context();FixtureHandler.submissions.clear()
        self.server.redirect_unknown=False
        self.server.forwarded=[]
        store=Store(self.root/'data/applications')
        key=digest(json.dumps(['one','Describe your experience with Python',self.profile['version']],ensure_ascii=False).encode())
        store.cache('verified:'+key,dict(answer='I built a Python service for Fixture Labs.',verification_status='user_verified'));store.close()
        self.batch=migrate_existing(self.root,self.agent,self.profile,self.config,'one')

    def tearDown(self):self.context.close();self.temp.cleanup()

    def run_batch(self):return apply_approved(self.root,self.agent,self.profile,self.config,self.batch,context=self.context)

    def test_reviewed_destination_submits_without_global_portal_permissions(self):
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        self.run_batch()
        self.assertEqual(len(FixtureHandler.submissions),1)
        store=Store(self.root/'data/applications');row=store.get('one');store.close()
        self.assertEqual(row['status'],'APPLIED');self.assertEqual(row['confirmation_id'],'LOCAL-123')
        self.run_batch();self.assertEqual(len(FixtureHandler.submissions),1)

    def test_pending_and_dry_run_do_not_submit(self):
        self.run_batch();self.assertEqual(FixtureHandler.submissions,[])
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        self.config['DRY_RUN']=True
        self.run_batch();self.assertEqual(FixtureHandler.submissions,[])
        store=Store(self.root/'data/applications');row=store.get('one');store.close()
        self.assertEqual(row['status'],'READY_TO_APPLY')

    def test_wrong_company_heading_remains_blocked(self):
        page=self.context.new_page();page.goto(self.origin+'/jobs/123')
        from job_pipeline.applications.browser import NativeFormAdapter, Blocked
        store=Store(self.root/'data/applications')
        adapter=NativeFormAdapter(page,dict(self.config,APPROVED_FORM_ORIGINS=[self.origin]),self.profile,store,dict(self.job,company='Other Employer'),{},self.root/'audit')
        with self.assertRaises(Blocked):adapter.validate_identity()
        store.close();page.close()

    def test_unexpected_auth_redirect_is_specific_review_blocker(self):
        self.server.redirect_unknown=True
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})
        self.run_batch()
        store=Store(self.root/'data/applications');row=store.get('one');store.close()
        self.assertEqual(row['status'],'NEEDS_REVIEW',row)
        self.assertIn('Cannot associate destination',row['review_reason'])
        self.assertEqual(FixtureHandler.submissions,[])

    def test_307_cannot_forward_sensitive_post_to_unapproved_origin(self):
        from job_pipeline.applications.destinations import enforce
        page=self.context.new_page();guard=enforce(page,scope(self.job['source_url']))
        page.goto(self.job['source_url'])
        result=page.evaluate("async()=>{try{await fetch('/bounce',{method:'POST',body:'Synthetic secret'});return 'sent'}catch(e){return 'blocked'}}")
        self.assertEqual(result,'blocked')
        self.assertTrue(guard)
        self.assertEqual(self.server.forwarded,[])
        guard.close();page.close()

    def test_unknown_navigation_origin_blocked_before_data(self):
        from job_pipeline.applications.destinations import enforce
        page=self.context.new_page();blocked=enforce(page,scope(self.job['source_url']))
        with self.assertRaises(Exception):page.goto('http://localhost:'+str(self.server.server_port)+'/login')
        self.assertTrue(blocked);self.assertEqual(FixtureHandler.submissions,[]);page.close()


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS')=='1','Set RUN_BROWSER_TESTS=1 for loopback authentication tests')
class ReviewedAuthBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        from test_account_policy import AuthFixture
        cls.handler=AuthFixture
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),AuthFixture)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin=f'http://127.0.0.1:{cls.server.server_port}'
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join()

    def setUp(self):
        from job_pipeline.applications.vault import CredentialVault
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.agent,self.job,self.pdf,self.profile=setup_data(self.root,self.origin+'/tenant-a/jobs/123')
        self.config=json.loads((ROOT/'application_config.json').read_text())
        self.config.update(DRY_RUN=False,AUTO_SUBMIT_ENABLED=True,ACCOUNT_CREATION_DRY_RUN=False,AUTH_RESULT_TIMEOUT_MS=1000)
        self.context=self.browser.new_context()
        self.handler.users={};self.handler.auth_posts=[];self.handler.applications=[];self.handler.scenario='new'
        CredentialVault(self.root/'data/credentials/vault.json','Synthetic batch master passphrase').initialize()
        save_json(self.root/'account_secrets.json',{'LEGACY_PASSWORD_SUFFIX':'Fixture_73'})
        self.env=patch.dict(os.environ,JOB_PORTAL_VAULT_PASSPHRASE='Synthetic batch master passphrase');self.env.start()
        store=Store(self.root/'data/applications')
        key=digest(json.dumps(['one','Describe your experience with Python',self.profile['version']],ensure_ascii=False).encode())
        store.cache('verified:'+key,dict(answer='I built a Python service for Fixture Labs.',verification_status='user_verified'));store.close()
        self.batch=migrate_existing(self.root,self.agent,self.profile,self.config,'one')
        edit(self.root,self.batch,{'one':{'review_decision':'APPROVED'}})

    def tearDown(self):self.env.stop();self.context.close();self.temp.cleanup()

    def run_batch(self):return apply_approved(self.root,self.agent,self.profile,self.config,self.batch,context=self.context)

    def test_reviewed_native_registration_without_portal_mapping(self):
        self.run_batch()
        self.assertEqual(len(self.handler.auth_posts),1)
        self.assertEqual(len(self.handler.applications),1)
        store=Store(self.root/'data/applications');row=store.get('one');store.close()
        self.assertEqual(row['status'],'APPLIED',row)

    def test_reviewed_native_existing_login_without_portal_mapping(self):
        from job_pipeline.applications.identity import CONFIRMED_EMAIL
        self.handler.scenario='login';self.handler.users[('tenant-a',CONFIRMED_EMAIL)]='EFixture_73'
        self.run_batch()
        self.assertEqual([p[1] for p in self.handler.auth_posts],['login'])
        self.assertEqual(len(self.handler.applications),1)

    def test_dry_run_never_authenticates_or_registers(self):
        self.config['DRY_RUN']=True
        self.run_batch()
        self.assertEqual(self.handler.auth_posts,[]);self.assertEqual(self.handler.applications,[])
        store=Store(self.root/'data/applications');row=store.get('one');store.close()
        self.assertEqual(row['authentication_error'],'AUTH_DRY_RUN_NO_LOGIN_OR_REGISTRATION')

    def test_cross_origin_credential_form_action_blocked_before_fill(self):
        from job_pipeline.applications.accounts import AccountManager
        from job_pipeline.applications.browser import Blocked
        page=self.context.new_page();page.goto(self.origin+'/tenant-a/register')
        page.set_content('<form action="https://unrelated.invalid/login"><input type="email"><input type="password"></form>')
        store=Store(self.root/'data/applications')
        config=dict(self.config,REVIEWED_DESTINATION=dict(job_id='one',destination=scope(self.job['source_url'])),APPROVED_FORM_ORIGINS=[self.origin])
        manager=AccountManager(self.root,config,self.profile,store,self.job)
        manager.choose_portal(page)
        with self.assertRaisesRegex(Blocked,'CREDENTIAL_POST_TENANT_MISMATCH'):
            manager.auth_form(page,'Synthetic password not transmitted',False)
        self.assertEqual(page.locator('input[type=password]').input_value(),'')
        self.assertEqual(self.handler.auth_posts,[])
        store.close();page.close()

    def test_mismatched_authenticated_email_blocked(self):
        from job_pipeline.applications.accounts import AccountManager
        from job_pipeline.applications.browser import Blocked
        page=self.context.new_page();page.goto(self.origin+'/tenant-a/register')
        page.set_content('<div data-applicant-email="someone@other.test">Signed in</div>')
        store=Store(self.root/'data/applications')
        config=dict(self.config,REVIEWED_DESTINATION=dict(job_id='one',destination=scope(self.job['source_url'])),APPROVED_FORM_ORIGINS=[self.origin])
        with self.assertRaisesRegex(Blocked,'SESSION_APPLICANT_EMAIL_NOT_VERIFIED'):
            AccountManager(self.root,config,self.profile,store,self.job).ensure_authenticated(page)
        store.close();page.close();self.assertEqual(self.handler.auth_posts,[])

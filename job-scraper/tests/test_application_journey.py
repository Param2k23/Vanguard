"""Controlled navigation, generic forms and approval-reporting regressions."""
import csv
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from unittest.mock import patch
from job_pipeline.applications.journey import ApplicationJourney,title_match
from job_pipeline.applications.review_batches import Batches,migrate_existing,apply_approved
from job_pipeline.applications.storage import Store
from job_pipeline.applications.browser import Blocked,NativeFormAdapter
import test_applications as fixtures
import test_review_batches as review_fixtures

FORM='''<h1>Software Engineer</h1><p>Example Company</p><form id="app">
<label>Full name<input name="full_name" required></label><label>Email<input name="email" type="email" required></label>
<label>Resume<input name="resume" type="file" required></label><button type="submit">Submit application</button></form><h2 id="result"></h2>
<script>app.onsubmit=async e=>{e.preventDefault();const r=await fetch('/submitted',{method:'POST',body:new FormData(app)});result.textContent=await r.text();app.hidden=true}</script>'''

class Handler(BaseHTTPRequestHandler):
    posts=[]
    def log_message(self,*args):pass
    def do_GET(self):
        port=self.server.server_port
        if self.path.startswith('/redirect'):
            self.send_response(302);self.send_header('Location',f'http://localhost:{port}/ats/jobs/123');self.end_headers();return
        if self.path.startswith('/login-redirect'):
            self.send_response(302);self.send_header('Location','/login');self.end_headers();return
        body=FORM
        if self.path.startswith('/button'):
            body=f'<h1>Software Engineer</h1><p>Example Company</p><a target="_blank" href="http://localhost:{port}/ats/jobs/123">Apply now</a>'
        elif self.path.startswith('/login'):
            body='<h1>Sign in</h1><p>Example Company</p><form><label>Email<input type="email"></label><label>Password<input type="password"></label><button>Sign in</button></form>'
        elif self.path.startswith('/modified'):
            body=FORM.replace('Software Engineer','Engineer, Software')
        elif self.path.startswith('/wrong'):
            body=FORM.replace('Software Engineer','Chief Financial Officer').replace('Example Company','Unrelated Company')
        elif self.path.startswith('/steps'):
            body=FORM.replace('<button type="submit">','<button type="button" onclick="this.remove();document.getElementById(\'nextstep\').hidden=false">Next</button><div id="nextstep" hidden><label>Phone<input></label></div><button type="submit">')
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body.encode())
    def do_POST(self):
        body=self.rfile.read(int(self.headers['Content-Length']));self.posts.append(body)
        self.send_response(200);self.send_header('Content-Type','text/plain');self.end_headers();self.wfile.write(b'Application submitted. Confirmation ID: JOURNEY-42')

class TitleTests(unittest.TestCase):
    def test_four_approved_one_pending_report_keeps_halvik_visible(self):
        from job_pipeline.applications.review_batches import session_report
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);b=Batches(root)
            companies=['RFA Engineering','Flexera','Fulgent Genetics','Halvik','Affirm']
            batch=b.create([dict(id=str(i),company=c,title='Software Engineer',source_url='https://example.test/jobs/'+str(i)) for i,c in enumerate(companies)])
            for r in b.rows(batch):
                r.update(review_decision='PENDING' if r['company']=='Affirm' else 'APPROVED',resume_status='RESUME_FAILED' if r['company']=='Affirm' else 'READY_FOR_APPLICATION')
                b.put(r)
            summary=session_report(b,batch,'APPLICATION',b.rows(batch))
            self.assertEqual((summary['approved'],summary['pending'],summary['rejected'],summary['authorized_application_candidates']),(4,1,0,4))
            self.assertEqual(len(summary['approved_application_results']),4)
            self.assertIn('Halvik',[r['company'] for r in summary['approved_application_results']])
            self.assertEqual([r['company'] for r in summary['pending_approval']],['Affirm'])
            self.assertEqual([r['company'] for r in summary['preparation_issues']],['Affirm'])
            b.close()

    def test_reformatted_and_seniority_titles(self):
        self.assertTrue(title_match('Junior Software Engineer — Applied AI','Software Engineer, Applied AI'))
        self.assertTrue(title_match('Software Engineer','Engineer, Software'))
        self.assertFalse(title_match('Software Engineer','Finance Manager'))

@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS')=='1','Controlled browser tests')
class JourneyBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin=f'http://127.0.0.1:{cls.server.server_port}'
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.context=self.browser.new_context();Handler.posts=[]
        self.agent,self.job,self.pdf,self.profile=fixtures.setup_data(self.root,self.origin+'/jobs/123')
        self.config=json.loads((fixtures.ROOT/'application_config.json').read_text());self.config.update(DRY_RUN=True,AUTO_SUBMIT_ENABLED=False)
    def tearDown(self):self.context.close();self.temp.cleanup()
    def batch(self,path):
        self.job['source_url']=self.origin+path
        from job_pipeline.applications.storage import save_json
        save_json(self.agent/'jobs/example/job.json',dict(company=self.job['company'],role=self.job['title'],job_url=self.job['source_url']))
        with (self.root/'data/jobs.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,self.job);w.writeheader();w.writerow(self.job)
        batch=migrate_existing(self.root,self.agent,self.profile,self.config,'one')
        review_fixtures.edit(self.root,batch,{'one':{'review_decision':'APPROVED'}})
        return batch
    def run_job(self,path,live=False):
        batch=self.batch(path)
        if live:self.config.update(DRY_RUN=False,AUTO_SUBMIT_ENABLED=True)
        apply_approved(self.root,self.agent,self.profile,self.config,batch,context=self.context)
        store=Store(self.root/'data/applications');row=store.get('one');store.close()
        return batch,row
    def test_company_to_external_ats_redirect_retains_approval(self):
        batch,row=self.run_job('/redirect/jobs/123')
        self.assertEqual(row['status'],'READY_TO_APPLY',row)
        b=Batches(self.root);self.assertEqual(b.rows(batch)[0]['review_decision'],'APPROVED');b.close();self.assertEqual(Handler.posts,[])
    def test_same_ats_paths_and_session_parameters_preserve_authorization(self):
        batch=self.batch('/jobs/12345');b=Batches(self.root);b.import_csv(batch)
        row=b.rows(batch)[0];approved=b.approval(row,self.job);original=approved['approval_hash'];b.close()
        page=self.context.new_page();store=Store(self.root/'data/applications')
        config=dict(self.config,REVIEWED_DESTINATION=approved)
        journey=ApplicationJourney(page,self.job,config,store);journey.install()
        page.goto(self.job['source_url']);journey.authorize(page)
        page.goto(self.origin+'/application/step2?session=new&tracking=changed')
        page.set_content('<h1>Personal information</h1><label>Full name<input></label>')
        self.assertTrue(journey.authorize(page))
        b=Batches(self.root);self.assertEqual(b.approval(b.rows(batch)[0],self.job)['approval_hash'],original);b.close()
        page.set_content('<h1>Senior Finance Manager</h1><p>Example Company</p>')
        with self.assertRaisesRegex(Blocked,'Position differs'):journey.authorize(page)
        journey.close();store.close();page.close()

    def test_apply_popup_to_external_form(self):
        _,row=self.run_job('/button/jobs/123');self.assertEqual(row['status'],'READY_TO_APPLY',row)
    def test_legitimate_login_redirect_not_generic_url_block(self):
        _,row=self.run_job('/login-redirect/jobs/123');self.assertEqual(row['review_reason'],'AUTH_DRY_RUN_NO_LOGIN_OR_REGISTRATION',row)
    def test_modified_title(self):
        _,row=self.run_job('/modified/jobs/123');self.assertEqual(row['status'],'READY_TO_APPLY',row)
    def test_generic_fallback_on_workday_platform(self):
        with patch('job_pipeline.applications.browser.platform',return_value='workday'):
            _,row=self.run_job('/jobs/123')
        self.assertEqual(row['status'],'READY_TO_APPLY',row)
    def test_multistep_forms(self):
        _,row=self.run_job('/steps/jobs/123');self.assertEqual(row['status'],'READY_TO_APPLY',row)
    def test_submission_confirmation_and_duplicate_protection(self):
        batch,row=self.run_job('/redirect/jobs/123',live=True)
        self.assertEqual(row['status'],'APPLIED',row);self.assertEqual(row['confirmation_id'],'JOURNEY-42')
        apply_approved(self.root,self.agent,self.profile,self.config,batch,context=self.context)
        self.assertEqual(len(Handler.posts),1)
    def test_requisition_history_cannot_repeat_via_new_session_url(self):
        store=Store(self.root/'data/applications')
        store.update('previous',company='Example Company',status='APPLIED',application_url=self.origin+'/jobs/12345',confirmation_evidence='Application submitted.')
        store.close()
        _,row=self.run_job('/jobs/12345?session=new')
        self.assertEqual(row['status'],'ALREADY_APPLIED',row)
        self.assertEqual(Handler.posts,[])

    def test_unrelated_employer_rejected_without_data(self):
        _,row=self.run_job('/wrong/jobs/123');self.assertEqual(row['status'],'NEEDS_REVIEW',row);self.assertEqual(Handler.posts,[])
    def test_pending_resume_failure_not_application_failure(self):
        batch=self.batch('/jobs/123')
        review_fixtures.edit(self.root,batch,{'one':{'review_decision':'PENDING'}})
        b=Batches(self.root);r=b.rows(batch)[0];r.update(resume_status='RESUME_FAILED',application_status='RESUME_FAILED');b.put(r);b.export(batch);b.close()
        # Export intentionally defers while the review edit is pending.
        apply_approved(self.root,self.agent,self.profile,self.config,batch,context=self.context)
        summary=json.loads((self.root/'data/applications/last_session.json').read_text())
        self.assertEqual(summary['approved_application_results'],[]);self.assertEqual(summary['submission_failures'],0)
        self.assertEqual(summary['attempted_job_ids'],[]);self.assertEqual(summary['pending'],1)
        self.assertEqual(len(summary['preparation_issues']),1)
    def test_cross_origin_password_form_stays_blocked(self):
        batch=self.batch('/jobs/123');b=Batches(self.root);b.import_csv(batch)
        r=b.rows(batch)[0];approved=b.approval(r,self.job);b.close()
        page=self.context.new_page();store=Store(self.root/'data/applications')
        config=dict(self.config,REVIEWED_DESTINATION=approved)
        journey=ApplicationJourney(page,self.job,config,store);journey.install();page.goto(self.job['source_url']);journey.authorize(page)
        page.set_content('<h1>Software Engineer</h1><p>Example Company</p><form action="https://unrelated.invalid/password"><input type="password"></form>')
        from job_pipeline.applications.accounts import AccountManager
        manager=AccountManager(self.root,dict(config,APPLICATION_JOURNEY=journey),self.profile,store,self.job);manager.choose_portal(page)
        with self.assertRaises(Blocked):manager.auth_form(page,'never transmitted',False)
        journey.close();store.close();page.close();self.assertEqual(Handler.posts,[])

@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS')=='1','Controlled browser controls')
class GenericControlsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):cls.browser.close();cls.pw.stop()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.agent,self.job,self.pdf,self.profile=fixtures.setup_data(self.root)
        self.store=Store(self.root/'data/applications');self.page=self.browser.new_page()
        self.config=json.loads((fixtures.ROOT/'application_config.json').read_text())
    def tearDown(self):self.page.close();self.store.close();self.temp.cleanup()
    def adapter(self,html):
        self.page.route('https://example.test/**',lambda route:route.fulfill(content_type='text/html',body=html))
        self.page.goto('https://example.test/jobs/123')
        return NativeFormAdapter(self.page,self.config,self.profile,self.store,self.job,{'path':str(self.pdf),'sha256':fixtures.digest(self.pdf.read_bytes())},self.root/'audit')
    def test_two_education_records_and_employment_fields_are_scoped(self):
        self.profile['education']=[dict(university='School A',degree='MS'),dict(university='School B',degree='BS')]
        a=self.adapter('<fieldset data-history-section="education"><legend>Education</legend><label>University<input required></label><label>Degree<input required></label></fieldset><fieldset data-history-section="education"><legend>Education</legend><label>University<input required></label><label>Degree<input required></label></fieldset><fieldset data-history-section="employment"><legend>Work experience</legend><label>Employer<input required></label><label>Job title<input required></label></fieldset>')
        a.fill_fields()
        self.assertEqual(self.page.locator('input').evaluate_all('(n)=>n.map(x=>x.value)'),['School A','MS','School B','BS','Fixture Labs','Software Intern'])
    def test_accessible_autocomplete_selects_only_exact_verified_option(self):
        a=self.adapter('<label>University<input role="combobox" aria-autocomplete="list" required></label><div role="listbox"><div role="option" onclick="document.querySelector(\'input\').value=this.textContent">Stony Brook University</div><div role="option">Wrong School</div></div>')
        a.fill_fields();self.assertEqual(self.page.locator('input').input_value(),'Stony Brook University')
    def test_required_unknown_legal_declaration_remains_unanswered(self):
        a=self.adapter('<label>I agree to binding arbitration<input type="checkbox" required></label>')
        with self.assertRaises(Blocked):a.fill_fields()
        self.assertFalse(self.page.locator('input').is_checked())
    def test_accessible_custom_radio_and_checkbox_readback(self):
        a=self.adapter("""<fieldset><legend>Gender</legend><button type="button" role="radio" aria-checked="false">Female</button><button type="button" role="radio" aria-checked="false" onclick="this.setAttribute('aria-checked','true')">I do not wish to answer</button></fieldset><button type="button" role="checkbox" aria-checked="false" onclick="this.setAttribute('aria-checked','true')">Are you authorized to work in the US?</button>""")
        a.fill_fields()
        self.assertEqual(self.page.get_by_role('radio',name='I do not wish to answer').get_attribute('aria-checked'),'true')
        self.assertEqual(self.page.get_by_role('checkbox').get_attribute('aria-checked'),'true')

    def test_newsletter_form_does_not_block_or_receive_applicant_details(self):
        a=self.adapter('<h1>Software Engineer</h1><p>Example Company</p><form id="newsletter" action="https://unrelated.invalid/news"><label>Email<input type="email" required></label></form>'+FORM)
        a.config['APPROVED_FORM_ORIGINS']=['https://example.test']
        result=a.run()
        self.assertEqual(result['status'],'READY_TO_APPLY')
        self.assertEqual(self.page.locator('#newsletter input').input_value(),'')
        self.assertEqual(self.page.locator('#app input[name=email]').input_value(),'applicant@example.test')

    def test_unknown_autocomplete_option_is_not_guessed(self):
        a=self.adapter('<label>University<input role="combobox" required></label><div role="option">Different University</div>')
        with self.assertRaises(Blocked):a.fill_fields()

import csv
import hashlib
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from job_pipeline.applications.answers import validate_answer
from job_pipeline.applications.browser import NativeFormAdapter, Blocked, platform
from job_pipeline.applications.integration import ReadinessError, application_url, canonical_url, digest, identity, resume_for
from job_pipeline.applications.profile import answer_field, load_profile
from job_pipeline.applications.runner import process
from job_pipeline.applications.storage import Store, save_json

ROOT = Path(__file__).resolve().parent.parent


def setup_data(root, url='https://example.test/jobs/123'):
    agent = root / 'agent'
    folder = agent / 'jobs/example'
    folder.mkdir(parents=True)
    jd = root / 'data/jds/one.txt'
    jd.parent.mkdir(parents=True)
    jd.write_text('Company: Example Company\nTitle: Software Engineer\nRequirements: Python and SQL')
    (folder / 'jd.txt').write_bytes(jd.read_bytes())
    tex = folder / 'resume.tex'
    tex.write_text('Verified local fixture professional experience only')
    pdf = folder / 'Example.pdf'
    # A valid, one-page synthetic PDF: tests exercise real upload bytes, not a renamed text file.
    stream=b'BT /F1 12 Tf 36 750 Td (Synthetic test applicant resume) Tj ET'
    objects=[b'<< /Type /Catalog /Pages 2 0 R >>',b'<< /Type /Pages /Kids [3 0 R] /Count 1 >>',b'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>',b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>',b'<< /Length '+str(len(stream)).encode()+b' >>\nstream\n'+stream+b'\nendstream']
    data=bytearray(b'%PDF-1.4\n');offsets=[0]
    for index,obj in enumerate(objects,1):
        offsets.append(len(data));data.extend(str(index).encode()+b' 0 obj\n'+obj+b'\nendobj\n')
    xref=len(data);data.extend(b'xref\n0 6\n0000000000 65535 f \n')
    for offset in offsets[1:]: data.extend(f'{offset:010d} 00000 n \n'.encode())
    data.extend(b'trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n'+str(xref).encode()+b'\n%%EOF\n')
    pdf.write_bytes(data)
    sha = digest(pdf.read_bytes())
    save_json(folder / 'job.json', dict(company='Example Company', role='Software Engineer', job_url=url))
    save_json(folder / '.verified.json', dict(pdf=pdf.name, pdf_sha256=sha, tex_sha256=digest(tex.read_bytes())))
    job = dict(id='one',company='Example Company',title='Software Engineer',status='jd_fetched',jd_path='data/jds/one.txt',source_url=url,jobright_url='https://jobright.ai/jobs/info/one')
    with (root / 'data/jobs.csv').open('w', newline='') as stream:
        w = csv.DictWriter(stream, fieldnames=list(job)); w.writeheader(); w.writerow(job)
    key = digest(f'{agent.resolve()}\none\n{digest(jd.read_bytes())}'.encode())
    save_json(root / 'data/resumes/state.json', {key:dict(job_id='one',status='completed',folder=str(folder),pdf=str(pdf))})
    p = json.loads((ROOT / 'applicant_profile.example.json').read_text())
    p['facts'].update(full_name='Test Applicant',email='applicant@example.test',phone='5551234567',technical_skills='Python, SQL', university='Stony Brook University', degree='Master of Science', field='Computer Science', authorized_us=True, future_sponsorship=True, us_citizen=False, permanent_resident=False, authorization_end='2099-07-20')
    p['demographics']['gender'] = 'Male'
    p['employment']=[dict(employer='Fixture Labs',title='Software Intern',start_month='2025-06',end_month='2025-08')]
    p['verified_professional_evidence']=['I built a Python service for Fixture Labs.']
    p['resume_approvals'] = {'one':sha}
    save_json(root / 'profile.json', p)
    return agent, job, pdf, load_profile(root / 'profile.json')


class IdentityTests(unittest.TestCase):
    def test_explicit_identity_overrides_profile(self):
        from job_pipeline.applications.identity import configured_email
        with patch.dict(os.environ, {'VANGUARD_EMAIL': 'custom@example.test'}):
            self.assertEqual(configured_email(), 'custom@example.test')

    def test_profile_identity_used_without_override(self):
        from job_pipeline.applications.identity import configured_email
        with patch.dict(os.environ, {'VANGUARD_EMAIL': ''}), patch.object(
            Path, 'is_file', return_value=True), patch.object(
            Path, 'read_text', return_value='{"facts":{"email":"profile@example.test"}}'):
            self.assertEqual(configured_email(), 'profile@example.test')

    def test_missing_identity_has_no_personal_default(self):
        from job_pipeline.applications.identity import configured_email
        with patch.dict(os.environ, {'VANGUARD_EMAIL': ''}), patch.object(Path, 'is_file', return_value=False):
            self.assertEqual(configured_email(), '')


class ApplicationUnitTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.agent, self.job, self.pdf, self.profile = setup_data(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def test_exact_resume_and_approval(self):
        result = resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])
        self.assertEqual(result['path'],str(self.pdf))
        with self.assertRaisesRegex(ReadinessError,'factual approval'):
            resume_for(self.root,self.agent,self.job,{},approval_mode='manual')

    def test_wrong_company_blocked(self):
        self.job['company']='Another Company'
        with self.assertRaisesRegex(ReadinessError,'mismatch'):
            resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])

    def test_changed_jd_waits_for_new_resume(self):
        (self.root/self.job['jd_path']).write_text('New requirements')
        with self.assertRaises(ReadinessError) as caught:
            resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])
        self.assertEqual(caught.exception.status,'WAITING_FOR_RESUME')

    def test_modified_pdf_blocked(self):
        self.pdf.write_bytes(b'%PDF-1.4 altered')
        with self.assertRaisesRegex(ReadinessError,'hash'):
            resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])

    def test_missing_pdf_waits(self):
        self.pdf.unlink()
        with self.assertRaises(ReadinessError) as caught:
            resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])
        self.assertEqual(caught.exception.status,'WAITING_FOR_RESUME')

    def test_exported_pdf_exact_hash(self):
        export = self.root/'exported.pdf'
        self.pdf.rename(export)
        save_json(self.agent/'jobs/example/.exported.json',dict(export_path=str(export)))
        self.assertEqual(resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])['path'],str(export))

    def test_urls_preserve_requisitions(self):
        self.assertEqual(application_url(self.job),'https://example.test/jobs/123')
        self.assertEqual(canonical_url('https://example.test/a?utm_x=1&jobId=23#apply'),'https://example.test/a?jobId=23')
        self.assertNotEqual(identity('https://example.test/?jobId=1'),identity('https://example.test/?jobId=2'))
        self.assertEqual(identity('https://jobs.lever.co/acme/12345678-1234-1234-1234-123456789012'),identity('https://jobs.lever.co/acme/12345678-1234-1234-1234-123456789012/apply'))
        self.assertEqual(identity('https://boards.greenhouse.io/acme/jobs/123?gh_src=abc'),identity('https://acme.greenhouse.io/embed?gh_jid=123'))
        with self.assertRaises(ValueError): canonical_url('javascript:alert(1)')

    def test_profile_and_authorization(self):
        def answer(q): return answer_field(q,self.profile)
        self.assertEqual(answer('Full legal name')['answer'],'Test Applicant')
        self.assertIs(answer('Are you legally authorized to work in the United States?')['answer'],True)
        self.assertIs(answer('Will you now or in the future require sponsorship for employment visa status?')['answer'],True)
        self.assertIs(answer('Are you a U.S. citizen?')['answer'],False)
        self.assertIs(answer('Are you a permanent resident?')['answer'],False)
        self.assertEqual(answer('When does your current work authorization expire?')['answer'],'2099-07-20')
        for q in ['Do you require sponsorship now?','Can you work without restriction for any U.S. employer?','Will you need STEM OPT participation?','Are you legally authorized to work in the United States without sponsorship forever?','Do you have a security clearance?','I agree to arbitration','Veteran status']:
            self.assertTrue(answer(q)['requires_review'],q)
        self.assertEqual(answer('Gender')['answer'],'I do not wish to answer')
        self.assertEqual(answer_field('Gender',self.profile,True)['answer'],'Male')
        self.assertTrue(answer('Earliest start date')['requires_review'])

    def test_expired_authorization_never_reused(self):
        self.profile['facts']['authorization_end']='2000-01-01'
        self.assertTrue(answer_field('Are you authorized to work in the US?',self.profile)['requires_review'])

    def test_generated_answer_validation(self):
        v=dict(answer='I built a Python service.',question_category='technical',evidence_sources=['Built a Python service'],confidence=.9,requires_review=False,reason='')
        result=validate_answer(v,'Built a Python service')
        self.assertTrue(result['requires_review'])
        with self.assertRaises(ValueError):validate_answer(v,'Different evidence')
        with self.assertRaises(ValueError):validate_answer(v,'Built a Python service',max_chars=5)
        with self.assertRaises(ValueError):validate_answer(v,'Built a Python service',numeric=True)

    def test_ai_workflow_is_company_scoped_and_drafts_require_review(self):
        from job_pipeline.applications.answers import generate_application_answer
        from types import SimpleNamespace
        store=Store(self.root/'memory')
        self.profile['verified_professional_evidence']=['Built a Python service']
        resume=resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])
        calls=[]
        def model(command, **kwargs):
            calls.append(kwargs['input'])
            output=Path(command[command.index('--output-last-message')+1])
            save_json(output,dict(answer='I built a Python service.',question_category='technical',evidence_sources=['Built a Python service'],confidence=.9,requires_review=False,reason=''))
            self.assertIn('--ignore-user-config',command)
            self.assertIn('features.shell_tool=false',command)
            self.assertNotIn('OPENAI_API_KEY',kwargs['env'])
            return SimpleNamespace(returncode=0)
        with patch('job_pipeline.applications.answers.subprocess.run',side_effect=model):
            result=generate_application_answer('Why this company?',self.job,resume,self.profile,self.agent,self.root/'answers',store)
            self.assertTrue(result['requires_review'])
            generate_application_answer('Why this company?',self.job,resume,self.profile,self.agent,self.root/'answers',store)
            other=dict(self.job,company='Other Company')
            generate_application_answer('Why this company?',other,resume,self.profile,self.agent,self.root/'answers',store)
        self.assertEqual(len(calls),2)
        self.assertIn('Other Company',calls[-1])
        store.close()

    def test_wrong_metadata_url_and_changed_source_block(self):
        folder=self.agent/'jobs/example'
        save_json(folder/'job.json',dict(company=self.job['company'],role=self.job['title'],job_url='https://example.test/jobs/999'))
        with self.assertRaisesRegex(ReadinessError,'different job URL'):
            resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])
        save_json(folder/'job.json',dict(company=self.job['company'],role=self.job['title']))
        (folder/'resume.tex').write_text('changed')
        with self.assertRaisesRegex(ReadinessError,'source changed'):
            resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])

    def test_previous_employment_uses_verified_record(self):
        self.assertEqual(answer_field('Most recent employer',self.profile)['answer'],'Fixture Labs')
        self.assertEqual(answer_field('Most recent employment start month',self.profile)['answer'],'2025-06')
        self.assertTrue(answer_field('All previous employers',self.profile)['requires_review'])

    def test_csv_state_duplicates_and_crash(self):
        store=Store(self.root/'state')
        store.update('a',identity='req1',status='IN_PROGRESS',company='=IMPORTXML("bad")',attempt_count=1)
        store.close()
        store=Store(self.root/'state')
        self.assertEqual(store.get('a')['status'],'READY_TO_APPLY')
        store.update('a',status='SUBMISSION_UNCONFIRMED')
        self.assertEqual(store.duplicate('req1','b')['job_id'],'a')
        self.assertIsNone(store.duplicate('req2','b'))
        with self.assertRaises(ValueError):store.update('a',status='READY_TO_APPLY')
        store.update('a',status='APPLIED',confirmation_evidence='Application received',submitted_at='2026-10-21T00:00:00+00:00')
        with self.assertRaises(ValueError):store.update('a',status='IN_PROGRESS')
        with self.assertRaises(ValueError):store.update('c',status='APPLIED')
        store.export()
        with (self.root/'state/job_applications.csv').open() as stream:
            csvrows=list(csv.DictReader(stream))
        self.assertEqual(csvrows[0]['status'],'APPLIED')
        self.assertTrue(csvrows[0]['company'].startswith("'="))
        store.close()

    def test_review_queue_and_memory(self):
        store=Store(self.root/'state')
        review_id=store.review('one','Start date?','Missing',suggested_answer=None)
        self.assertEqual(store.reviews()[0]['id'],review_id)
        store.review('one','Start date?','Still missing')
        self.assertEqual(len(store.reviews()),1)
        store.cache('key',dict(answer='value',profile_version='v1'))
        self.assertEqual(store.cache('key')['answer'],'value')
        store.close()

    def test_detect_platform_exact_hostname(self):
        self.assertEqual(platform('https://evilgreenhouse.io/jobs/1'),'generic')
        self.assertEqual(platform('https://jobs.lever.co/company/abc'),'lever')
        self.assertEqual(platform('https://acme.myworkdayjobs.com/job/1'),'workday')

    def test_inspect_reads_real_interfaces_without_browser(self):
        config=json.loads((ROOT/'application_config.json').read_text())
        summary=process(self.root,self.agent,self.profile,config)
        self.assertEqual(summary['counts'],{'READY_TO_APPLY':1})
        self.assertEqual(summary['verified_submissions_this_run'],0)
        self.assertTrue((self.root/'data/applications/job_applications.csv').exists())


class FixtureHandler(BaseHTTPRequestHandler):
    submissions=[]
    def log_message(self,*args): pass
    def do_GET(self):
        body=(ROOT/'tests/fixtures/application.html').read_bytes()
        self.send_response(200);self.send_header('Content-Type','text/html');self.end_headers();self.wfile.write(body)
    def do_POST(self):
        body=self.rfile.read(int(self.headers['Content-Length']))
        self.submissions.append(body)
        if 'fail' in self.path:
            self.send_response(422);content=b'Required screening response is invalid'
        elif 'uncertain' in self.path:
            self.send_response(200);content=b'Thank you for visiting'
        else:
            required=[b'Test Applicant',b'applicant@example.test',b'Example.pdf',b'%PDF-1.4',b'2099-07-20']
            if not all(x in body for x in required):
                self.send_response(400);content=b'Fixture rejected missing fields or resume'
            else:
                self.send_response(200);content=b'Application submitted. Confirmation ID: LOCAL-123'
        self.send_header('Content-Type','text/plain');self.end_headers();self.wfile.write(content)


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS')=='1','Set RUN_BROWSER_TESTS=1 for controlled local browser tests')
class ApplicationBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from playwright.sync_api import sync_playwright
        cls.server=ThreadingHTTPServer(('127.0.0.1',0),FixtureHandler)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.origin=f'http://127.0.0.1:{cls.server.server_port}'
        cls.pw=sync_playwright().start();cls.browser=cls.pw.chromium.launch(headless=True)
    @classmethod
    def tearDownClass(cls):
        cls.browser.close();cls.pw.stop();cls.server.shutdown();cls.server.server_close();cls.thread.join()
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.agent,self.job,self.pdf,self.profile=setup_data(self.root,self.origin+'/jobs/123')
        self.context=self.browser.new_context()
        self.config=json.loads((ROOT/'application_config.json').read_text())
        self.config.update(APPROVED_FORM_ORIGINS=[self.origin],APPROVED_SUBMIT_ORIGINS=[self.origin],AUTO_SUBMIT_ENABLED=True,DRY_RUN=False)
        FixtureHandler.submissions.clear()
        store=Store(self.root/'data/applications')
        key=digest(json.dumps(['one','Describe your experience with Python',self.profile['version']],ensure_ascii=False).encode())
        store.cache('verified:'+key,dict(answer='I built a Python service for Fixture Labs.',verification_status='user_verified'))
        store.close()
    def tearDown(self):
        self.context.close();self.temp.cleanup()
    def run_pipeline(self,mode='apply'):
        return process(self.root,self.agent,self.profile,self.config,mode=mode,context=self.context)
    def change_url(self,suffix):
        self.job['source_url']=self.origin+'/jobs/123'+suffix
        save_json(self.agent/'jobs/example/job.json',dict(company=self.job['company'],role=self.job['title'],job_url=self.job['source_url']))
        with (self.root/'data/jobs.csv').open('w',newline='') as s:
            w=csv.DictWriter(s,fieldnames=list(self.job));w.writeheader();w.writerow(self.job)
    def test_end_to_end_real_http_upload_submission_and_duplicate_skip(self):
        summary=self.run_pipeline()
        self.assertEqual(summary['counts'],{'APPLIED':1},summary)
        self.assertEqual(summary['verified_submissions_this_run'],1)
        self.assertEqual(len(FixtureHandler.submissions),1)
        row=summary['applications'][0]
        self.assertEqual(row['confirmation_id'],'LOCAL-123')
        self.assertIn('Application submitted',row['confirmation_evidence'])
        body=FixtureHandler.submissions[0]
        self.assertIn(b'name="citizen"\r\n\r\nNo',body)
        self.assertIn(b'name="sponsorship"\r\n\r\nYes',body)
        self.assertIn(b'I do not wish to answer',body)
        self.assertIn(b'Fixture Labs',body)
        self.assertIn(b'2025-06',body)
        self.assertIn(b'I built a Python service for Fixture Labs.',body)
        again=self.run_pipeline()
        self.assertEqual(again['verified_submissions_this_run'],0)
        self.assertEqual(len(FixtureHandler.submissions),1)
    def test_automatic_resume_without_approval_dry_run(self):
        self.profile['resume_approvals'] = {}
        self.config['DRY_RUN'] = True
        summary = self.run_pipeline()
        self.assertEqual(summary['counts'], {'READY_TO_APPLY': 1}, summary)
        self.assertEqual(summary['applications'][0]['resume_path'], str(self.pdf))
        self.assertEqual(summary['applications'][0]['resume_status'], 'READY_FOR_APPLICATION')
        self.assertEqual(FixtureHandler.submissions, [])

    def test_duplicate_jobright_records_share_confirmed_requisition(self):
        self.run_pipeline()
        alias=dict(self.job,id='two',jobright_url='https://jobright.ai/jobs/info/two')
        with (self.root/'data/jobs.csv').open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(self.job));writer.writeheader();writer.writerows([self.job,alias])
        summary=self.run_pipeline()
        self.assertEqual(summary['counts'],{'APPLIED':1,'ALREADY_APPLIED':1},summary)
        self.assertEqual(summary['verified_submissions_this_run'],0)
        self.assertEqual(len(FixtureHandler.submissions),1)

    def test_applied_history_does_not_consume_run_limit(self):
        import shutil
        self.run_pipeline()
        second=dict(self.job,id='two',source_url=self.origin+'/jobs/124',jobright_url='https://jobright.ai/jobs/info/two')
        folder=self.agent/'jobs/second'
        shutil.copytree(self.agent/'jobs/example',folder)
        save_json(folder/'job.json',dict(company=second['company'],role=second['title'],job_url=second['source_url']))
        (self.root/'data/jds/two.txt').write_bytes((self.root/self.job['jd_path']).read_bytes())
        second['jd_path']='data/jds/two.txt'
        statepath=self.root/'data/resumes/state.json';state=json.loads(statepath.read_text())
        key=digest(f"{self.agent.resolve()}\ntwo\n{digest((self.root/second['jd_path']).read_bytes())}".encode())
        state[key]=dict(job_id='two',status='completed',folder=str(folder),pdf=str(folder/'Example.pdf'))
        save_json(statepath,state)
        self.profile['resume_approvals']['two']=self.profile['resume_approvals']['one']
        store=Store(self.root/'data/applications')
        key=digest(json.dumps(['two','Describe your experience with Python',self.profile['version']],ensure_ascii=False).encode())
        store.cache('verified:'+key,dict(answer='I built a Python service for Fixture Labs.',verification_status='user_verified'))
        store.close()
        with (self.root/'data/jobs.csv').open('w',newline='') as stream:
            writer=csv.DictWriter(stream,fieldnames=list(self.job));writer.writeheader();writer.writerows([self.job,second])
        self.config['MAX_APPLICATIONS_PER_RUN']=1
        summary=self.run_pipeline()
        self.assertEqual(summary['verified_submissions_this_run'],1,summary)
        self.assertEqual(len(FixtureHandler.submissions),2)

    def test_known_immigration_facts_cannot_be_overridden_by_memory(self):
        page,store,adapter=self.isolated_adapter('<label>Are you a U.S. citizen?<select required><option value="">Select</option><option>Yes</option><option>No</option></select></label>')
        key=digest(json.dumps(['one','Are you a U.S. citizen?',self.profile['version']],ensure_ascii=False).encode())
        store.cache('verified:'+key,dict(answer=True,verification_status='user_verified'))
        adapter.fill_fields()
        self.assertEqual(page.locator('select').input_value(),'No')
        store.close();page.close()

    def test_dry_run_never_posts(self):
        self.config['DRY_RUN']=True
        summary=self.run_pipeline('dry-run')
        self.assertEqual(summary['counts'],{'READY_TO_APPLY':1},summary)
        self.assertEqual(FixtureHandler.submissions,[])
    def test_missing_personal_field_creates_review_and_does_not_submit(self):
        self.profile['facts']['phone']=None
        summary=self.run_pipeline()
        self.assertEqual(summary['counts'],{'NEEDS_REVIEW':1},summary)
        self.assertEqual(FixtureHandler.submissions,[])
        store=Store(self.root/'data/applications')
        self.assertTrue(any(r['question']=='Phone number' for r in store.reviews()));store.close()
    def test_unconfirmed_never_retries(self):
        self.change_url('?uncertain=1')
        summary=self.run_pipeline()
        self.assertEqual(summary['counts'],{'SUBMISSION_UNCONFIRMED':1},summary)
        self.run_pipeline();self.assertEqual(len(FixtureHandler.submissions),1)
    def test_server_validation_failure_is_not_success_or_resubmitted(self):
        self.change_url('?fail=1')
        summary=self.run_pipeline()
        self.assertEqual(summary['counts'],{'SUBMISSION_UNCONFIRMED':1},summary)
        self.assertEqual(len(FixtureHandler.submissions),1)
    def test_unapproved_origin_is_blocked(self):
        self.config['APPROVED_FORM_ORIGINS']=[]
        summary=self.run_pipeline()
        self.assertEqual(summary['counts'],{'NEEDS_REVIEW':1},summary)
        self.assertEqual(FixtureHandler.submissions,[])
    def test_unsupported_widget_and_consequential_checkbox_block(self):
        page=self.context.new_page();page.goto(self.job['source_url'])
        page.locator('#one').evaluate('(e)=>e.innerHTML=`<label>I agree to arbitration<input type="checkbox" required></label>`')
        store=Store(self.root/'isolated');store.update('one',status='IN_PROGRESS')
        resume=resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])
        adapter=NativeFormAdapter(page,self.config,self.profile,store,self.job,resume,self.root/'evidence')
        with self.assertRaises(Blocked):adapter.fill_fields()
        self.assertFalse(page.locator('input[type=checkbox]').is_checked())
        store.close();page.close()
    def isolated_adapter(self, html):
        page=self.context.new_page();page.goto(self.job['source_url']);page.set_content(html)
        store=Store(self.root/'isolated')
        resume=resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])
        return page,store,NativeFormAdapter(page,self.config,self.profile,store,self.job,resume,self.root/'evidence')

    def test_auth_captcha_and_unsupported_ats(self):
        for html,status in [('<input type="password">','BLOCKED_AUTH'),('<div class="g-recaptcha"></div>','BLOCKED_CAPTCHA'),('<p>This position is closed</p>','JOB_CLOSED')]:
            page,store,adapter=self.isolated_adapter(html)
            with self.assertRaises(Blocked) as caught:adapter.guard()
            self.assertEqual(caught.exception.status,status)
            store.close();page.close()
        page,store,adapter=self.isolated_adapter('<h1>Software Engineer</h1>')
        page.route('https://acme.myworkdayjobs.com/**',lambda route:route.fulfill(body='<h1>Software Engineer</h1><p>Example Company</p>'))
        page.goto('https://acme.myworkdayjobs.com/job/123')
        adapter.job=dict(adapter.job,source_url=page.url)
        adapter.config=dict(adapter.config,APPROVED_FORM_ORIGINS=['https://acme.myworkdayjobs.com'])
        with self.assertRaises(Blocked) as caught:adapter.run()
        self.assertEqual(caught.exception.status,'NOT_SUPPORTED')
        store.close();page.close()

    def test_duplicate_labels_and_custom_widgets_need_review(self):
        for html in ['<label>University<input required></label><label>University<input required></label>', '<label for="widget">Degree</label><div id="widget" role="combobox" aria-required="true" tabindex="0"></div>']:
            page,store,adapter=self.isolated_adapter(html)
            with self.assertRaises(Blocked):adapter.fill_fields()
            self.assertTrue(store.reviews())
            store.close();page.close()

    def test_citizenship_option_mismatch_never_guessed(self):
        page,store,adapter=self.isolated_adapter('<label>Are you a U.S. citizen?<select required><option value="">Select</option><option>Yes, citizen</option><option>Yes, permanent resident</option></select></label>')
        with self.assertRaises(Blocked):adapter.fill_fields()
        self.assertEqual(page.locator('select').input_value(),'')
        store.close();page.close()

    def test_destination_title_mismatch_stops_before_upload(self):
        page=self.context.new_page();page.goto(self.job['source_url'])
        page.locator('h1').inner_text()
        page.locator('h1').evaluate('(e)=>e.textContent="Different Engineer"')
        store=Store(self.root/'isolated')
        resume=resume_for(self.root,self.agent,self.job,self.profile['resume_approvals'])
        adapter=NativeFormAdapter(page,self.config,self.profile,store,self.job,resume,self.root/'evidence')
        with self.assertRaises(Blocked):adapter.validate_identity()
        self.assertEqual(page.locator('input[type=file]').evaluate('(e)=>e.files.length'),0)
        store.close();page.close()

    def test_submit_boundary_crash_preserves_uncertainty(self):
        from playwright.sync_api import Locator
        original=Locator.click
        def crash(control,*args,**kwargs):
            if control.inner_text()=='Submit application':
                raise OSError('Controlled browser crash at irreversible boundary')
            return original(control,*args,**kwargs)
        with patch.object(Locator,'click',crash):
            summary=self.run_pipeline()
        self.assertEqual(summary['counts'],{'SUBMISSION_UNCONFIRMED':1},summary)
        self.assertEqual(FixtureHandler.submissions,[])
        self.run_pipeline()
        self.assertEqual(FixtureHandler.submissions,[])

    def test_confirmation_does_not_accept_job_description_phrase(self):
        page=self.context.new_page();page.set_content('<h1>Software Engineer</h1><p>Your application has been submitted</p>')
        store=Store(self.root/'isolated')
        adapter=NativeFormAdapter(page,self.config,self.profile,store,self.job,{},self.root/'evidence')
        self.assertEqual(adapter.inspect_confirmation(),'')
        page.set_content('<div role="status">Application received. Reference: ABC-1</div>')
        self.assertIn('ABC-1',adapter.inspect_confirmation())
        store.close();page.close()

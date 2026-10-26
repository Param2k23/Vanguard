"""Fresh-only orchestration regressions. No external submissions/accounts."""
import csv
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import yaml

from job_pipeline.applications.sessions import Session, Ledger, full, DEFAULTS
from job_pipeline.applications.storage import Store, save_json
from job_pipeline.applications.integration import identity
from job_pipeline.pipeline import FIELDS


def job(i, company='Example'):
    return dict(id=str(i), company=company, title='Engineer', source_url=f'https://boards.greenhouse.io/example/jobs/{i}',
                jobright_url=f'https://jobright.ai/jobs/info/{i}', status='jd_fetched',
                jd_path=f'data/jds/{i}.txt', collected_at='', notes='')


class FreshTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / 'data/jds').mkdir(parents=True)
        self.agent = self.root / 'agent'
        self.generated = []
        self.processed = []
        self.config = dict(DEFAULTS, DRY_RUN=True, MAX_SCRAPE_LISTINGS=300)
        self.write([job(i) for i in range(111)])

    def write(self, rows):
        with (self.root / 'data/jobs.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, FIELDS, extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
        for row in rows:
            (self.root / row['jd_path']).write_text('Requirements for ' + row['id'])

    def scraper(self, rows):
        def scrape(session, headed):
            with (self.root / 'data/jobs.csv').open() as stream:
                saved = list(csv.DictReader(stream))
            for row in rows:
                if len(session.state['selected']) >= session.state['requested_job_limit'] or not session.scan():
                    break
                session.accept(dict(row))
                if session.last_new:
                    saved.append(row)
            self.write(saved)
        return scrape

    def generate(self, root, agent, row):
        self.generated.append(row['id'])

    def processor(self, root, agent, profile, config, **kwargs):
        self.assertEqual(kwargs['limit'], 1)
        self.assertEqual(len(kwargs['job_ids']), 1)
        identifier = next(iter(kwargs['job_ids']))
        self.processed.append(identifier)
        store = Store(root / 'data/applications')
        try:
            row = next(j for j in csv.DictReader((root / 'data/jobs.csv').open()) if j['id'] == identifier)
            policy = SessionPolicy(row)
            store.update(identifier, company=row['company'], position=row['title'], status='NEEDS_REVIEW', company_policy=policy, resume_path=f'{identifier}.pdf')
        finally:
            store.close()
        return dict(verified_submissions_this_run=0, stopped=False)

    def run_full(self, rows, **kw):
        with patch('job_pipeline.applications.sessions.resume_for', return_value={'path':'fixture.pdf'}):
            return full(self.root, self.agent, {}, self.config, scraper=self.scraper(rows),
                        generator=self.generate, processor=self.processor, **kw)

    def test_A_B_C_D_baseline_new_repeated_mixed(self):
        ledger = Ledger(self.root)
        original = ledger.baseline_path.read_bytes()
        first = self.run_full([job(i) for i in range(116)], limit=5)
        self.assertEqual([j['id'] for j in first['selected']], [str(i) for i in range(111,116)])
        self.assertEqual(len(self.generated),5)
        second = self.run_full([job(i) for i in range(111,116)], limit=5)
        self.assertEqual(second['selected'],[])
        third = self.run_full([job(i) for i in list(range(10))+list(range(111,119))], limit=10)
        self.assertEqual([j['id'] for j in third['selected']],['116','117','118'])
        self.assertEqual(ledger.baseline_path.read_bytes(), original)
        self.assertEqual(len(Ledger(self.root).records),119)
        self.assertEqual(len(self.processed),8)

    def test_E_F_G_policy_reports(self):
        first = self.run_full([job(201,'Google'), job(202,'Amazon'), job(203,'TikTok'),job(204,'IBM')])
        self.assertEqual(self.generated,['201','202'])
        self.assertEqual(first['referral_jobs'],2)
        self.assertEqual(first['excluded_jobs'],2)
        second = self.run_full([])
        self.assertNotEqual(first['run_id'],second['run_id'])
        latest = (self.root / 'data/applications/latest_run_report.md').read_text()
        self.assertIn(second['run_id'],latest)
        self.assertEqual(second['verified_applications'],0)
        self.assertTrue(Path(first['report_path']).is_file())

    def test_H_crash_resume(self):
        original = self.processor
        def crash(*args, **kw):
            if self.processed:
                raise KeyboardInterrupt
            return original(*args, **kw)
        self.processor = crash
        with self.assertRaises(KeyboardInterrupt):
            self.run_full([job(201),job(202),job(203)])
        path = next((self.root / 'data/applications/runs').glob('*/run_summary.json'))
        interrupted = json.loads(path.read_text())
        self.processor = original
        result = self.run_full([],run_id=interrupted['run_id'])
        self.assertEqual(result['run_id'],interrupted['run_id'])
        self.assertEqual(self.processed,['201','202','203'])
        self.assertEqual(result['resume_generation_successes'],3)

    def test_I_bounds(self):
        self.config['MAX_SCRAPE_LISTINGS']=3
        result = self.run_full([job(i) for i in range(201,220)],limit=10)
        self.assertEqual(result['new_jobs_found'],3)
        session = Session(self.root,self.config)
        session.state['number_of_listings_scanned']=3
        self.assertFalse(session.scan())
        self.assertTrue(session.state['scrape_limit_reached'])

    def test_requisition_and_uncertain_identity(self):
        ledger = Ledger(self.root)
        same = job(500); same['source_url']=job(1)['source_url']+'?utm_source=new'
        self.assertIsNotNone(ledger.match(same)[0])
        self.assertIsNone(ledger.match(job(501))[0])
        uncertain = job(502); uncertain['source_url']=''
        self.assertTrue(ledger.match(uncertain)[1])

    def test_preserve_verified_history(self):
        store=Store(self.root/'data/applications')
        row=store.update('1',status='APPLIED',confirmation_evidence='Local fixture confirmation',submitted_at='2026-01-01T00:00:00+00:00')
        store.close()
        Ledger(self.root)
        store=Store(self.root/'data/applications')
        self.assertEqual(store.get('1'),row)
        store.close()


def SessionPolicy(row):
    return 'REFERRAL_REQUIRED' if row['company'] in ('Google','Amazon') else 'AUTO_APPLY'


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS')=='1','Local browser integration requires RUN_BROWSER_TESTS=1')
class RealEntryPointTests(unittest.TestCase):
    def test_J_real_collect_fetch_and_resume_entrypoint(self):
        """Use real DOM scraper and real resume CLI, with a local fake Codex process."""
        import generate_resumes
        from tests.test_applications import setup_data
        from job_pipeline.pipeline import Pipeline
        from playwright.sync_api import sync_playwright
        from job_pipeline.applications.runner import process
        from apply_jobs import configuration
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            (root/'data').mkdir()
            (root/'data/jobs.csv').write_text(','.join(FIELDS)+'\n')
            agent=root/'agent'
            for name in ('AGENTS.md','base/system_prompt.md','scripts/new_job.py','scripts/verify_resume.py'):
                target=agent/name; target.parent.mkdir(parents=True,exist_ok=True);target.touch()
            (root/'job_pipeline').mkdir()
            (root/'job_pipeline/scroll.js').write_text(Path('job_pipeline/scroll.js').read_text())
            with tempfile.TemporaryDirectory() as pdf_tmp:
                fixture_pdf = setup_data(Path(pdf_tmp))[2].read_bytes()
            cfg=yaml.safe_load(Path('config.yaml').read_text())
            cfg.update(max_scroll_attempts=0,settle_ms=1)
            html='''<div data-tut="jobs-card-match-score"><a href="https://boards.greenhouse.io/example/jobs/999"><h2>Engineer</h2></a><div class="company">Fixture</div></div>'''
            cfg['selectors']['company']='.company'
            cfg['selectors']['link']='a'
            cfg['selectors']['card_locations']='.absent'
            with sync_playwright() as pw:
                browser=pw.chromium.launch(headless=True)
                context=browser.new_context()
                context.route('**/*',lambda route: route.fulfill(status=200,content_type='text/html',body=html if 'jobright.ai' in route.request.url else '<main>'+'Software requirements and responsibilities. '*60+'</main>'))
                page=context.new_page()
                def scraper(session,headed):
                    pipe=Pipeline(page,cfg)
                    pipe.collect(1,session=session)
                    pipe.fetch(job_ids={j['id'] for j in session.state['selected']})
                def generator(root,agent,job):
                    def command(cmd,**kwargs):
                        import subprocess
                        if 'new_job.py' in str(cmd[1]):
                            folder=agent/'jobs/fixture'; folder.mkdir(parents=True)
                            (folder/'jd.txt').write_bytes((root/job['jd_path']).read_bytes())
                            (folder/'job.json').write_text(json.dumps(dict(company=job['company'],role=job['title'],job_url=job['source_url'])))
                            return subprocess.CompletedProcess(cmd,0,stdout='jobs/fixture',stderr='')
                        folder=agent/'jobs/fixture'
                        pdf=folder/'resume.pdf'
                        if cmd[0]=='codex':
                            self.assertIn('Never invent employment', kwargs['input'])
                            self.assertIn('Strategy B MUST trigger', kwargs['input'])
                            self.assertIn('Do not perform outreach or send messages', kwargs['input'])
                            self.assertNotIn('This is an assumed-completion draft', kwargs['input'])
                            pdf.write_bytes(fixture_pdf)
                            (folder/'resume.tex').write_text('fixture tex')
                            (folder/'report.md').write_text('Local fixture only')
                            save_json(folder/'.verified.json',dict(pdf='resume.pdf',pdf_sha256=hashlib.sha256(pdf.read_bytes()).hexdigest(),tex_sha256=hashlib.sha256((folder/'resume.tex').read_bytes()).hexdigest()))
                            save_json(Path(cmd[cmd.index('--output-last-message')+1]),dict(success=True,base='data',summary='local fixture'))
                            return subprocess.CompletedProcess(cmd,0)
                        return subprocess.CompletedProcess(cmd,0,stdout='PASS: '+str(pdf)+'\n',stderr='')
                    with patch.object(generate_resumes,'ROOT',root), patch('sys.argv',['generate_resumes.py','--agent-dir',str(agent),'--job-id',job['id']]),patch.object(generate_resumes.subprocess,'run',side_effect=command),patch.object(generate_resumes.shutil,'which',return_value='/fixture'),patch.object(generate_resumes,'check_project_spacing'):
                        self.assertEqual(generate_resumes.main(),0)
                received=[]
                def processor(*args,**kwargs):
                    received.append(kwargs['job_ids'])
                    return process(*args,context=context,**kwargs)
                config=configuration(Path('/tmp/no-local-configuration.json'))
                try:
                    import apply_jobs
                    save_json(root/'application_config.json', config)
                    save_json(root/'applicant_profile.json', {'resume_approvals':{}})
                    captured=[]
                    def integrated(*args, **kwargs):
                        captured.append(full(*args, **kwargs, scraper=scraper,generator=generator,processor=processor))
                        return captured[-1]
                    with patch('job_pipeline.pipeline.ROOT',root), patch.object(apply_jobs,'ROOT',root), patch('job_pipeline.applications.sessions.full',side_effect=integrated):
                        self.assertEqual(apply_jobs.main(['full','--fresh','--limit','5','--dry-run','--headed','--disable-auto-submit','--agent-dir',str(agent),'--profile',str(root/'applicant_profile.json')]),0)
                    result=captured[0]
                    self.assertEqual(result['new_jobs_found'],1)
                    identifier=result['selected'][0]['id']
                    self.assertEqual(received,[{identifier}])
                    self.assertTrue(result['outcomes'][identifier]['resume_path'].endswith('resume.pdf'))
                    self.assertEqual(result['outcomes'][identifier]['status'],'NEEDS_REVIEW')
                    self.assertEqual(result['verified_applications'],0)
                    self.assertEqual(result['application_attempts'],1)
                    self.assertTrue(result['outcomes'][identifier]['last_attempt_at'])
                    self.assertNotIn('factual approval',result['outcomes'][identifier]['review_reason'])
                finally:
                    browser.close()

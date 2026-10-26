"""Offline regression checks; never sends mail or alters base files."""
import contextlib
import hashlib
import io
import json
import os
import shutil
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import verify_resume as verifier

def normalize_links(text):
    return re.sub(r'\\href\{https://github.com/example-user/([^}]+)\}\{example-user/[^}]+\}',lambda m:r'\href{https://github.com/example-user/'+m[1]+r'}{\underline{example-user/'+m[1]+'}}',text)

class WorkflowTest(unittest.TestCase):
    def verify(self,path,base):
        with tempfile.TemporaryDirectory() as tmp:
            fixture=Path(tmp)
            shutil.copytree(ROOT/'scripts', fixture/'scripts', ignore=shutil.ignore_patterns('__pycache__'))
            (fixture/'base').mkdir()
            for label in ('Data','Fullstack'):
                shutil.copy2(ROOT/'base'/f'Applicant_{label}.example.tex', fixture/'base'/f'Applicant_{label}.tex')
            env={**os.environ, 'VANGUARD_RESUME_PREFIX':'Applicant', 'VANGUARD_GITHUB_USER':'example-user'}
            result=subprocess.run([sys.executable,str(fixture/'scripts/verify_resume.py'),str(path),'--base',base],capture_output=True,text=True,env=env)
        return result.returncode,result.stdout
    @unittest.skipUnless(shutil.which("pdflatex") and shutil.which("pdfinfo") and shutil.which("pdftotext"), "TeX and Poppler required")
    def test_base_copies_and_guardrails(self):
        for base,label in [('data','Data'),('fullstack','Fullstack')]:
            original=(ROOT/'base'/f'Applicant_{label}.example.tex').read_bytes()
            with tempfile.TemporaryDirectory() as tmp:
                job=Path(tmp); (job/'job.json').write_text(json.dumps({'company':'Selftest','role':label}))
                source=job/'resume.tex'; source.write_text(original.decode().replace(r'\underline{example-user/enterprise-agent-lab}', 'example-user/enterprise-agent-lab'))
                code,output=self.verify(job,base)
                self.assertNotEqual(code,0); self.assertIn('FAIL: project GitHub links',output)
                self.assertFalse((job/'.verified.json').exists())
                corrected=normalize_links(original.decode()); source.write_text(corrected)
                code,output=self.verify(job,base)
                self.assertEqual(code,0,output); self.assertTrue((job/'Applicant_Selftest.pdf').exists())
                receipt=json.loads((job/'.verified.json').read_text())
                self.assertEqual(receipt['page_count'],1)
                self.assertTrue(receipt['timestamp']); self.assertTrue(receipt['checks'])
                self.assertEqual(receipt['tex_sha256'],verifier.digest(source))
                self.assertEqual(receipt['pdf_sha256'],verifier.digest(job/receipt['pdf']))
                source.write_text(corrected.replace('Languages}{:', 'Languages}{: Rust, '))
                code,output=self.verify(job,base)
                self.assertNotEqual(code,0); self.assertIn('FAIL: skills character budgets',output)
                self.assertFalse((job/'.verified.json').exists())
                source.write_text(corrected.replace('Enterprise Agent Lab','Selftest Agent Lab'))
                code,output=self.verify(job,base)
                self.assertNotEqual(code,0); self.assertIn('FAIL: company absent',output)
                source.write_text(corrected.replace('Enterprise Agent Lab','Alternate Agent Lab').replace('supporting 50+ LLM providers','supporting many LLM providers'))
                code,output=self.verify(job,base)
                # The replaced project retains other metrics, so explicitly remove all digits from one bullet.
                projects=verifier.projects(source.read_text()); bullet=projects[-1][2][0]
                source.write_text(source.read_text().replace(bullet,re.sub(r'\d','',bullet)))
                code,output=self.verify(job,base)
                self.assertNotEqual(code,0); self.assertIn('FAIL: replacement:',output)
                source.write_text(corrected.replace(r'\end{document}',r'\newpage Extra page\end{document}'))
                code,output=self.verify(job,base)
                self.assertNotEqual(code,0); self.assertIn('FAIL: exactly 1 page',output)
            self.assertEqual(original,(ROOT/'base'/f'Applicant_{label}.example.tex').read_bytes())
    def test_intake_logging_and_patterns(self):
        from unittest.mock import patch
        import new_job, log_job, email_patterns
        from common import read_csv
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            raw='Company: Example\nRole: Engineer\n  Preserve spacing!\n'
            for _ in range(2):
                with patch.object(new_job,'ROOT',root), patch.object(sys,'argv',['new_job.py','Example','Engineer']), patch.object(sys,'stdin',io.StringIO(raw)), contextlib.redirect_stdout(io.StringIO()):
                    new_job.main()
            jobs=list((root/'jobs').iterdir())
            self.assertEqual(len(jobs),2)
            self.assertTrue(all((j/'jd.txt').read_text()==raw for j in jobs))
            for company in ['Example','example']:
                with patch.object(log_job,'ROOT',root), patch.object(sys,'argv',['log_job.py','--company',company,'--role','Engineer']), contextlib.redirect_stdout(io.StringIO()): log_job.main()
            self.assertEqual(len(read_csv(root/'tracker/jobs.csv')),1)
            with patch.object(email_patterns,'ROOT',root), patch.object(sys,'argv',['email_patterns.py','--company','Example','--example-name','Jane Smith','--example-email','jane.smith@example.com','--names','Alex Jones']), contextlib.redirect_stdout(io.StringIO()): email_patterns.main()
            contact=read_csv(root/'outreach/contacts.csv')[0]
            self.assertEqual(contact['email'],'alex.jones@example.com')
            self.assertEqual(contact['verified'],'false')
    def test_backfill_routing_and_skips(self):
        from unittest.mock import patch
        from common import write_csv
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); jobs=root/'jobs'; jobs.mkdir()
            for name in ('selftest-data','scratch-demo','no-source','no-pdf','unknown','good','bad','tracker-base','conflict'):
                job=jobs/name; job.mkdir()
                if name!='no-source': (job/'resume.tex').write_text('source')
                if name!='no-pdf': (job/'resume.pdf').write_text('pdf')
            for name in ('good','bad','conflict'):
                (jobs/name/'report.md').write_text('- **Base Resume Selected:** Applicant_Data.tex')
            for name in ('tracker-base','conflict'):
                (jobs/name/'job.json').write_text(json.dumps({'company':name,'role':'Engineer'}))
            write_csv(root/'tracker/jobs.csv',['company','role','base_resume'],[
                {'company':name,'role':'Engineer','base_resume':'fullstack'} for name in ('tracker-base','conflict')])
            calls=[]
            def fake_verify(job,base):
                calls.append((job.name,base))
                if job.name=='bad': print('FAIL: exactly 1 page'); return 1
                return 0
            with patch.object(verifier,'verify',side_effect=fake_verify), contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(verifier.backfill(root),1)
            self.assertEqual(calls,[('bad','data'),('good','data'),('tracker-base','fullstack')])
            self.assertIn('FAIL bad: exactly 1 page',output.getvalue())
            self.assertIn('PASS=2, FAIL=1, SKIP=6',output.getvalue())
            for name in ('selftest-data','scratch-demo','no-source','no-pdf','unknown','conflict'):
                self.assertIn('SKIP '+name,output.getvalue())

    def test_parsing(self):
        self.assertEqual(verifier.arguments(r'\resumeItem{A {nested} 10\% metric}','resumeItem')[0][2],['A {nested} 10\\% metric'])
        self.assertEqual(verifier.rendered_counts('E DUCATION\nline\nT ECHNICAL S KILLS\nrow\n',['Education','Technical Skills']),{'Education':1,'Technical Skills':1})
if __name__=='__main__': unittest.main()

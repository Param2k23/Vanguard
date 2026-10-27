"""Offline export safety checks; no real Windows writes."""
import contextlib
import datetime
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import export_pdfs as exporter
from common import read_csv, write_csv

class ExportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.mount = self.root/'drive'; self.mount.mkdir()
        self.base = self.mount/'Resume'
        self.job = self.make_job('example-engineer', 'Engineer')

    def make_job(self, name, role):
        job = self.root/'jobs'/name; job.mkdir(parents=True)
        (job/'job.json').write_text(json.dumps({'company':'Example','role':role}))
        for file in ('jd.txt','report.md','resume.tex','answers.md','resume.aux','Applicant_Example.pdf'):
            (job/file).write_text(file)
        (job/'.verified.json').write_text(json.dumps({'pdf':'Applicant_Example.pdf','pdf_sha256':exporter.digest(job/'Applicant_Example.pdf'),'tex_sha256':exporter.digest(job/'resume.tex')}))
        return job

    def run_export(self, **kwargs):
        with contextlib.redirect_stdout(io.StringIO()) as output:
            result = exporter.export(self.root,self.base,mount=self.mount,today=datetime.date(2026,10,13),**kwargs)
        return result, output.getvalue()

    def test_date_and_cleanup(self):
        self.assertEqual(self.run_export(apply=True)[0],0)
        day = self.base/'2026-10-13'
        self.assertTrue((day/'Applicant_Example.pdf').exists())
        self.assertTrue((day/self.job.name/'report.md').exists())
        self.assertFalse((self.job/'resume.aux').exists())
        for file in ('resume.tex','jd.txt','report.md','answers.md'): self.assertTrue((self.job/file).exists())
        self.assertIn('already exported',self.run_export(apply=True)[1])

    def test_portable_local_export_without_drive_mount(self):
        with contextlib.redirect_stdout(io.StringIO()):
            code = exporter.export(self.root, self.root/'portable-export', apply=True)
        self.assertEqual(code, 0)
        self.assertEqual(len(list((self.root/'portable-export').rglob('*.pdf'))), 1)
        self.assertTrue((self.job/'resume.tex').exists())
        self.assertFalse((self.job/'Applicant_Example.pdf').exists())

    def test_collision(self):
        self.make_job('example-manager','Manager')
        self.run_export(apply=True)
        self.assertTrue((self.base/'2026-10-13'/'Applicant_Example_manager.pdf').exists())

    def test_hash_failure_no_delete(self):
        with patch.object(exporter,'matches',return_value=False):
            self.assertEqual(self.run_export(apply=True)[0],1)
        self.assertTrue((self.job/'Applicant_Example.pdf').exists())
        self.assertTrue((self.job/'resume.aux').exists())
        self.assertFalse((self.job/'.exported.json').exists())

    def test_unmounted_no_delete(self):
        with self.assertRaisesRegex(ValueError,'not mounted'):
            exporter.export(self.root,self.base,apply=True,mount=self.root/'absent')
        self.assertTrue((self.job/'Applicant_Example.pdf').exists())
        self.assertFalse(self.base.exists())

    def test_dry_run(self):
        before = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        result, output = self.run_export()
        after = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob('*') if p.is_file()}
        self.assertEqual(before,after)
        self.assertFalse(self.base.exists())
        self.assertIn('COPY',output); self.assertIn('DELETE',output)

    def test_stale_receipt(self):
        (self.job/'resume.tex').write_text('changed')
        self.assertIn('stale',self.run_export(apply=True)[1])
        self.assertFalse((self.job/'.verified.json').exists())
        self.assertTrue((self.job/'resume.aux').exists())

    def test_missing_or_invalid_marker(self):
        for marker in (None, '{}', 'invalid json'):
            receipt=self.job/'.verified.json'
            if marker is None: receipt.unlink(missing_ok=True)
            else: receipt.write_text(marker)
            code,output=self.run_export(apply=True)
            self.assertEqual(code,1)
            if marker is None: self.assertIn('no verification marker; run verify_resume.py --all',output)
            self.assertFalse(self.base.exists())
            self.assertTrue((self.job/'Applicant_Example.pdf').exists())
            self.assertTrue((self.job/'resume.aux').exists())

    def test_changed_pdf_removes_marker(self):
        (self.job/'Applicant_Example.pdf').write_text('changed PDF')
        code,output=self.run_export(apply=True)
        self.assertEqual(code,1); self.assertIn('hash changed',output)
        self.assertFalse((self.job/'.verified.json').exists())
        self.assertFalse(self.base.exists())
        self.assertTrue((self.job/'resume.aux').exists())

    def test_daily_export_skips_old_drafts_and_exports_verified_jobs(self):
        draft = self.root/'jobs'/'old-draft'
        draft.mkdir()
        (draft/'resume.tex').write_text('unfinished draft')
        before = {p.name: p.read_bytes() for p in draft.iterdir()}
        code, output = self.run_export(apply=True, skip_unverified=True)
        self.assertEqual(code, 0)
        self.assertIn('SKIP old-draft: unverified draft; left untouched', output)
        self.assertTrue((self.base/'2026-10-13'/'Applicant_Example.pdf').exists())
        self.assertEqual(before, {p.name: p.read_bytes() for p in draft.iterdir()})
        self.assertEqual(self.run_export(apply=True, skip_unverified=True)[0], 0)

    def test_daily_export_still_fails_on_invalid_verified_artifacts(self):
        for change in ('stale', 'invalid', 'copy_failure'):
            with self.subTest(change=change):
                original = (self.job/'resume.tex').read_text()
                # Restore a valid receipt before each independent failure.
                (self.job/'.verified.json').write_text(json.dumps({
                    'pdf': 'Applicant_Example.pdf',
                    'pdf_sha256': exporter.digest(self.job/'Applicant_Example.pdf'),
                    'tex_sha256': exporter.digest(self.job/'resume.tex')}))
                if change == 'stale':
                    (self.job/'resume.tex').write_text('modified source')
                elif change == 'invalid':
                    (self.job/'.verified.json').write_text('{}')
                with patch.object(exporter, 'matches', return_value=False):
                    code, _ = self.run_export(apply=True, skip_unverified=True)
                self.assertEqual(code, 1)
                self.assertTrue((self.job/'Applicant_Example.pdf').exists())
                self.assertFalse((self.job/'.exported.json').exists())
                (self.job/'resume.tex').write_text(original)

    def test_purge_updates_tracker(self):
        write_csv(self.root/'tracker/jobs.csv',['company','role'],[{'company':'Example','role':'Engineer'}])
        self.assertEqual(self.run_export(apply=True,purge=True)[0],0)
        self.assertFalse(self.job.exists())
        row = read_csv(self.root/'tracker/jobs.csv')[0]
        self.assertTrue(row['export_path'].endswith('Applicant_Example.pdf'))
        self.assertTrue(row['exported_at'])

if __name__ == '__main__': unittest.main()

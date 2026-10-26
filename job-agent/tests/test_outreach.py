"""Offline intake tests; no mail, credentials, or model calls."""
import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
from common import JOB_FIELDS, DRAFT_FIELDS, read_csv, write_csv
from outreach import process


class OutreachTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.job('example', 'Engineer')
        write_csv(self.root/'tracker/jobs.csv', JOB_FIELDS, [dict.fromkeys(JOB_FIELDS, '') | dict(company='Example', role='Engineer')])

    def job(self, slug, role):
        folder = self.root/'jobs'/slug
        folder.mkdir(parents=True)
        (folder/'job.json').write_text(json.dumps(dict(company='Example', role=role)))
        (folder/'resume.tex').write_text('Built a Python service')

    def run_intake(self, text, drafts=None, selected=()):
        with contextlib.redirect_stdout(io.StringIO()):
            process(self.root, text, drafts, selected)

    def draft(self, name='Alex Jones'):
        return dict(company='Example', name=name, subject='Engineer role',
                    body='Hi Alex, I am interested in the Engineer role. I built a Python service. Would you share what your team is working on? If you prefer not to hear from me, no worries. Thanks, Example',
                    resume_evidence='Built a Python service')

    def test_batch_inference_and_idempotence(self):
        text='Example: Alex Jones, Engineering Manager\nExample: Jane Smith, Recruiter, jane.smith@example.com (verified)'
        self.run_intake(text, [self.draft()])
        contacts=read_csv(self.root/'outreach/contacts.csv')
        self.assertEqual(contacts[0]['email'], 'alex.jones@example.com')
        self.assertEqual(contacts[0]['verified'], 'false')
        self.assertEqual(contacts[1]['verified'], 'true')
        drafts=read_csv(self.root/'outreach/drafts.csv')
        self.assertEqual(drafts[0]['status'], 'draft')
        drafts[0]['status']='sent'
        write_csv(self.root/'outreach/drafts.csv', DRAFT_FIELDS, drafts)
        self.run_intake(text, [self.draft()])
        self.assertEqual(read_csv(self.root/'outreach/drafts.csv'), drafts)

    def test_no_example_still_drafts(self):
        self.run_intake('Example: Alex Jones, Team Member', [self.draft()])
        self.assertEqual(read_csv(self.root/'outreach/drafts.csv')[0]['contact_email'], '')

    def test_ambiguity_requires_selection(self):
        self.job('example-again', 'Engineer')
        with self.assertRaisesRegex(ValueError, 'Which job'):
            self.run_intake('Example: Alex Jones, Recruiter')
        self.assertFalse((self.root/'outreach/contacts.csv').exists())
        self.run_intake('Example: Alex Jones, Recruiter', selected=['example'])

    def test_invalid_drafts_do_not_write(self):
        for change in [dict(body='word '*120), dict(resume_evidence='Invented work'), dict(body='Engineer role without opt-out')]:
            with self.assertRaises(ValueError):
                self.run_intake('Example: Alex Jones, Recruiter', [self.draft() | change])
            self.assertFalse((self.root/'outreach/drafts.csv').exists())


if __name__ == '__main__':
    unittest.main()

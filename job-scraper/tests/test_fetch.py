import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import yaml

from job_pipeline.content import extract
from job_pipeline.pipeline import Pipeline, ROOT, SessionExpired


CONFIG = yaml.safe_load((ROOT / 'config.yaml').read_text())
JOBRIGHT = 'https://jobright.ai/jobs/info/42'
EMPLOYER = 'https://careers.example.com/42'
JD = '<div class="job-description">' + (Path(__file__).parent / 'fixtures/job.html').read_text() + '</div>'


class FetchTests(unittest.TestCase):
    def row(self, status='collected', jobright_url=JOBRIGHT):
        return dict(id='42', company='Acme', title='Engineer', jobright_url=jobright_url,
                    source_url=EMPLOYER, status=status, jd_path='', notes='Old CAPTCHA')

    def fetch(self, rows, job_ids=None, reasons=None, error=None):
        page = Mock(url=JOBRIGHT)
        page.content.return_value = JD
        pipeline = Pipeline(page, CONFIG)
        pipeline.goto = Mock(return_value=200, side_effect=error)
        pipeline.inspect = Mock(return_value=('', reasons or []))
        pipeline.artifacts = Mock()
        with tempfile.TemporaryDirectory() as tmp, \
                patch('job_pipeline.pipeline.ROOT', Path(tmp)), \
                patch('job_pipeline.pipeline.read_rows', return_value=rows), \
                patch('job_pipeline.pipeline.write_rows'):
            pipeline.fetch(job_ids)
            if rows[0]['status'] == 'jd_fetched' and rows[0]['jd_path']:
                self.assertIn('Requirements', (Path(tmp) / rows[0]['jd_path']).read_text())
        return pipeline

    def test_uses_jobright_without_visiting_employer(self):
        row = self.row()
        pipeline = self.fetch([row])
        pipeline.goto.assert_called_once_with(JOBRIGHT)
        self.assertEqual(row['status'], 'jd_fetched')
        self.assertEqual(row['source_url'], EMPLOYER)

    def test_existing_review_is_retried_and_cleared(self):
        row = self.row('needs_review')
        self.fetch([row])
        self.assertEqual(row['status'], 'jd_fetched')
        self.assertNotIn('CAPTCHA', row['notes'])

    def test_fetched_and_unselected_jobs_are_skipped(self):
        for row, ids in [(self.row('jd_fetched'), None), (self.row('needs_review'), {'other'})]:
            self.fetch([row], ids).goto.assert_not_called()

    def test_non_jobright_row_uses_original_posting(self):
        self.fetch([self.row(jobright_url='')]).goto.assert_called_once_with(EMPLOYER)

    def test_jobright_block_remains_review_without_employer_navigation(self):
        row = self.row()
        pipeline = self.fetch([row], reasons=['Captcha or verification'])
        pipeline.goto.assert_called_once_with(JOBRIGHT)
        self.assertEqual(row['status'], 'needs_review')
        self.assertEqual(row['jd_path'], '')

    def test_expired_jobright_session_stops_batch(self):
        with self.assertRaises(SessionExpired):
            self.fetch([self.row()], error=SessionExpired())


class JobrightExtractionTests(unittest.TestCase):
    def test_combines_all_jd_sections_and_excludes_other_panels(self):
        html = '''<div class="index_jobDetailContent__abc">
          <section><h2>Insider Connection</h2>Find any email</section>
          <section><h2>Responsibilities</h2>Build APIs</section>
          <section><h2>Qualification</h2>Python and SQL</section>
          <section><h2>Benefits</h2>Paid holidays</section>
        </div><main>Recommended jobs</main>'''
        text, method = extract(html, JOBRIGHT, CONFIG['selectors'])
        self.assertEqual(method, 'jobright')
        for content in ('Build APIs', 'Python and SQL', 'Paid holidays'):
            self.assertIn(content, text)
        self.assertNotIn('Find any email', text)
        self.assertNotIn('Recommended jobs', text)

    def test_feed_content_cannot_be_saved_as_a_jd(self):
        text, _ = extract('<main>Requirements ' + 'recommended jobs ' * 100 + '</main>',
                          JOBRIGHT, CONFIG['selectors'])
        self.assertEqual(text, '')

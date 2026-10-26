import unittest
from pathlib import Path
import yaml
from bs4 import BeautifulSoup
from job_pipeline.content import clean_text, duplicate, extract, review_reasons, slug
from job_pipeline.pipeline import ROOT

FIXTURES = Path(__file__).parent / 'fixtures'
SELECTORS = yaml.safe_load((ROOT / 'config.yaml').read_text())['selectors']


class ContentTests(unittest.TestCase):
    def test_slug(self):
        self.assertEqual(slug('Acme / Dév & Co.'), 'acme-dev-co')
        self.assertEqual(slug('你好'), 'unknown')
        self.assertLessEqual(len(slug('a' * 100)), 70)

    def test_dedupe(self):
        row = dict(company=' ACME  Inc ', title='Senior Engineer', jobright_url='https://example.com/job/1/', source_url='https://ats.com/42')
        self.assertTrue(duplicate(dict(row, company='Other', jobright_url='https://example.com/job/1#apply'), [row]))
        self.assertTrue(duplicate(dict(row, company='acme inc', title=' senior engineer ', jobright_url='https://new.com', source_url=''), [row]))
        self.assertFalse(duplicate(dict(row, title='Designer', jobright_url='https://new.com', source_url=''), [row]))

    def test_cleaning(self):
        text, method = extract((FIXTURES / 'job.html').read_text(), 'https://example.com', SELECTORS)
        self.assertEqual(method, 'main')
        self.assertNotIn('Privacy policy', text)
        self.assertNotIn('equal opportunity employer', text)
        self.assertNotIn('Home', text)
        self.assertIn('Python, SQL, Linux', text)
        self.assertIn('Responsibilities', text)
        self.assertEqual(review_reasons(text, text, 'https://example.com'), [])
        self.assertIn('Benefits include training', clean_text('Benefits include training\nRequirements: Python'))

    def test_structured_priority(self):
        text, method = extract((FIXTURES / 'structured.html').read_text(), 'https://example.com', SELECTORS)
        self.assertEqual(method, 'json-ld')
        self.assertIn('Python and SQL', text)
        self.assertNotIn('Wrong fallback', text)

    def test_review(self):
        for fixture, expected in [('login.html', 'Page requires login'), ('verification.html', 'Captcha or verification; try --headed if headless')]:
            raw = BeautifulSoup((FIXTURES / fixture).read_text(), 'html.parser').get_text()
            self.assertIn(expected, review_reasons('', raw, 'https://example.com'))
        self.assertIn('Description under 400 characters', review_reasons('Requirements: SQL', '', 'https://example.com'))
        self.assertIn('No requirements or responsibilities section detected', review_reasons('x' * 450, '', 'https://example.com'))
        self.assertTrue(any('blocked' in r for r in review_reasons('Requirements ' * 50, '', 'https://example.com', 403)))
        self.assertIn('Page requires login', review_reasons('', '', 'https://example.com/login'))
        self.assertTrue(any('verification' in r for r in review_reasons('', '', 'https://example.com', verification_visible=True)))

    def test_ats_and_fallback(self):
        for host, selector_html, method in [('boards.greenhouse.io', '<div id="job_description">Requirements: Python</div>', 'greenhouse'), ('jobs.lever.co', '<div class="section-wrapper page-full-width">Requirements: Python</div>', 'lever'), ('jobs.ashbyhq.com', '<div data-testid="job-description">Requirements: Python</div>', 'ashby'), ('acme.myworkdayjobs.com', '<div data-automation-id="jobPostingDescription">Requirements: Python</div>', 'workday'), ('jobs.smartrecruiters.com', '<div class="job-description">Requirements: Python</div>', 'smartrecruiters'), ('jobright.ai', '<div class="job-description">Requirements: Python</div>', 'jobright')]:
            with self.subTest(host=host):
                self.assertEqual(extract(selector_html, 'https://' + host, SELECTORS)[1], method)
        self.assertEqual(extract('<main>short</main><article>Longer requirements block</article>', 'https://example.com', SELECTORS)[0], 'Longer requirements block')


if __name__ == '__main__':
    unittest.main()

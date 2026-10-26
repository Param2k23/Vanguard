import copy
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
import yaml
from bs4 import BeautifulSoup
from job_pipeline.filters import skip_reason
from job_pipeline.pipeline import Pipeline, ROOT

CONFIG = yaml.safe_load((ROOT / 'config.yaml').read_text())


class FilterTests(unittest.TestCase):
    def test_feed_and_detail_labels(self):
        soup = BeautifulSoup((Path(__file__).parent / 'fixtures/filtered_feed.html').read_text(), 'html.parser')
        sel = CONFIG['selectors']
        reasons = []
        for card in soup.select(sel['cards']):
            reasons.append(skip_reason([n.get_text() for n in card.select(sel['card_badges'])],
                                       [n.get_text() for n in card.select(sel['card_company_types'])], {}))
        self.assertEqual(reasons, ['reposted', 'consulting company', ''])
        self.assertEqual(skip_reason([], [n.get_text() for n in soup.select(sel['detail_company_types'])], {}), 'consulting company')

    def test_case_variants_missing_labels_and_toggles(self):
        for label in ['REPOSTED', 'Re-posted yesterday', 'Re posted today']:
            self.assertEqual(skip_reason([label], [], {}), 'reposted')
        self.assertEqual(skip_reason([], ['IT CONSULTING'], {}), 'consulting company')
        self.assertEqual(skip_reason([], [], {}), '')
        self.assertEqual(skip_reason(['Posted today'], ['Software'], {}), '')
        self.assertEqual(skip_reason(['Reposted'], ['Consulting'], dict(skip_reposted=False, skip_consulting=False)), '')

    def test_us_location_filter(self):
        for location in ['United States', 'Remote, USA', 'U.S.', 'Mountain View, CA',
                         'Phoenix, Arizona', 'Washington, DC', 'New York', 'Austin, TX (Remote)']:
            with self.subTest(location=location):
                self.assertEqual(skip_reason([], [], {}, locations=[location]), '')
        for location in ['Toronto, ON, Canada', 'London, UK', 'Bengaluru, India',
                         'Vancouver, BC', 'Remote - Canada', 'London', 'Georgia, Europe']:
            with self.subTest(location=location):
                self.assertEqual(skip_reason([], [], {}, locations=[location]), 'outside US')
        for location in ['', 'Remote', 'Not specified']:
            self.assertEqual(skip_reason([], [], {}, locations=[location]), '')
        self.assertEqual(skip_reason([], [], {}, locations=['Toronto, Canada', 'Boston, MA']), '')
        self.assertEqual(skip_reason([], [], {'us_only': False}, locations=['Canada']), '')

    def test_education_employers(self):
        for company in ['Stanford University', 'Massachusetts Institute of Technology',
                        'Example School District', 'Example College', 'National Institutes of Health']:
            self.assertEqual(skip_reason([], [], {}, companies=[company]), 'educational institution')
        for label in ['Higher Education', 'Education Management', 'Schools', 'Research Institute']:
            self.assertEqual(skip_reason([], [label], {}), 'educational institution')
        self.assertEqual(skip_reason(['4 school alumni'], ['Software'], {}, companies=['Acme']), '')
        self.assertEqual(skip_reason([], ['Software'], {}, companies=['Schoology']), '')
        self.assertEqual(skip_reason([], ['University'], {'skip_educational': False}), '')

    def test_new_detail_filters_never_append(self):
        for reason in ['outside US', 'educational institution']:
            pipeline, card = self.collector()
            pipeline.filter_reason = MagicMock(side_effect=['', reason])
            with patch('job_pipeline.pipeline.read_rows', return_value=[]), patch('job_pipeline.pipeline.write_rows') as write:
                pipeline.collect(1)
            write.assert_not_called()

    def collector(self):
        config = copy.deepcopy(CONFIG)
        config['max_scroll_attempts'] = 0
        pipeline = Pipeline(MagicMock(), config)
        pipeline.page.url = config['feed_url']
        pipeline.goto = MagicMock(return_value=200)
        pipeline.inspect = MagicMock(return_value=('', []))
        pipeline.card_keys = MagicMock(return_value=frozenset({'job'}))
        pipeline.feed_scroll = MagicMock(return_value={'top': 0})
        pipeline.artifacts = MagicMock()
        cards = MagicMock()
        card = cards.nth.return_value
        card.locator.return_value.first.inner_text.return_value = 'Example'
        card.locator.return_value.first.get_attribute.return_value = '/jobs/info/example'
        pipeline.wait_for_cards = MagicMock(return_value=(cards, 1))
        return pipeline, card

    def test_feed_filter_runs_before_field_reads_or_detail_navigation(self):
        pipeline, card = self.collector()
        pipeline.filter_reason = MagicMock(return_value='reposted')
        with patch('job_pipeline.pipeline.read_rows', return_value=[]), patch('job_pipeline.pipeline.write_rows') as write:
            pipeline.collect(1)  # All filtered is valid; no missing-field error.
        card.locator.assert_not_called()
        self.assertEqual(pipeline.goto.call_count, 1)
        write.assert_not_called()

    def test_consulting_detail_never_appended(self):
        pipeline, card = self.collector()
        pipeline.filter_reason = MagicMock(side_effect=['', 'consulting company'])
        with patch('job_pipeline.pipeline.read_rows', return_value=[]), patch('job_pipeline.pipeline.write_rows') as write:
            pipeline.collect(1)
        self.assertEqual(pipeline.goto.call_count, 2)
        pipeline.filter_reason.assert_any_call(pipeline.page, detail=True)
        write.assert_not_called()

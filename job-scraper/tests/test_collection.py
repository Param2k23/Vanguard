import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch
from urllib.parse import urljoin

import yaml
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from job_pipeline.pipeline import Pipeline, ROOT, SessionExpired

CONFIG = yaml.safe_load((ROOT / 'config.yaml').read_text())


class CollectionTests(unittest.TestCase):
    def test_observed_feed_structure(self):
        soup = BeautifulSoup((Path(__file__).parent / 'fixtures/feed.html').read_text(), 'html.parser')
        selectors = CONFIG['selectors']
        cards = soup.select(selectors['cards'])
        self.assertEqual(len(cards), 2)
        self.assertEqual(len(soup.select(selectors['scroll_container'])), 1)
        extracted = []
        for card in cards:
            links = card.select(selectors['link'])
            self.assertEqual(len(links), 1)  # Excludes the duplicate score link.
            extracted.append((card.select_one(selectors['title']).get_text(),
                              card.select_one(selectors['company']).get_text(),
                              urljoin(CONFIG['feed_url'], links[0]['href'])))
        self.assertEqual(extracted[0], ('Associate Software Engineer, Core Infrastructure - Moveworks',
                                      'ServiceNow', 'https://jobright.ai/jobs/info/fake1'))
        self.assertEqual(extracted[1][1], 'Brillio')

    def pipeline(self, count, url='https://jobright.ai/jobs/recommend', raw='Recommended jobs'):
        page = MagicMock()
        page.url = url
        page.title.return_value = 'Job Recommendations | Jobright AI'
        page.locator.return_value.count.return_value = count
        page.locator.return_value.inner_text.return_value = raw
        pipeline = Pipeline(page, CONFIG)
        pipeline.visible = MagicMock(return_value=False)
        pipeline.artifacts = MagicMock()
        return pipeline

    def test_wait_and_card_count_logged(self):
        pipeline = self.pipeline(7)
        with self.assertLogs(level='INFO') as logs:
            _, count = pipeline.wait_for_cards(2)
        self.assertEqual(count, 7)
        pipeline.page.locator.return_value.first.wait_for.assert_called_once_with(
            state='attached', timeout=CONFIG['card_wait_timeout_ms'])
        self.assertIn('scroll step 2: 7 cards found', '\n'.join(logs.output))

    def test_zero_cards_fails_with_diagnostics(self):
        pipeline = self.pipeline(0)
        pipeline.page.locator.return_value.first.wait_for.side_effect = PlaywrightTimeoutError('timeout')
        with self.assertRaisesRegex(RuntimeError, 'Zero job cards') as error:
            pipeline.wait_for_cards(0)
        message = str(error.exception)
        self.assertIn('URL: https://jobright.ai/jobs/recommend', message)
        self.assertIn('Page title: Job Recommendations | Jobright AI', message)
        self.assertIn('Looks like a login page: no', message)
        pipeline.artifacts.assert_called_once()

    def test_zero_cards_login_stops(self):
        pipeline = self.pipeline(0, 'https://jobright.ai/login', 'Sign in to view jobs')
        with self.assertRaisesRegex(SessionExpired, 'Looks like a login page: yes.*--login'):
            pipeline.wait_for_cards(0)

    def test_collect_does_not_silently_succeed_with_zero_cards(self):
        pipeline = self.pipeline(0)
        pipeline.goto = MagicMock(return_value=200)
        with patch('job_pipeline.pipeline.read_rows', return_value=[]), patch('job_pipeline.pipeline.write_rows') as write:
            with self.assertRaisesRegex(RuntimeError, 'Zero job cards'):
                pipeline.collect(1)
            write.assert_not_called()

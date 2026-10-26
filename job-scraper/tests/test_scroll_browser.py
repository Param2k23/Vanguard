"""Optional browser regression tests using only local, fabricated HTML."""
import os
import unittest
import yaml
from playwright.sync_api import sync_playwright
from job_pipeline.pipeline import Pipeline, ROOT


@unittest.skipUnless(os.environ.get('RUN_BROWSER_TESTS') == '1', 'Set RUN_BROWSER_TESTS=1 to test local scroll geometry in Chromium')
class BrowserScrollTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.pw = sync_playwright().start()
        cls.browser = cls.pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.pw.stop()

    def setUp(self):
        self.page = self.browser.new_page(viewport={'width': 800, 'height': 600})
        self.page.set_content('''<style>
            body {margin:0} #outer {height:200px; overflow-y:auto}
            .index_jobs-list-scrollable__fixture {height:2000px}
            </style><div id="outer"><div class="index_jobs-list-scrollable__fixture">
            <div data-tut="jobs-card-match-score" id="first"><h2>Engineer</h2></div>
            </div></div>''')
        self.pipeline = Pipeline(self.page, yaml.safe_load((ROOT / 'config.yaml').read_text()))

    def tearDown(self):
        self.page.close()

    def test_non_scrollable_wrapper_falls_back_to_ancestor(self):
        state = self.pipeline.feed_scroll('advance')
        self.assertEqual(state['target'], 'card ancestor')
        self.assertEqual(state['top'], 160)
        self.assertEqual(self.page.locator('#outer').evaluate('(el) => el.scrollTop'), 160)
        self.assertEqual(self.pipeline.feed_scroll('restore', 80)['top'], 80)

    def test_scrollable_hint_is_used(self):
        self.page.add_style_tag(content='.index_jobs-list-scrollable__fixture {height:200px; overflow-y:auto} [data-tut] {height:2000px}')
        state = self.pipeline.feed_scroll('advance')
        self.assertEqual(state['target'], 'configured panel')
        self.assertEqual(state['top'], 160)

    def test_document_fallback(self):
        self.page.add_style_tag(content='#outer {height:auto; overflow:visible}')
        state = self.pipeline.feed_scroll('advance')
        self.assertEqual(state['target'], 'document')
        self.assertGreater(state['top'], 0)

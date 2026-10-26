import json
import unittest

from job_pipeline.content import original_posting_url


class OriginalPostingTests(unittest.TestCase):
    def page(self, data, next_data=False):
        data = {'jobResult': data}
        if next_data:
            data = {'props': {'pageProps': {'dataSource': data}}}
        script_id = '__NEXT_DATA__' if next_data else 'jobright-helper-job-detail-info'
        return f'<script id="{script_id}" type="application/json">{json.dumps(data)}</script>'

    def test_button_only_page_uses_embedded_apply_link(self):
        url = 'https://careers.example.com/jobs/42?source=jobright&lang=en'
        html = '<button>APPLY NOW</button>' + self.page({
            'applyLink': url, 'originalUrl': 'https://other.example/jobs/42'})
        self.assertEqual(original_posting_url(html), url)

    def test_next_data_original_url_fallback(self):
        url = 'https://careers.example.com/jobs/42'
        self.assertEqual(original_posting_url(self.page({
            'applyLink': 'https://jobright.ai/jobs/info/42', 'originalUrl': url}, True)), url)

    def test_missing_malformed_or_unsafe_data(self):
        for html in ('<button>Apply</button>',
                     '<script id="jobright-helper-job-detail-info">invalid</script>',
                     self.page({'applyLink': 'javascript:alert(1)'}),
                     self.page({'originalUrl': 'https://sub.jobright.ai/jobs/info/42'}),
                     self.page({'applyLink': None}), self.page([])):
            with self.subTest(html=html):
                self.assertEqual(original_posting_url(html), '')

    def test_does_not_use_unrelated_recommended_job(self):
        self.assertEqual(original_posting_url(self.page({
            'recommendations': [{'applyLink': 'https://other.example/jobs/99'}]})), '')

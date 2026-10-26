import copy
import unittest
from unittest.mock import MagicMock, patch
import yaml
from job_pipeline.pipeline import Pipeline, ROOT

CONFIG = yaml.safe_load((ROOT / 'config.yaml').read_text())


class ScrollingTests(unittest.TestCase):
    def collector(self, batches):
        pipeline = Pipeline(MagicMock(), copy.deepcopy(CONFIG))
        pipeline.page.url = CONFIG['feed_url']
        pipeline.goto = MagicMock(return_value=200)
        pipeline.inspect = MagicMock(return_value=('', []))
        pipeline.feed_scroll = MagicMock(return_value={'top': 0})
        pipeline.advance_feed = MagicMock()
        pipeline.artifacts = MagicMock()
        cards = MagicMock()
        cards.nth.return_value.locator.return_value.first.inner_text.return_value = 'Example'
        cards.nth.return_value.locator.return_value.first.get_attribute.return_value = '/jobs/info/example'
        pipeline.card_keys = MagicMock()
        sequence = iter(batches)

        def wait(attempt):
            pipeline.card_keys.return_value = frozenset(next(sequence))
            return cards, 1
        pipeline.wait_for_cards = MagicMock(side_effect=wait)
        return pipeline

    def test_same_cards_stop_after_three_scrolls(self):
        pipeline = self.collector([{'a'}] * 4)
        with patch('job_pipeline.pipeline.read_rows', return_value=[]), patch('job_pipeline.pipeline.duplicate', return_value=True), patch('job_pipeline.pipeline.write_rows') as write:
            pipeline.collect(8)
        self.assertEqual(pipeline.advance_feed.call_count, 3)
        self.assertEqual(pipeline.wait_for_cards.call_count, 4)
        write.assert_not_called()
        self.assertTrue(any('scroll-stalled-' in str(call) for call in pipeline.artifacts.call_args_list))

    def test_replaced_cards_with_same_count_reset_stall_counter(self):
        pipeline = self.collector([{'a'}, {'a'}, {'b'}, {'b'}, {'b'}, {'b'}])
        with patch('job_pipeline.pipeline.read_rows', return_value=[]), patch('job_pipeline.pipeline.duplicate', return_value=True):
            pipeline.collect(8)
        self.assertEqual(pipeline.advance_feed.call_count, 5)
        self.assertEqual(pipeline.wait_for_cards.call_count, 6)

    def test_advance_waits_for_changed_card_identity(self):
        pipeline = Pipeline(MagicMock(), CONFIG)
        pipeline.feed_scroll = MagicMock(return_value=dict(target='card ancestor', before=0, top=400, height=500, total=3000, bottom=False))
        pipeline.card_keys = MagicMock(side_effect=[frozenset({'a'}), frozenset({'b'})])
        pipeline.advance_feed(frozenset({'a'}))
        pipeline.feed_scroll.assert_called_once_with('advance')
        self.assertEqual(pipeline.card_keys.call_count, 2)
        pipeline.page.wait_for_timeout.assert_any_call(250)

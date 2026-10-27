"""Offline checks for scheduled execution, checkpoints and Arizona slot handling."""
import argparse
from contextlib import redirect_stdout
from datetime import datetime
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import scheduled_run as schedule
import vanguard


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state_path = self.root / 'state.json'
        self.config = {'run_dir': self.root / 'runs'}
        self.config['run_dir'].mkdir()
        self.now = datetime(2026, 10, 31, 7, tzinfo=schedule.TIMEZONE)

    def test_slots_include_weekends_and_before_morning(self):
        self.assertEqual(schedule.slot_at(self.now), '2026-10-31T06:00:00-07:00')
        self.assertEqual(schedule.slot_at(self.now.replace(hour=2)), '2026-10-30T12:00:00-07:00')
        self.assertEqual(schedule.next_slot(self.now), '2026-10-31T12:00:00-07:00')
        self.assertEqual(schedule.next_slot(self.now.replace(hour=13)), '2026-11-01T06:00:00-07:00')

    def test_arizona_does_not_shift_when_other_states_change_dst(self):
        for month, day in ((3, 8), (11, 1)):
            now = datetime(2026, month, day, 7, tzinfo=schedule.TIMEZONE)
            self.assertTrue(schedule.slot_at(now).endswith('06:00:00-07:00'))

    def test_successful_slot_not_run_twice(self):
        state = {}
        with patch.object(vanguard, 'run', return_value=0) as run, redirect_stdout(io.StringIO()):
            self.assertEqual(schedule.execute(self.config, self.root, self.state_path, state, self.now), 0)
            self.assertEqual(schedule.execute(self.config, self.root, self.state_path, state, self.now), 0)
        self.assertEqual(run.call_count, 1)
        self.assertIn('last_success_at', json.loads(self.state_path.read_text()))

    def test_failed_resume_retries_without_collecting_more_jobs(self):
        state = {}
        def failure(config, args):
            report = {'stages': {'scrape': {'status': 'completed'}, 'resumes': {'status': 'failed'}}}
            (config['run_dir'] / 'test.json').write_text(json.dumps(report))
            return 1
        with patch.object(vanguard, 'run', side_effect=failure):
            self.assertEqual(schedule.execute(self.config, self.root, self.state_path, state, self.now), 1)
        self.assertEqual(state['current']['next_stage'], 'resumes')
        with patch.object(vanguard, 'run', return_value=0) as run:
            self.assertEqual(schedule.execute(self.config, self.root, self.state_path, state, self.now), 0)
        self.assertEqual(run.call_args.args[1].from_stage, 'resumes')

    def test_unfinished_export_finished_before_next_slots_new_collection(self):
        state = {'current': {'slot': 'older', 'status': 'failed', 'next_stage': 'export', 'attempt': 1}}
        with patch.object(vanguard, 'run', return_value=0) as run:
            schedule.execute(self.config, self.root, self.state_path, state, self.now)
        self.assertEqual(run.call_args.args[1].from_stage, 'export')
        self.assertEqual(state['current']['slot'], schedule.slot_at(self.now))

    def test_resumed_export_failure_stays_at_export_across_slots(self):
        state = {'current': {'slot': 'older', 'status': 'failed', 'next_stage': 'export'}}
        def failure(config, args):
            self.assertEqual(args.from_stage, 'export')
            report = {'stages': {'export': {'status': 'failed'}}}
            (config['run_dir'] / 'export.json').write_text(json.dumps(report))
            return 1
        with patch.object(vanguard, 'run', side_effect=failure):
            self.assertEqual(schedule.execute(self.config, self.root, self.state_path, state, self.now), 1)
        self.assertEqual(state['current']['next_stage'], 'export')
        with patch.object(vanguard, 'run', return_value=0) as run:
            schedule.execute(self.config, self.root, self.state_path, state, self.now.replace(hour=13))
        self.assertEqual(run.call_args.args[1].from_stage, 'export')

    def test_resumed_generation_then_failed_export_does_not_restart_scraping(self):
        state = {'current': {'slot': 'older', 'status': 'failed', 'next_stage': 'resumes'}}
        def failure(config, args):
            report = {'stages': {'resumes': {'status': 'completed'}, 'export': {'status': 'failed'}}}
            (config['run_dir'] / 'resumed.json').write_text(json.dumps(report))
            return 1
        with patch.object(vanguard, 'run', side_effect=failure):
            schedule.execute(self.config, self.root, self.state_path, state, self.now)
        self.assertEqual(state['current']['next_stage'], 'export')

    def test_stage_checkpoint_persisted_before_process_interruption(self):
        state = {}
        def interrupted(config, args):
            args.stage_callback('resumes', config['run_dir'] / 'current.json')
            saved = json.loads(self.state_path.read_text())
            self.assertEqual(saved['current']['next_stage'], 'resumes')
            raise KeyboardInterrupt
        with patch.object(vanguard, 'run', side_effect=interrupted), self.assertRaises(KeyboardInterrupt):
            schedule.execute(self.config, self.root, self.state_path, state, self.now)
        self.assertEqual(state['current']['status'], 'failed')
        # A lost process retains the checkpoint even if its finally block never ran.
        self.assertEqual(state['current']['next_stage'], 'resumes')

    def test_dry_run_does_not_write_scheduler_state(self):
        with patch.object(vanguard, 'run', return_value=0):
            schedule.execute(self.config, self.root, self.state_path, {}, self.now, dry_run=True)
        self.assertFalse(self.state_path.exists())

    def test_runtime_finds_configured_node_without_interactive_shell(self):
        env = schedule.runtime_environment({'tool_bin': '/example/node/bin'})
        self.assertIn('/example/node/bin', env['PATH'].split(':'))


if __name__ == '__main__':
    unittest.main()

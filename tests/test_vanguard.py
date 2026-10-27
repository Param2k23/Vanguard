"""Offline orchestration regression tests; no browser, Codex or account traffic."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import vanguard


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.scraper = self.root / 'scraper'
        self.scraper.mkdir()
        self.config = dict(scraper_dir=self.scraper, agent_dir=self.root / 'agent',
                           export_dir=self.root / 'exports', run_dir=self.root / 'runs',
                           email='developer@sample.org', github_user='example-user',
                           resume_prefix='Applicant', limit=15)
        self.args = argparse.Namespace(command='run', from_stage='scrape', limit=None,
                                       headed=False, attach=None, dry_run=False)

    def run_pipeline(self, codes):
        calls = []
        def execute(command, **kwargs):
            calls.append((command, kwargs))
            return subprocess.CompletedProcess(command, codes[len(calls) - 1])
        with patch.object(vanguard, 'check', return_value=[]), patch.object(
            vanguard.subprocess, 'run', side_effect=execute), contextlib.redirect_stdout(io.StringIO()):
            result = vanguard.run(self.config, self.args)
        return result, calls

    def report(self):
        return json.loads(next(self.config['run_dir'].glob('*.json')).read_text())

    def test_runs_in_order_and_defers_inner_export(self):
        code, calls = self.run_pipeline([0, 0, 0])
        self.assertEqual(code, 0)
        self.assertEqual([Path(command[1]).name for command, _ in calls],
                         ['scrape.py', 'generate_resumes.py', 'export_pdfs.py'])
        self.assertNotIn('--limit', calls[1][0])
        self.assertIn('--apply', calls[2][0])
        self.assertIn('--skip-unverified', calls[2][0])
        self.assertEqual(calls[1][1]['env']['RESUME_EXPORT_APPLY'], '0')
        self.assertEqual(calls[1][1]['env']['VANGUARD_DEFER_EXPORT'], '1')
        self.assertEqual(self.report()['status'], 'completed')

    def test_failure_stops_later_stages(self):
        for codes in ([7], [0, 9]):
            with self.subTest(codes=codes):
                code, calls = self.run_pipeline(codes)
                self.assertEqual(code, codes[-1])
                self.assertEqual(len(calls), len(codes))
        reports = [json.loads(p.read_text()) for p in self.config['run_dir'].glob('*.json')]
        self.assertTrue(all(r['status'] == 'failed' for r in reports))

    def test_checkpoint_callback_precedes_each_stage(self):
        events = []
        def checkpoint(stage, path):
            self.assertEqual(json.loads(path.read_text())['stages'][stage]['status'], 'running')
            events.append(('checkpoint', stage))
        def execute(command, **kwargs):
            events.append(('execute', Path(command[1]).name))
            return subprocess.CompletedProcess(command, 0)
        self.args.stage_callback = checkpoint
        with patch.object(vanguard, 'check', return_value=[]), patch.object(
            vanguard.subprocess, 'run', side_effect=execute), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vanguard.run(self.config, self.args), 0)
        self.assertEqual(events, [('checkpoint', 'scrape'), ('execute', 'scrape.py'),
                                  ('checkpoint', 'resumes'), ('execute', 'generate_resumes.py'),
                                  ('checkpoint', 'export'), ('execute', 'export_pdfs.py')])

    def test_restart_at_export(self):
        self.args.from_stage = 'export'
        code, calls = self.run_pipeline([0])
        self.assertEqual(code, 0)
        self.assertEqual(Path(calls[0][0][1]).name, 'export_pdfs.py')
        self.assertEqual(list(self.report()['stages']), ['export'])

    def test_dry_run_does_not_write_or_execute(self):
        self.args.dry_run = True
        with patch.object(vanguard.subprocess, 'run') as execute, contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(vanguard.run(self.config, self.args), 0)
        execute.assert_not_called()
        self.assertEqual(list(self.scraper.iterdir()), [])
        self.assertFalse(self.config['run_dir'].exists())

    def test_concurrent_run_rejected_then_lock_released(self):
        with vanguard.pipeline_lock(self.config):
            with self.assertRaisesRegex(ValueError, 'Another Vanguard'):
                with vanguard.pipeline_lock(self.config):
                    self.fail('must not acquire a second lock')
        with vanguard.pipeline_lock(self.config):
            pass

    def test_preflight_fails_before_execution(self):
        with patch.object(vanguard, 'check', return_value=['missing tool']), patch.object(
            vanguard.subprocess, 'run') as execute:
            with self.assertRaisesRegex(ValueError, 'missing tool'):
                vanguard.run(self.config, self.args)
        execute.assert_not_called()
        self.assertFalse(self.config['run_dir'].exists())

    def test_interruption_recorded_and_lock_released(self):
        with patch.object(vanguard, 'check', return_value=[]), patch.object(
            vanguard.subprocess, 'run', side_effect=KeyboardInterrupt), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):
                vanguard.run(self.config, self.args)
        self.assertEqual(self.report()['status'], 'interrupted')
        with vanguard.pipeline_lock(self.config):
            pass

    def test_relative_config_paths_and_positive_limit(self):
        path = self.root / 'local.yaml'
        path.write_text('email: developer@sample.org\ngithub_user: example-user\nresume_prefix: Applicant\n')
        config = vanguard.load_config(path)
        self.assertEqual(config['scraper_dir'], vanguard.ROOT / 'job-scraper')
        path.write_text(path.read_text() + 'limit: 0\n')
        with self.assertRaisesRegex(ValueError, 'positive integer'):
            vanguard.load_config(path)

    def test_mount_checked_before_scraping(self):
        self.config['export_dir'] = Path('/mnt/d/Resume')
        with patch.object(vanguard.os.path, 'ismount', return_value=False):
            errors = vanguard.check(self.config, ['export'])
        self.assertTrue(any('not mounted' in error for error in errors))
        export = vanguard.commands(self.config, self.args)['export']
        self.assertEqual(export[-2:], ['--mount', '/mnt/d'])


if __name__ == '__main__':
    unittest.main()

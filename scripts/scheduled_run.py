#!/usr/bin/env python3
"""Noninteractive launcher with per-slot checkpoints, logs and duplicate protection."""
import argparse
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import vanguard

TIMEZONE = ZoneInfo('America/Phoenix')
HOURS = (6, 12)


def slot_at(now):
    now = now.astimezone(TIMEZONE)
    candidates = [now.replace(hour=hour, minute=0, second=0, microsecond=0) for hour in HOURS]
    candidates += [(now - timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0) for hour in HOURS]
    return max(value for value in candidates if value <= now).isoformat()


def next_slot(now):
    now = now.astimezone(TIMEZONE)
    return min((now + timedelta(days=day)).replace(hour=hour, minute=0, second=0, microsecond=0)
               for day in (0, 1) for hour in HOURS
               if (now + timedelta(days=day)).replace(hour=hour, minute=0, second=0, microsecond=0) > now).isoformat()


def runtime_environment(settings):
    env = dict(os.environ)
    bins = [ROOT / '.venv/bin', Path.home() / '.local/bin']
    if settings.get('tool_bin'):
        bins.append(Path(settings['tool_bin']).expanduser())
    elif not shutil.which('codex'):
        # A task does not source interactive .bashrc, where nvm is usually initialized.
        choices = list((Path.home() / '.nvm/versions/node').glob('*/bin'))
        def version(path):
            try:
                return tuple(int(part) for part in path.parent.name.lstrip('v').split('.'))
            except ValueError:
                return (0,)
        bins += sorted(choices, key=version, reverse=True)
    env['PATH'] = os.pathsep.join(str(path) for path in bins) + os.pathsep + env.get('PATH', '/usr/bin:/bin')
    env['PYTHONUNBUFFERED'] = '1'
    return env


@contextmanager
def lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / 'scheduler.lock').open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another scheduled run is active') from None
        yield


def resume_stage(config, before, fallback):
    reports = sorted(set(config['run_dir'].glob('*.json')) - before)
    if not reports:
        return fallback, None
    report_path = reports[-1]
    report = json.loads(report_path.read_text(encoding='utf-8'))
    for stage in vanguard.STAGES:
        # Resumed reports omit earlier stages; absence is not a failed stage.
        if stage in report.get('stages', {}) and report['stages'][stage].get('status') != 'completed':
            return stage, str(report_path)
    return fallback, str(report_path)


def execute(config, directory, state_path, state, now, dry_run=False):
    slot = slot_at(now)
    previous = state.get('current', {})
    if previous.get('slot') == slot and previous.get('status') == 'completed':
        print(f'Slot {slot} already completed; no new scrape or model calls.')
        return 0
    # Retry unfinished generation/export before collecting another batch, even after
    # the next scheduled slot. This avoids creating new jobs on every failure retry.
    unfinished = previous.get('status') in ('failed', 'running')
    stage = previous.get('next_stage', 'scrape') if unfinished else 'scrape'
    entry = {'slot': slot, 'started_at': now.isoformat(), 'status': 'running',
             'next_stage': stage, 'attempt': previous.get('attempt', 0) + 1 if unfinished else 1}
    args = argparse.Namespace(command='run', from_stage=stage, limit=None, headed=False,
                              attach=None, dry_run=dry_run)
    if dry_run:
        return vanguard.run(config, args)
    state['current'] = entry
    vanguard.save_report(state_path, state)
    def checkpoint(stage, report_path):
        entry.update(next_stage=stage, pipeline_report=str(report_path))
        vanguard.save_report(state_path, state)
    args.stage_callback = checkpoint
    before = set(config['run_dir'].glob('*.json'))
    code = 1
    try:
        code = vanguard.run(config, args)
        entry['status'] = 'completed' if code == 0 else 'failed'
        return code
    except BaseException as error:
        entry['status'] = 'failed'
        entry['error'] = type(error).__name__
        raise
    finally:
        entry['finished_at'] = datetime.now(TIMEZONE).isoformat()
        entry['exit_code'] = code
        entry['next_stage'], entry['pipeline_report'] = resume_stage(config, before, entry['next_stage'])
        if entry['status'] == 'completed':
            state['last_success_at'] = entry['finished_at']
        vanguard.save_report(state_path, state)


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['run', 'doctor', 'status'], default='run')
    parser.add_argument('--config', type=Path, default=ROOT / 'config/pipeline.local.yaml')
    parser.add_argument('--settings', type=Path, default=ROOT / 'config/schedule.local.json')
    parser.add_argument('--dry-run', action='store_true')
    args = parser.parse_args(argv)
    settings = json.loads(args.settings.read_text()) if args.settings.exists() else {}
    os.environ.update(runtime_environment(settings))
    directory = ROOT / 'var/scheduler'
    state_path = directory / 'state.json'
    try:
        if args.command == 'status':
            state = json.loads(state_path.read_text()) if state_path.exists() else {}
            print(json.dumps({'timezone': str(TIMEZONE), 'times': ['06:00', '12:00'],
                              'next_regular_slot': next_slot(datetime.now(TIMEZONE)), **state}, indent=2))
            return 0
        config = vanguard.load_config(args.config.expanduser().resolve())
        if args.dry_run:
            return execute(config, directory, state_path, {}, datetime.now(TIMEZONE), dry_run=True)
        with lock(directory):
            stamp = datetime.now(TIMEZONE).strftime('%Y%m%dT%H%M%S%f')
            log = directory / f'{stamp}-{args.command}.log'
            with log.open('w', encoding='utf-8', buffering=1) as output, redirect_stdout(output), redirect_stderr(output):
                print(f'Scheduled {args.command}: {datetime.now(TIMEZONE).isoformat()}', flush=True)
                # vanguard subprocesses inherit file descriptors, not Python redirects.
                # Point their OS stdout/stderr at the same persistent log.
                stdout_fd, stderr_fd = os.dup(1), os.dup(2)
                try:
                    os.dup2(output.fileno(), 1)
                    os.dup2(output.fileno(), 2)
                    if args.command == 'doctor':
                        errors = vanguard.check(config, vanguard.STAGES)
                        for error in errors:
                            print(f'FAIL: {error}')
                        auth = subprocess.run(['codex', 'login', 'status'], capture_output=True, text=True)
                        # Only the CLI status text, never authentication files/tokens.
                        print(auth.stdout + auth.stderr)
                        os.environ.update(vanguard.environment(config))
                        from playwright.sync_api import sync_playwright
                        with sync_playwright() as pw:
                            browser = pw.chromium.launch(headless=True)
                            browser.close()
                        print('Headless browser: OK')
                        from urllib.request import urlopen
                        from urllib.error import HTTPError
                        for url in ('https://jobright.ai/', 'https://chatgpt.com/'):
                            try:
                                with urlopen(url, timeout=30) as response:
                                    print(f'Network reachability {url}: HTTP {response.status}')
                            except HTTPError as error:
                                # A public endpoint denying a non-browser request still
                                # establishes DNS/TLS/network reachability, not login.
                                print(f'Network reachability {url}: HTTP {error.code}')
                        ok = not errors and auth.returncode == 0
                        print('SCHEDULED DOCTOR PASS' if ok else 'SCHEDULED DOCTOR FAIL', flush=True)
                        return int(not ok)
                    state = json.loads(state_path.read_text()) if state_path.exists() else {}
                    return execute(config, directory, state_path, state, datetime.now(TIMEZONE))
                except BaseException as error:
                    print(f'Scheduled launcher error: {type(error).__name__}: {error}', flush=True)
                    raise
                finally:
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os.dup2(stdout_fd, 1)
                    os.dup2(stderr_fd, 2)
                    os.close(stdout_fd)
                    os.close(stderr_fd)
    except (OSError, ValueError, ImportError) as error:
        print(f'Scheduled run stopped: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    raise SystemExit(main())

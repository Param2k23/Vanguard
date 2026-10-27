#!/usr/bin/env python3
"""Run the Vanguard scrape → resume → export pipeline."""
import argparse
from contextlib import contextmanager
from datetime import datetime
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import uuid

ROOT = Path(__file__).resolve().parent
STAGES = ('scrape', 'resumes', 'export')


def resolve(value):
    path = Path(os.path.expandvars(str(value))).expanduser()
    return (ROOT / path).resolve() if not path.is_absolute() else path.resolve()


def load_config(path):
    import yaml
    config = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    if not isinstance(config, dict):
        raise ValueError('Pipeline configuration must be a YAML mapping')
    for name, default in [('scraper_dir', 'job-scraper'), ('agent_dir', 'job-agent'),
                          ('export_dir', 'exports'), ('run_dir', 'var/runs')]:
        config[name] = resolve(config.get(name, default))
    limit = config.get('limit', 15)
    if type(limit) is not int or limit < 1:
        raise ValueError('limit must be a positive integer')
    config['limit'] = limit
    for key in ('email', 'github_user', 'resume_prefix'):
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f'Set {key} in your pipeline configuration')
    if '@' not in config['email'] or config['email'].endswith(('.test', 'example.com')):
        raise ValueError('Set email to your own contact email')
    for key in ('resume_prefix', 'github_user'):
        if any(c in config[key] for c in '/\\\n\r') or config[key] in ('.', '..'):
            raise ValueError(f'{key} must not contain path separators')
    return config


def environment(config):
    env = {**os.environ, 'PYTHONUNBUFFERED': '1', 'RESUME_EXPORT_APPLY': '0',
           'VANGUARD_DEFER_EXPORT': '1', 'RESUME_EXPORT_DIR': str(config['export_dir']),
           'VANGUARD_EMAIL': config['email'], 'VANGUARD_GITHUB_USER': config['github_user'],
           'VANGUARD_RESUME_PREFIX': config['resume_prefix']}
    browser_path = ROOT / '.playwright-browsers'
    if browser_path.exists():
        env.setdefault('PLAYWRIGHT_BROWSERS_PATH', str(browser_path))
    if config.get('browser_path'):
        env['PLAYWRIGHT_BROWSERS_PATH'] = str(resolve(config['browser_path']))
    return env


def commands(config, args):
    scraper, agent = config['scraper_dir'], config['agent_dir']
    scrape = [sys.executable, str(scraper / 'scrape.py'), 'run', '--limit', str(args.limit or config['limit'])]
    if args.headed:
        scrape.append('--headed')
    if args.attach is not None:
        scrape += ['--attach', str(args.attach)]
    resume = [sys.executable, str(scraper / 'generate_resumes.py'), '--agent-dir', str(agent)]
    if config.get('model'):
        resume += ['--model', config['model']]
    # Use the portable exporter even when reusing an existing job-agent directory.
    export = [sys.executable, str(ROOT / 'job-agent/scripts/export_pdfs.py'),
              '--root', str(agent), '--apply', '--skip-unverified']
    mount = config.get('export_mount')
    parts = config['export_dir'].parts
    if not mount and len(parts) >= 3 and parts[1] == 'mnt' and len(parts[2]) == 1:
        mount = str(Path(*parts[:3]))
    if mount:
        export += ['--mount', str(resolve(mount))]
    return dict(zip(STAGES, (scrape, resume, export)))


def check(config, stages):
    errors = []
    scraper, agent = config['scraper_dir'], config['agent_dir']
    files = []
    if 'scrape' in stages:
        files += [scraper / 'scrape.py', scraper / 'config.yaml']
        try:
            import playwright.sync_api  # noqa: F401
            import bs4  # noqa: F401
        except ImportError:
            errors.append('Install requirements.txt into the active Python environment')
    if 'resumes' in stages:
        files += [scraper / 'generate_resumes.py', agent / 'AGENTS.md', agent / 'base/system_prompt.md',
                  agent / 'scripts/new_job.py', agent / 'scripts/verify_resume.py']
        files += [agent / 'base' / f"{config['resume_prefix']}_{label}.tex" for label in ('Data', 'Fullstack')]
        for executable in ('codex', 'pdflatex', 'pdfinfo', 'pdftotext'):
            if not shutil.which(executable):
                errors.append(f'Missing command: {executable}')
    if 'export' in stages:
        files += [ROOT / 'job-agent/scripts/export_pdfs.py']
        dest = config['export_dir']
        if dest == agent or agent.is_relative_to(dest) or dest.is_relative_to(agent / 'jobs'):
            errors.append('Choose an export directory outside the job-agent workspace')
        existing = dest
        while not existing.exists() and existing.parent != existing:
            existing = existing.parent
        if not existing.is_dir() or not os.access(existing, os.W_OK):
            errors.append(f'Export destination is not writable: {dest}')
        parts = dest.parts
        mount = resolve(config['export_mount']) if config.get('export_mount') else (
            Path(*parts[:3]) if len(parts) >= 3 and parts[1] == 'mnt' and len(parts[2]) == 1 else None)
        if mount and (not os.path.ismount(mount) or not os.access(mount, os.W_OK)):
            errors.append(f'Export drive is not mounted and writable: {mount}')
        if mount and not dest.is_relative_to(mount):
            errors.append('Export directory must be inside export_mount')
    errors += [f'Missing file: {file}' for file in files if not file.is_file()]
    return errors


@contextmanager
def pipeline_lock(config):
    # Lock the actual source workspace so different configurations cannot overlap.
    path = config['scraper_dir'] / '.vanguard.lock'
    with path.open('a') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another Vanguard command is using this scraper workspace') from None
        yield


def save_report(path, report):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    temp.replace(path)


def run(config, args):
    selected = STAGES[STAGES.index(args.from_stage):] if args.command == 'run' else (args.command,)
    plan = commands(config, args)
    if args.dry_run:
        for stage in selected:
            print(f'{stage}: {shlex.join(plan[stage])}')
        print(f"Export destination: {config['export_dir']} (dated subfolder)")
        return 0
    errors = check(config, selected)
    if errors:
        raise ValueError('\n'.join(errors))
    with pipeline_lock(config):
        config['run_dir'].mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime('%Y%m%dT%H%M%S') + '-' + uuid.uuid4().hex[:8]
        path = config['run_dir'] / f'{stamp}.json'
        report = {'started_at': datetime.now().astimezone().isoformat(), 'status': 'running', 'stages': {}}
        save_report(path, report)
        code = 0
        try:
            for stage in selected:
                print(f'\n[{stage.upper()}]', flush=True)
                report['stages'][stage] = {'status': 'running'}
                save_report(path, report)
                if callback := getattr(args, 'stage_callback', None):
                    callback(stage, path)
                code = subprocess.run(plan[stage], cwd=ROOT, env=environment(config)).returncode
                report['stages'][stage] = {'status': 'failed' if code else 'completed', 'exit_code': code}
                save_report(path, report)
                if code:
                    break
            report['status'] = 'failed' if code else 'completed'
        except BaseException:
            report['status'] = 'interrupted'
            raise
        finally:
            report['finished_at'] = datetime.now().astimezone().isoformat()
            save_report(path, report)
            print(f'Run report: {path}', flush=True)
        return code


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='run', choices=['run', 'doctor', 'login', *STAGES])
    parser.add_argument('--config', type=Path, default=ROOT / 'config/pipeline.local.yaml')
    parser.add_argument('--limit', type=int, help='New jobs to collect; resumes process all pending fetched JDs')
    parser.add_argument('--from-stage', choices=STAGES, default='scrape', help='Resume a run at this stage')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--attach', nargs='?', const=9222, type=int)
    parser.add_argument('--dry-run', action='store_true', help='Print the plan without browser/model/file operations')
    args = parser.parse_args(argv)
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be positive')
    if args.attach is not None and not 1 <= args.attach <= 65535:
        parser.error('--attach must be a port between 1 and 65535')
    try:
        config = load_config(args.config.expanduser().resolve())
        if args.command == 'doctor':
            errors = check(config, STAGES)
            for error in errors:
                print(f'FAIL: {error}')
            if not errors:
                print('Local prerequisites OK. First login, Codex authentication, feed selectors and account limits require a live run.')
            return int(bool(errors))
        if args.command == 'login':
            if args.dry_run:
                print(f"Login: {config['scraper_dir'] / 'scrape.py'} --login")
                return 0
            with pipeline_lock(config):
                command = [sys.executable, str(config['scraper_dir'] / 'scrape.py'), '--login']
                if args.attach is not None:
                    command += ['--attach', str(args.attach)]
                return subprocess.run(command, cwd=ROOT, env=environment(config)).returncode
        return run(config, args)
    except (OSError, ValueError, ImportError) as error:
        print(f'STOP: {error}', file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print('Interrupted. Rerun to retry unfinished work.', file=sys.stderr)
        return 130


if __name__ == '__main__':
    raise SystemExit(main())

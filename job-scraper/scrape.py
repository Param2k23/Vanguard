#!/usr/bin/env python3
"""Standalone local Jobright pipeline."""
import argparse
import csv
import logging
from datetime import date
import yaml
from job_pipeline.pipeline import ROOT, Pipeline, read_rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', choices=['collect', 'fetch', 'run', 'status', 'export'],
                        help='Default: run (collect jobs, then fetch descriptions)')
    parser.add_argument('--login', action='store_true')
    parser.add_argument('--headed', action='store_true')
    parser.add_argument('--debug', action='store_true')
    parser.add_argument('--attach', nargs='?', const=9222, type=int, metavar='PORT', help='Attach to local Chrome CDP (default 9222)')
    parser.add_argument('--limit', type=int, default=15)
    args = parser.parse_args()
    if args.limit < 1 or (args.attach is not None and not 1 <= args.attach <= 65535):
        parser.error('limit must be positive and port must be 1..65535')
    if args.login and args.command:
        parser.error('--login is a separate mode')
    if not args.login and not args.command:
        args.command = 'run'
    (ROOT / 'logs').mkdir(exist_ok=True)
    logging.basicConfig(filename=ROOT / f'logs/run-{date.today()}.log', level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')
    if args.command == 'status':
        rows = read_rows()
        columns = ['company', 'title', 'status', 'notes']
        widths = {c: max([len(c)] + [len(r[c]) for r in rows]) for c in columns}
        print(' | '.join(c.ljust(widths[c]) for c in columns))
        for row in rows:
            print(' | '.join(row[c].ljust(widths[c]) for c in columns))
        return 0
    if args.command == 'export':
        path = ROOT / 'data/ready.csv'
        path.parent.mkdir(exist_ok=True)
        with path.open('w', newline='', encoding='utf-8') as f:
            writer = csv.DictWriter(f, ['company', 'title', 'link', 'jd_path'])
            writer.writeheader()
            for row in read_rows():
                if row['status'] == 'jd_fetched':
                    writer.writerow(dict(company=row['company'], title=row['title'], link=row['source_url'] or row['jobright_url'], jd_path=row['jd_path']))
        print(f'Exported {path}')
        return 0
    config = yaml.safe_load((ROOT / 'config.yaml').read_text())
    from playwright.sync_api import sync_playwright
    try:
        with sync_playwright() as pw:
            context = None
            page = None
            try:
                if args.attach is not None:
                    browser = pw.chromium.connect_over_cdp(f'http://127.0.0.1:{args.attach}')
                    if not browser.contexts:
                        raise RuntimeError('Attached Chrome has no browser context')
                    context = browser.contexts[0]
                else:
                    context = pw.chromium.launch_persistent_context(str(ROOT / 'browser-profile'), headless=not (args.headed or args.login))
                page = context.new_page()
                pipeline = Pipeline(page, config, args.debug)
                if args.login:
                    pipeline.goto(config['login_url'])
                    input('Log in manually in the browser, then press Enter here to save and exit: ')
                else:
                    if args.command in ('collect', 'run'):
                        print('Collecting jobs...')
                        pipeline.collect(args.limit)
                    if args.command in ('fetch', 'run'):
                        print('Fetching job descriptions...')
                        pipeline.fetch()
                        print('Fetch complete. Saved descriptions: data/jds/; job statuses: data/jobs.csv')
            finally:
                if args.attach is not None:
                    if page and not page.is_closed():
                        page.close()  # Leave user's Chrome and existing tabs open.
                elif context:
                    context.close()
        return 0
    except Exception as exc:
        from job_pipeline.pipeline import SessionExpired
        if isinstance(exc, (SessionExpired, ValueError)) or type(exc) is RuntimeError:
            print(str(exc))
        else:
            print(f'Browser failure ({type(exc).__name__}). Check browser installation; try --headed. On WSL without a display, use the Windows-native setup in README.md.')
        logging.error('Run stopped: %s', type(exc).__name__)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())

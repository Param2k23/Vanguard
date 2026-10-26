"""Create a job directory, preserving pasted JD text."""
import argparse
import datetime
import json
import sys
from common import ROOT, slug

def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('company'); p.add_argument('role'); p.add_argument('--jd-file')
    a = p.parse_args()
    if a.jd_file:
        with open(a.jd_file, encoding='utf-8', newline='') as f: jd = f.read()
    else:
        jd = '' if sys.stdin.isatty() else sys.stdin.read()
    stem = f'{slug(a.company)}-{slug(a.role)}'
    path = ROOT / 'jobs' / stem
    if path.exists():
        stem += '-' + datetime.date.today().isoformat()
        path = ROOT / 'jobs' / stem
        n = 2
        while path.exists():
            path = ROOT / 'jobs' / f'{stem}-{n}'; n += 1
    path.mkdir(parents=True)
    with (path / 'jd.txt').open('w', encoding='utf-8', newline='') as f: f.write(jd)
    (path / 'job.json').write_text(json.dumps({'company': a.company, 'role': a.role}, indent=2)+'\n')
    print(path.relative_to(ROOT))
if __name__ == '__main__': main()

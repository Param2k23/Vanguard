"""Append a job unless its company and role already exist."""
import argparse
import csv
import os
import subprocess
import sys
import datetime
import fcntl
from common import ROOT, JOB_FIELDS

def auto_export():
    if os.environ.get('RESUME_EXPORT_APPLY') == '1' and os.environ.get('VANGUARD_DEFER_EXPORT') != '1':
        result = subprocess.run([sys.executable, str(ROOT/'scripts/export_pdfs.py'), '--apply'])
        if result.returncode:
            print('Export incomplete; see skipped jobs above')

def main():
    p = argparse.ArgumentParser(description=__doc__)
    for field in JOB_FIELDS:
        p.add_argument('--'+field.replace('_','-'), required=field in ('company','role'), default=datetime.date.today().isoformat() if field=='date_added' else '')
    row = vars(p.parse_args())
    path = ROOT / 'tracker/jobs.csv'
    key = lambda r: (r['company'].strip().casefold(), r['role'].strip().casefold())
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+', newline='', encoding='utf-8') as f:
        # Hold the same file lock across duplicate checking and appending.
        # Do not replace the file: concurrent processes lock its inode.
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        f.seek(0)
        reader = csv.DictReader(f)
        rows = list(reader)
        fields = list(reader.fieldnames or JOB_FIELDS)
        for field in JOB_FIELDS:
            if field not in fields: fields.append(field)
        duplicate = any(key(r)==key(row) for r in rows)
        if not duplicate: rows.append(row)
        f.seek(0); f.truncate()
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)
        f.flush()  # Flush buffered writes before closing releases the lock.
    print('SKIP: company + role already logged' if duplicate else 'PASS: job logged')
    auto_export()
if __name__ == '__main__': main()

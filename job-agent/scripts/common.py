"""Shared paths and CSV helpers; standard library only."""
import csv
import os
import re
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
RESUME_PREFIX = os.environ.get('VANGUARD_RESUME_PREFIX', 'Applicant')
GITHUB_USER = os.environ.get('VANGUARD_GITHUB_USER', 'example-user')
JOB_FIELDS = 'date_added company role location job_url base_resume strategy match_score status notes export_path exported_at'.split()
CONTACT_FIELDS = 'company name title email source verified pattern'.split()
DRAFT_FIELDS = 'company role contact_name contact_email verified subject body status'.split()
def slug(value):
    result = re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')
    if not result:
        raise ValueError('Name must contain a letter or digit')
    return result
def read_csv(path):
    if not path.exists(): return []
    with path.open(newline='', encoding='utf-8') as f: return list(csv.DictReader(f))
def write_csv(path, fields, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)

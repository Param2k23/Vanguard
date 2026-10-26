"""Configured identity shared by the resume and experimental application flows."""
import json
import os
from pathlib import Path


def configured_email():
    email = os.environ.get('VANGUARD_EMAIL', '').strip()
    if email:
        return email
    path = Path(__file__).resolve().parents[2] / 'applicant_profile.json'
    if path.is_file():
        return json.loads(path.read_text(encoding='utf-8')).get('facts', {}).get('email') or ''
    return ''


CONFIRMED_EMAIL = configured_email()

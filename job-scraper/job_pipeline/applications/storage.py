import csv
import hashlib
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path

STATUSES = set('HISTORICAL_MANUALLY_APPLIED RESUME_FAILED DISCOVERED WAITING_FOR_RESUME READY_TO_APPLY IN_PROGRESS APPLIED NEEDS_REVIEW BLOCKED_AUTH BLOCKED_CAPTCHA NOT_SUPPORTED JOB_CLOSED FAILED SUBMISSION_UNCONFIRMED SKIPPED ALREADY_APPLIED'.split())
PROTECTED = {'HISTORICAL_MANUALLY_APPLIED', 'APPLIED', 'ALREADY_APPLIED', 'SUBMISSION_UNCONFIRMED'}
ACCOUNT_COLUMNS = 'account_id company portal_name portal_domain login_url email credential_reference password_type account_status created_at last_login_at verification_status associated_job_ids last_error notes'.split()
COLUMNS = 'job_id company position location job_url application_url ats_platform resume_path resume_version resume_status resume_acceptance_mode status first_attempt_at last_attempt_at submitted_at attempt_count confirmation_id confirmation_evidence review_reason error_message generated_answers_path notes company_policy application_method referral_status referral_contact referral_notes policy_evaluated_at portal_account_id portal_account_status login_required login_successful account_created verification_required authentication_error'.split()


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_json(path, value):
    atomic_write(path, json.dumps(value, indent=2, ensure_ascii=False) + '\n')


class Store:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(self.directory / 'applications.sqlite3')
        os.chmod(self.directory / 'applications.sqlite3', 0o600)
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS applications (job_id TEXT PRIMARY KEY, identity TEXT, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS application_identity ON applications(identity);
            CREATE TABLE IF NOT EXISTS events (id INTEGER PRIMARY KEY, job_id TEXT, at TEXT, payload TEXT);
            CREATE TABLE IF NOT EXISTS reviews (id TEXT PRIMARY KEY, job_id TEXT, question TEXT, payload TEXT, resolved INTEGER DEFAULT 0);
            CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, payload TEXT);
            CREATE TABLE IF NOT EXISTS portal_accounts (account_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
        ''')
        # A crash after the irreversible boundary must never become a retry.
        for row in self.rows():
            if row['status'] == 'IN_PROGRESS':
                self.update(row['job_id'], status='READY_TO_APPLY', review_reason='', notes='Interrupted before final submit boundary; readiness and all fields will be revalidated')
        self.export()

    def close(self):
        self.export()
        self.db.close()

    def rows(self):
        return [dict(dict.fromkeys(COLUMNS, ''), **json.loads(r[0])) for r in self.db.execute('SELECT payload FROM applications ORDER BY job_id')]

    def get(self, job_id):
        row = self.db.execute('SELECT payload FROM applications WHERE job_id=?', (job_id,)).fetchone()
        return dict(dict.fromkeys(COLUMNS, ''), **json.loads(row[0])) if row else None

    def update(self, job_id, identity=None, **changes):
        old = self.get(job_id) or dict.fromkeys(COLUMNS, '')
        status = changes.get('status', old['status'] or 'DISCOVERED')
        if status not in STATUSES:
            raise ValueError('Invalid status')
        if old['status'] in PROTECTED and status != old['status']:
            # Only explicit read-only reconciliation may resolve uncertainty.
            if not (old['status'] == 'SUBMISSION_UNCONFIRMED' and status == 'APPLIED' and changes.get('confirmation_evidence')):
                raise ValueError('Protected application cannot be retried or reset')
        if status == 'APPLIED' and not changes.get('confirmation_evidence', old.get('confirmation_evidence')):
            raise ValueError('Confirmation evidence required')
        old.update(changes, job_id=job_id, status=status)
        with self.db:
            self.db.execute('INSERT INTO applications VALUES (?,?,?) ON CONFLICT(job_id) DO UPDATE SET identity=COALESCE(excluded.identity,applications.identity),payload=excluded.payload', (job_id, identity, json.dumps(old)))
            self.db.execute('INSERT INTO events(job_id,at,payload) VALUES (?,?,?)', (job_id, now(), json.dumps(changes)))
        self.export()
        return old

    def duplicate(self, identity, job_id):
        if not identity:
            return None
        for row in self.db.execute('SELECT payload FROM applications WHERE identity=? AND job_id != ?', (identity, job_id)):
            item = json.loads(row[0])
            if item['status'] in PROTECTED:
                return item
        return None

    def review(self, job_id, question, reason, **extra):
        key = digest(f'{job_id}\n{question}'.encode())
        payload = dict(reason=reason, next_action='Provide verified answer, then explicitly resume this job', **extra)
        with self.db:
            self.db.execute('INSERT INTO reviews VALUES (?,?,?,?,0) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,resolved=0', (key, job_id, question, json.dumps(payload)))
        return key

    def reviews(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM reviews WHERE resolved=0')]

    def cache(self, key, value=None):
        if value is not None:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO answers VALUES (?,?)', (key, json.dumps(value)))
        row = self.db.execute('SELECT payload FROM answers WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def account(self, account_id):
        row = self.db.execute('SELECT payload FROM portal_accounts WHERE account_id=?', (account_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def accounts(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM portal_accounts ORDER BY account_id')]

    def update_account(self, account_id, **changes):
        value = self.account(account_id) or dict.fromkeys(ACCOUNT_COLUMNS, '')
        value.update(changes, account_id=account_id)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO portal_accounts VALUES (?,?)', (account_id, json.dumps(value)))
        self.export()
        return value

    def export(self):
        import io
        out = io.StringIO(newline='')
        writer = csv.DictWriter(out, fieldnames=COLUMNS, extrasaction='ignore')
        writer.writeheader()
        for row in self.rows():
            # Protect spreadsheet readers against formula injection.
            writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for k, v in row.items()})
        atomic_write(self.directory / 'job_applications.csv', out.getvalue())
        out = io.StringIO(newline='')
        writer = csv.DictWriter(out, fieldnames=ACCOUNT_COLUMNS, extrasaction='ignore')
        writer.writeheader()
        for row in self.accounts():
            writer.writerow({k: "'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@', '\t', '\r')) else v for k, v in row.items()})
        atomic_write(self.directory / 'job_portal_accounts.csv', out.getvalue())
        save_json(self.directory / 'referral_attention_queue.json', [r for r in self.rows() if r.get('company_policy') == 'REFERRAL_REQUIRED' and r['status'] not in PROTECTED])

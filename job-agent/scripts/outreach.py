"""Import pasted contacts and save session-written outreach drafts; never approves or sends."""
import argparse
import csv
import io
import json
import re
import sys
from common import ROOT, CONTACT_FIELDS, DRAFT_FIELDS, read_csv, write_csv
from email_patterns import infer_email


def key(value):
    return value.strip().casefold()


def parse_contacts(text):
    contacts = []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        company, separator, details = line.partition(':')
        fields = next(csv.reader(io.StringIO(details), skipinitialspace=True)) if separator else []
        if not company.strip() or len(fields) not in (2, 3) or not all(f.strip() for f in fields[:2]):
            raise ValueError(f'Line {number}: use Company: Name, Title, optional email (verified)')
        email = fields[2].strip() if len(fields) == 3 else ''
        verified = bool(re.search(r'\s*\(verified\)$', email, re.I))
        email = re.sub(r'\s*\(verified\)$', '', email, flags=re.I).strip()
        if email and not re.fullmatch(r'[^\s@,]+@[^\s@,]+\.[^\s@,]+', email):
            raise ValueError(f'Line {number}: invalid email')
        contacts.append(dict(company=company.strip(), name=fields[0].strip(), title=fields[1].strip(),
                             email=email, verified=str(verified).lower(), source='user intake', pattern=''))
    return contacts


def match_job(root, company, selected):
    rows = [r for r in read_csv(root/'tracker/jobs.csv') if key(r['company']) == key(company)]
    matches = []
    for metadata in sorted((root/'jobs').glob('*/job.json')):
        job = json.loads(metadata.read_text())
        if key(job.get('company', '')) != key(company):
            continue
        for row in rows:
            if key(row['role']) == key(job.get('role', '')):
                matches.append((row, metadata.parent))
    if selected:
        matches = [(row, folder) for row, folder in matches if folder.name in selected]
    if len(matches) != 1:
        choices = ', '.join(f'{r["role"]} ({f.name})' for r, f in matches)
        raise ValueError(f'Which job should {company} use? {choices or "No matching tracked job folder found."}')
    row, folder = matches[0]
    if not (folder/'resume.tex').is_file():
        raise ValueError(f'{company}: tailored resume missing in {folder}')
    return row, folder


def process(root, text, draft_input=None, selected=()):
    incoming = parse_contacts(text)
    jobs = {key(c['company']): match_job(root, c['company'], selected) for c in incoming}
    contacts = read_csv(root/'outreach/contacts.csv')
    drafts = read_csv(root/'outreach/drafts.csv')
    for contact in incoming:
        row, _ = jobs[key(contact['company'])]
        contact['company'] = row['company']
        old = next((c for c in contacts if key(c['company']) == key(contact['company']) and key(c['name']) == key(contact['name'])), None)
        if old:
            if old['verified'] == 'true' and contact['verified'] != 'true':
                contact.update(email=old['email'], verified='true', source=old['source'], pattern=old['pattern'])
            old.update(contact)
        else:
            contacts.append(contact)
    # Resolve after importing the entire batch so verified examples can appear anywhere.
    for contact in incoming:
        stored = next(c for c in contacts if key(c['company']) == key(contact['company']) and key(c['name']) == key(contact['name']))
        if stored['verified'] != 'true':
            candidates = set()
            for example in contacts:
                if key(example['company']) == key(stored['company']) and example['verified'] == 'true':
                    try:
                        candidates.add(infer_email(example['name'], example['email'], stored['name']))
                    except ValueError:
                        pass
            stored.update(email='', verified='false', pattern='')
            if len(candidates) == 1:
                stored['email'], stored['pattern'] = candidates.pop()
                stored['source'] = 'inferred from same-company verified address'
    pending = []
    for item in draft_input or []:
        contact = next((c for c in incoming if key(c['company']) == key(item['company']) and key(c['name']) == key(item['name'])), None)
        if contact is None:
            raise ValueError('Draft must match a contact in this intake')
        stored = next(c for c in contacts if key(c['company']) == key(contact['company']) and key(c['name']) == key(contact['name']))
        row, folder = jobs[key(contact['company'])]
        body, subject = item['body'].strip(), item['subject'].strip()
        if not body or len(body.split()) >= 120:
            raise ValueError('Draft must contain fewer than 120 words')
        if not subject or '\n' in subject or '\r' in subject:
            raise ValueError('Draft needs a single-line subject')
        # The session supplies a literal supporting excerpt for review, not invented facts.
        evidence = item.get('resume_evidence', '').strip()
        if not evidence or evidence not in (folder/'resume.tex').read_text():
            raise ValueError('Provide a literal resume_evidence excerpt from the matched tailored resume')
        if key(row['role']) not in key(body):
            raise ValueError('Draft body must refer to the specific role')
        if not any(phrase in key(body) for phrase in ('no worries', 'let me know and', 'no further messages', 'prefer not', 'opt out')):
            raise ValueError('Draft needs a polite opt-out line')
        duplicate = next((d for d in drafts + pending if key(d['company']) == key(row['company']) and key(d['role']) == key(row['role']) and key(d['contact_name']) == key(contact['name'])), None)
        if duplicate:
            continue
        pending.append(dict(company=row['company'], role=row['role'], contact_name=contact['name'],
                            contact_email=stored['email'], verified=stored['verified'], subject=subject, body=body, status='draft'))
    write_csv(root/'outreach/contacts.csv', CONTACT_FIELDS, contacts)
    if pending:
        write_csv(root/'outreach/drafts.csv', DRAFT_FIELDS, drafts + pending)
    print('| Company | Contact | Email | Verified or guessed | Status |')
    print('| --- | --- | --- | --- | --- |')
    for contact in incoming:
        stored = next(c for c in contacts if key(c['company']) == key(contact['company']) and key(c['name']) == key(contact['name']))
        row, folder = jobs[key(contact['company'])]
        draft = next((d for d in drafts + pending if key(d['company']) == key(row['company']) and key(d['role']) == key(row['role']) and key(d['contact_name']) == key(contact['name'])), None)
        print(f'| {row["company"]} | {contact["name"]} | {stored["email"] or "needs email"} | {"verified" if stored["verified"] == "true" else "guessed" if stored["email"] else "missing"} | {draft["status"] if draft else "needs draft"} |')
        if not draft:
            print(f'Resume for {contact["name"]}: {folder / "resume.tex"}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--contacts-file', help='Pasted contact lines; defaults to stdin')
    parser.add_argument('--drafts-file', help='Session-written JSON list: company, name, subject, body, resume_evidence')
    parser.add_argument('--job-folder', action='append', default=[], help='Folder basename to resolve an ambiguous company')
    args = parser.parse_args()
    try:
        text = open(args.contacts_file, encoding='utf-8').read() if args.contacts_file else sys.stdin.read()
        draft_input = json.loads(open(args.drafts_file, encoding='utf-8').read()) if args.drafts_file else None
        process(ROOT, text, draft_input, args.job_folder)
    except (ValueError, KeyError, OSError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    main()

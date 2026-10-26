"""Dry-run by default. Execute only on the user's explicit instruction."""
import argparse
import datetime
import json
import os
import random
import smtplib
import ssl
import time
from email.message import EmailMessage
from common import ROOT, DRAFT_FIELDS, read_csv, write_csv

def main():
    p=argparse.ArgumentParser(description=__doc__)
    mode=p.add_mutually_exclusive_group(); mode.add_argument('--dry-run',action='store_true'); mode.add_argument('--send',action='store_true')
    p.add_argument('--allow-unverified',action='store_true'); p.add_argument('--daily-cap',type=int,default=10)
    p.add_argument('--min-delay',type=float,default=15); p.add_argument('--max-delay',type=float,default=45)
    a=p.parse_args()
    if a.daily_cap<1 or not 0<a.min_delay<=a.max_delay: p.error('Cap and delays must be positive; max delay >= min delay')
    path=ROOT/'outreach/drafts.csv'; ledger=ROOT/'outreach/send_ledger.json'
    # A process lock prevents concurrent senders from exceeding the daily cap.
    import fcntl
    with (ROOT/'outreach/.send.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        rows=read_csv(path)
        if any(r['status'] not in {'draft','approved','sent','skipped'} for r in rows): p.error('Invalid draft status')
        counts=json.loads(ledger.read_text()) if ledger.exists() else {}
        day=datetime.date.today().isoformat(); remaining=max(0,a.daily_cap-counts.get(day,0))
        eligible=[]
        for r in rows:
            if r['status']!='approved': continue
            if r['verified'].strip().lower()!='true' and not a.allow_unverified:
                print('SKIP: unverified '+r['contact_email']); continue
            if any('\n' in r[k] or '\r' in r[k] for k in ('contact_email','subject')) or '@' not in r['contact_email']: p.error('Invalid email headers')
            eligible.append(r)
        eligible=eligible[:remaining]
        if not a.send:
            for r in eligible: print('DRY RUN: '+r['contact_email']+' | '+r['subject'])
            print(f'DRY RUN: {len(eligible)} eligible; {remaining} daily slots'); return
        user=os.environ.get('GMAIL_USER'); password=os.environ.get('GMAIL_APP_PASSWORD')
        if not user or not password: p.error('Set GMAIL_USER and GMAIL_APP_PASSWORD in your environment')
        if not eligible: print('No eligible approved drafts'); return
        with smtplib.SMTP_SSL('smtp.gmail.com',465,context=ssl.create_default_context()) as smtp:
            smtp.login(user,password)
            for i,r in enumerate(eligible):
                if i: time.sleep(random.uniform(a.min_delay,a.max_delay))
                message=EmailMessage(); message['From']=user; message['To']=r['contact_email']; message['Subject']=r['subject']; message.set_content(r['body'])
                # Reserve a daily slot before SMTP; failures conservatively consume it.
                counts[day]=counts.get(day,0)+1
                temp=ledger.with_suffix('.tmp'); temp.write_text(json.dumps(counts,indent=2)+'\n'); temp.replace(ledger)
                smtp.send_message(message)
                r['status']='sent'; write_csv(path,DRAFT_FIELDS,rows)
                print('SENT: '+r['contact_email'])
    # SMTP and file writes cannot be atomic. Reconcile ambiguous failures before retrying.
if __name__=='__main__': main()

"""Infer candidates from one user-verified example; candidates remain unverified."""
import argparse
import re
from common import ROOT, CONTACT_FIELDS, read_csv, write_csv

def patterns(name):
    parts = re.findall(r'[a-z]+', name.lower())
    if len(parts)<2: raise ValueError('Provide first and last names')
    first,last = parts[0],parts[-1]
    return dict(zip(('first.last','flast','first','f.last','firstl'), (first+'.'+last,first[0]+last,first,first[0]+'.'+last,first+last[0])))
def infer_email(example_name, example_email, name):
    """Return a guessed address and pattern; never establishes verification."""
    parts = example_email.lower().split('@')
    if len(parts) != 2 or not re.fullmatch(r'[a-z0-9.-]+\.[a-z]{2,}', parts[1]):
        raise ValueError('Invalid example email')
    local, domain = parts
    matches = [k for k, v in patterns(example_name).items() if v == local]
    if len(matches) != 1:
        raise ValueError('Example is unsupported or ambiguous; provide another verified example')
    pattern = matches[0]
    return patterns(name)[pattern] + '@' + domain, pattern

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--company', required=True); p.add_argument('--example-name', required=True)
    p.add_argument('--example-email', required=True); p.add_argument('--names', nargs='+', required=True)
    p.add_argument('--title',default=''); p.add_argument('--source',default='user-verified example')
    a=p.parse_args()
    try:
        path=ROOT/'outreach/contacts.csv'; rows=read_csv(path)
        for name in a.names:
            email,pattern=infer_email(a.example_name,a.example_email,name)
            row=dict(company=a.company,name=name,title=a.title,email=email,source=a.source,verified='false',pattern=pattern)
            if not any(r['company']==a.company and r['email']==email for r in rows): rows.append(row)
            print(f'{name}: {email} verified=false ({pattern})')
        write_csv(path,CONTACT_FIELDS,rows)
    except ValueError as e: p.error(str(e))
if __name__=='__main__': main()

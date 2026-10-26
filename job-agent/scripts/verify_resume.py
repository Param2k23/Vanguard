"""Compile and verify resume guardrails. PASS is mechanical, not factual verification."""
import argparse
import contextlib
import datetime
import io
from export_pdfs import digest
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from common import ROOT, read_csv, RESUME_PREFIX, GITHUB_USER

def arguments(text, command, count=1):
    result=[]
    for match in re.finditer(re.escape('\\'+command)+r'\s*(?=\{)', text):
        pos=match.end(); args=[]
        for _ in range(count):
            while pos<len(text) and text[pos].isspace(): pos+=1
            if pos>=len(text) or text[pos]!='{': raise ValueError('Malformed '+command)
            start=pos+1; depth=1; pos+=1
            while pos<len(text) and depth:
                if text[pos] in '{}' and (pos==0 or text[pos-1]!='\\'):
                    depth += 1 if text[pos]=='{' else -1
                pos+=1
            if depth: raise ValueError('Unbalanced '+command)
            args.append(text[start:pos-1])
        result.append((match.start(),pos,args))
    return result

def sections(text):
    entries=arguments(text,'section')
    return {a[0]:text[end:entries[i+1][0] if i+1<len(entries) else len(text)] for i,(_,end,a) in enumerate(entries)}
def projects(text):
    text=sections(text).get('Projects','')
    headings=arguments(text,'resumeProjectHeading',2)
    return [(a[0],a[1],[x[2][0] for x in arguments(text[end:headings[i+1][0] if i+1<len(headings) else len(text)],'resumeItem')]) for i,(_,end,a) in enumerate(headings)]
def skills(text):
    body=sections(text).get('Technical Skills','')
    # A logical skills row can span several source lines.
    entries=arguments(body,'textbf')
    rows=[]
    for i,(start,end,_) in enumerate(entries):
        stop=entries[i+1][0] if i+1<len(entries) else body.index('\\end{itemize}')
        row=body[start:stop].strip()
        if i+1==len(entries): row=row.rstrip().removesuffix('}').strip()
        rows.append(re.sub(r'\s+',' ',row).rstrip('\\').strip())
    return rows

def rendered_counts(text, names):
    lines=text.replace('\f','\n').splitlines(); positions=[]
    for name in names:
        hits=[i for i,line in enumerate(lines) if re.sub(r'\s+', '', line).casefold()==re.sub(r'\s+', '', name).casefold()]
        if len(hits)!=1: raise ValueError('Cannot locate rendered section '+name)
        positions.append(hits[0])
    if positions!=sorted(positions): raise ValueError('Rendered sections reordered')
    return {name:sum(bool(line.strip()) for line in lines[positions[i]+1:positions[i+1] if i+1<len(positions) else len(lines)]) for i,name in enumerate(names)}
def compile_pdf(source, dest):
    shutil.copy2(source,dest/'resume.tex')
    r=subprocess.run(['pdflatex','-interaction=nonstopmode','-halt-on-error','-no-shell-escape','resume.tex'],cwd=dest,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
    if r.returncode or not (dest/'resume.pdf').exists(): raise ValueError('Compilation failed: '+source.name+' (inspect source / local TeX dependencies)')
    info=subprocess.check_output(['pdfinfo',str(dest/'resume.pdf')],text=True)
    text=subprocess.check_output(['pdftotext','-layout',str(dest/'resume.pdf'),'-'],text=True)
    return int(re.search(r'^Pages:\s+(\d+)',info,re.M)[1]),text

def verify(job, base_name):
    job=Path(job).resolve(); base=ROOT/'base'/f'{RESUME_PREFIX}_{"Data" if base_name=="data" else "Fullstack"}.tex'
    (job/'.verified.json').unlink(missing_ok=True)
    checks=[]; passed=[]
    def check(name, ok):
        if ok: passed.append(name)
        checks.append(ok); print(('PASS' if ok else 'FAIL')+': '+name)
    try:
        source_hash=digest(job/'resume.tex')
        old=base.read_text(); new=(job/'resume.tex').read_text()
        meta=json.loads((job/'job.json').read_text()) if (job/'job.json').exists() else {}
        company=meta.get('company') or job.name.split('-')[0]
        with tempfile.TemporaryDirectory(prefix='.resume-build-', dir=job) as tmp:
            dirs=[Path(tmp)/x for x in ('base','tailored')]
            for d in dirs: d.mkdir()
            bp,bt=compile_pdf(base,dirs[0]); np,nt=compile_pdf(job/'resume.tex',dirs[1])
            check('base and tailored compile',True); check('exactly 1 page',np==1)
            names=list(sections(old)); same=list(sections(new))==names
            bc=rendered_counts(bt,names); nc=rendered_counts(nt,names)
            check('section line budgets',same and all(nc[k]<=bc[k] for k in names))
            op=projects(old); newp=projects(new)
            pattern = r'\\href\{https://github\.com/' + re.escape(GITHUB_USER) + r'/([A-Za-z0-9_.-]+)\}\{\\underline\{' + re.escape(GITHUB_USER) + r'/\1\}\}'
            check('project GitHub links',bool(newp) and all(re.fullmatch(pattern,x[1]) for x in newp))
            normalize=lambda s: re.sub('[^a-z0-9]','',s.lower())
            check('company absent from project titles/slugs',all(normalize(company) not in normalize(title+' '+link) for title,link,_ in newp))
            os,ns=skills(old),skills(new)
            check('skills character budgets',bool(os) and len(os)==len(ns) and all(len(n)<=len(o) for o,n in zip(os,ns)))
            parity=len(op)==len(newp)
            for (ot,ol,ob),(title,link,bullets) in zip(op,newp):
                oldslug=re.search(r'https://github.com/' + re.escape(GITHUB_USER) + r'/([^}]+)',ol)
                newslug=re.search(r'https://github.com/' + re.escape(GITHUB_USER) + r'/([^}]+)',link)
                replaced=ot!=title or (oldslug.group(1) if oldslug else '')!=(newslug.group(1) if newslug else '')
                if replaced: parity &= len(bullets)==3 and len(ob)>=3 and all(abs(len(n)-len(o))<=10 and re.search(r'\d',n) for o,n in zip(ob,bullets))
            check('replacement: 3 bullets, parity, digit metrics',bool(parity))
            check('source unchanged during verification',digest(job/'resume.tex')==source_hash)
            if all(checks):
                filename=RESUME_PREFIX+'_'+re.sub(r'[^A-Za-z0-9_-]+','_',company).strip('_')+'.pdf'
                shutil.copy2(dirs[1]/'resume.pdf',job/filename)
                (job/'.verified.json').write_text(json.dumps({'timestamp':datetime.datetime.now(datetime.timezone.utc).isoformat(), 'pdf':filename, 'pdf_sha256':digest(job/filename), 'tex_sha256':source_hash, 'page_count':np, 'checks':passed}))
                print('PASS: '+str(job/filename)); return 0
    except (OSError,ValueError,subprocess.SubprocessError,KeyError) as e: check(str(e),False)
    print('FAIL: resume verification'); return 1
def infer_base(job, rows):
    candidates=set()
    report=job/'report.md'
    if report.is_file():
        for line in report.read_text().splitlines():
            if 'Base Resume Selected:' in line:
                candidates.update(x.lower() for x in re.findall(re.escape(RESUME_PREFIX) + r'_(Data|Fullstack)\.tex',line,re.I))
    meta=json.loads((job/'job.json').read_text()) if (job/'job.json').is_file() else {}
    for row in rows:
        if meta and all(row.get(k,'').strip().casefold()==meta.get(k,'').strip().casefold() for k in ('company','role')):
            value=row.get('base_resume','').strip().lower()
            for name in ('data','fullstack'):
                if value in (name, f'{RESUME_PREFIX.lower()}_{name}.tex'): candidates.add(name)
    if len(candidates)!=1: raise ValueError('base unknown or conflicting in report/tracker')
    return candidates.pop()


def backfill(root):
    rows=read_csv(root/'tracker/jobs.csv')
    counts={'PASS':0,'FAIL':0,'SKIP':0}
    for job in sorted((root/'jobs').iterdir()):
        if not job.is_dir(): continue
        reason=None
        if job.is_symlink() or re.search(r'(^|[-_])(selftest|test|scratch|tmp|temp)([-_]|$)',job.name,re.I):
            reason='test/scratch folder or symlink'
        elif not (job/'resume.tex').is_file(): reason='missing resume.tex'
        elif not any(p.is_file() and not p.is_symlink() for p in job.glob('*.pdf')): reason='missing PDF'
        if reason is None:
            try: base=infer_base(job,rows)
            except (OSError,ValueError) as exc: reason=str(exc)
        if reason:
            counts['SKIP']+=1; print(f'SKIP {job.name}: {reason}'); continue
        with contextlib.redirect_stdout(io.StringIO()) as output:
            code=verify(job,base)
        status='FAIL' if code else 'PASS'; counts[status]+=1
        failures=[line.removeprefix('FAIL: ') for line in output.getvalue().splitlines() if line.startswith('FAIL: ')]
        print(f'{status} {job.name}: '+('; '.join(failures) if code else 'all checks passed'))
    print('TOTAL: '+', '.join(f'{key}={value}' for key,value in counts.items()))
    return int(counts['FAIL']>0)


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('job',nargs='?'); p.add_argument('--base',choices=['data','fullstack'])
    p.add_argument('--all',action='store_true',help='fully reverify existing jobs using report/tracker base metadata')
    a=p.parse_args()
    if a.all:
        if a.job or a.base: p.error('--all cannot be combined with job or --base')
        return backfill(ROOT)
    if not a.job or not a.base: p.error('job and --base are required unless --all is used')
    return verify(a.job,a.base)

if __name__=='__main__': raise SystemExit(main())

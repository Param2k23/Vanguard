"""Validate existing scraper/resume artifacts and audit technical acceptance."""
import csv
import json
import re
import subprocess
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .storage import digest, now, save_json


class ReadinessError(ValueError):
    def __init__(self, status, reason, **details):
        self.status = status
        self.details = details
        super().__init__(reason)


def normalize(value):
    return ' '.join(re.findall(r'[a-z0-9]+', value.lower()))


def canonical_url(url):
    p = urlsplit(url)
    if p.scheme not in ('https', 'http') or not p.hostname or p.username or p.password:
        raise ValueError('Expected an absolute HTTP(S) URL without credentials')
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if not k.lower().startswith('utm_') and k.lower() not in ('source', 'ref', 'gh_src', 'lever-source', 'trackingid', 'tracking_id', 'trk', 'refid')]
    # gh_jid and other requisition query parameters MUST survive canonicalization.
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/') or '/', urlencode(sorted(query)), ''))


def application_url(job):
    return canonical_url(job.get('source_url') or job.get('jobright_url') or '')


def identity(url):
    p = urlsplit(canonical_url(url))
    host = p.hostname or ''
    query = dict(parse_qsl(p.query))
    if host.endswith('greenhouse.io') or host.endswith('greenhouse.com'):
        requisition = query.get('gh_jid') or query.get('token')
        match = re.search(r'/jobs/(\d+)', p.path)
        if match:
            requisition = match[1]
        if requisition:
            return 'greenhouse:' + requisition
    if host == 'lever.co' or host.endswith('.lever.co'):
        parts = [x for x in p.path.split('/') if x]
        if len(parts) >= 2 and re.fullmatch(r'[0-9a-fA-F-]{32,36}', parts[1]):
            return 'lever:' + parts[0].lower() + ':' + parts[1].lower()
    if host == 'jobright.ai' or host.endswith('.jobright.ai'):
        match = re.search(r'/jobs/info/([^/]+)', p.path)
        if match:
            return 'jobright:' + match[1]
    # Company/title alone cannot distinguish two requisitions.
    return canonical_url(url)


def jobs(root):
    path = root / 'data/jobs.csv'
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as stream:
        rows = list(csv.DictReader(stream))
    ids = [r['id'] for r in rows]
    if any(not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier) for identifier in ids):
        raise ValueError('Invalid scraper job ID; expected a safe stable identifier')
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate scraper IDs; repair jobs.csv before applying')
    return rows


APPROVAL_REASON = 'Resume requires factual approval of exact PDF hash; Pipeline 2 permits assumed-completion drafts'


def validate_pdf(pdf):
    """Parse the PDF using the same Poppler tools required by Pipeline 2."""
    try:
        info = subprocess.run(['pdfinfo', str(pdf)], capture_output=True, timeout=20)
        text = subprocess.run(['pdftotext', str(pdf), '-'], capture_output=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ReadinessError('NEEDS_REVIEW', 'PDF readability validation unavailable or timed out') from error
    if info.returncode or text.returncode or not re.search(rb'Pages:\s*[1-9][0-9]*', info.stdout) or not text.stdout.strip():
        raise ReadinessError('NEEDS_REVIEW', 'PDF is corrupt, empty, or unreadable')


def resume_for(root, agent, job, approvals=None, approval_mode='automatic'):
    if approval_mode not in ('automatic', 'manual'):
        raise ValueError('RESUME_APPROVAL_MODE must be automatic or manual')
    if job.get('status') != 'jd_fetched' or not job.get('company') or not job.get('title'):
        raise ReadinessError('NEEDS_REVIEW', 'Scraped job metadata/JD is incomplete')
    jd = (root / job.get('jd_path', '')).resolve()
    if not jd.is_relative_to((root / 'data/jds').resolve()) or not jd.is_file():
        raise ReadinessError('NEEDS_REVIEW', 'Stored JD is missing or outside data/jds')
    version = digest(jd.read_bytes())
    key = digest(f'{agent.resolve()}\n{job["id"]}\n{version}'.encode())
    state_path = root / 'data/resumes/state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {}
    entry = state.get(key, {})
    if entry.get('status') != 'completed':
        raise ReadinessError('WAITING_FOR_RESUME', 'No completed resume for this job ID and current JD hash')
    if entry.get('job_id') != job['id']:
        raise ReadinessError('NEEDS_REVIEW', 'Resume state job ID mismatch')
    folder = Path(entry['folder']).resolve()
    if not folder.is_relative_to((agent / 'jobs').resolve()):
        raise ReadinessError('NEEDS_REVIEW', 'Resume folder is outside configured agent jobs')
    metadata = json.loads((folder / 'job.json').read_text())
    if normalize(metadata.get('company', '')) != normalize(job['company']) or normalize(metadata.get('role', '')) != normalize(job['title']):
        raise ReadinessError('NEEDS_REVIEW', 'Resume job.json company/role mismatch')
    if metadata.get('job_url') and identity(metadata['job_url']) not in {identity(u) for u in (job.get('source_url'), job.get('jobright_url')) if u}:
        raise ReadinessError('NEEDS_REVIEW', 'Resume metadata points to a different job URL/requisition')
    if digest((folder / 'jd.txt').read_bytes()) != version:
        raise ReadinessError('NEEDS_REVIEW', 'Resume source JD does not match scraper JD')
    verifier = json.loads((folder / '.verified.json').read_text())
    candidates = [Path(entry.get('pdf', '')), folder / verifier.get('pdf', 'missing')]
    exported = folder / '.exported.json'
    if exported.exists():
        raw = json.loads(exported.read_text()).get('export_path', '')
        if re.match(r'^[A-Za-z]:\\', raw):
            win = PureWindowsPath(raw)
            candidates.append(Path('/mnt') / win.drive[0].lower() / Path(*win.parts[1:]))
        elif raw:
            candidates.append(Path(raw))
    pdf = next((p.resolve() for p in candidates if p.suffix.lower() == '.pdf' and p.is_file()), None)
    if pdf is None:
        raise ReadinessError('WAITING_FOR_RESUME', 'Completed PDF unavailable locally or on export drive; restore the exact verified artifact')
    content = pdf.read_bytes()
    sha = digest(content)
    if not content.startswith(b'%PDF-') or sha != verifier.get('pdf_sha256'):
        raise ReadinessError('NEEDS_REVIEW', 'PDF bytes differ from job-specific verifier hash')
    tex = folder / 'resume.tex'
    if not tex.is_file() or digest(tex.read_bytes()) != verifier.get('tex_sha256'):
        raise ReadinessError('NEEDS_REVIEW', 'Resume source changed since verification')
    validate_pdf(pdf)
    if approval_mode == 'manual' and (approvals or {}).get(job['id']) != sha:
        raise ReadinessError('NEEDS_REVIEW', APPROVAL_REASON, resume_path=str(pdf), resume_version=sha)
    acceptance_path = root / 'data/resumes/acceptance.json'
    accepted = json.loads(acceptance_path.read_text()) if acceptance_path.exists() else {}
    acceptance_key = key + ':' + sha + ':' + approval_mode
    if acceptance_key not in accepted:
        accepted[acceptance_key] = dict(job_id=job['id'], company=job['company'], position=job['title'],
            path=str(pdf), sha256=sha, status='READY_FOR_APPLICATION', mode=approval_mode,
            validation='technical_artifact_checks', accepted_at=now(), human_verified=False)
        save_json(acceptance_path, accepted)
    return dict(path=str(pdf), sha256=sha, folder=str(folder), jd=jd.read_text(), tex=tex.read_text(), version=key)

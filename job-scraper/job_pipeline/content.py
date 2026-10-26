"""Deterministic extraction and conservative cleaning; no network or model calls."""
import json
import re
import unicodedata
from urllib.parse import urlsplit, urlunsplit
from bs4 import BeautifulSoup


def original_posting_url(html):
    """Read the current Jobright posting's application URL without clicking Apply."""
    soup = BeautifulSoup(html, 'html.parser')
    for selector in ('script#jobright-helper-job-detail-info', 'script#__NEXT_DATA__'):
        script = soup.select_one(selector)
        if not script:
            continue
        try:
            data = json.loads(script.get_text())
        except (ValueError, TypeError):
            continue
        if selector.endswith('__NEXT_DATA__'):
            try:
                data = data['props']['pageProps']['dataSource']
            except (KeyError, TypeError):
                continue
        if not isinstance(data, dict):
            continue
        data = data.get('jobResult', data)
        if not isinstance(data, dict):
            continue
        for key in ('applyLink', 'originalUrl'):
            url = data.get(key)
            if not isinstance(url, str):
                continue
            url = url.strip()
            parsed = urlsplit(url)
            host = parsed.hostname or ''
            if (parsed.scheme in ('http', 'https') and host
                    and host != 'jobright.ai' and not host.endswith('.jobright.ai')):
                return url
    return ''


def slug(value):
    value = unicodedata.normalize('NFKD', value).encode('ascii', 'ignore').decode()
    return re.sub(r'[^a-z0-9]+', '-', value.lower()).strip('-')[:70] or 'unknown'


def normalized(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def link_key(url):
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path.rstrip('/'), p.query, ''))


def duplicate(candidate, rows):
    links = {link_key(candidate.get(k, '')) for k in ('jobright_url', 'source_url')} - {''}
    pair = (normalized(candidate['company']), normalized(candidate['title']))
    return any(links.intersection({link_key(r.get(k, '')) for k in ('jobright_url', 'source_url')} - {''})
               or pair == (normalized(r['company']), normalized(r['title'])) for r in rows)


def clean_text(text):
    lines = [re.sub(r'[ \t]+', ' ', x).strip() for x in text.splitlines()]
    # Only remove unambiguous standalone boilerplate, never truncate the JD.
    boilerplate = re.compile(r'^(accept all cookies|reject all cookies|manage cookies|cookie preferences|privacy policy|terms of use|all rights reserved)[.!]?$|^we are an equal opportunity employer\b.*$', re.I)
    return '\n'.join(x for x in lines if x and not boilerplate.match(x))


def extract(html, url, selectors):
    soup = BeautifulSoup(html, 'html.parser')
    def postings(obj):
        if isinstance(obj, dict):
            types = obj.get('@type', [])
            if 'JobPosting' in (types if isinstance(types, list) else [types]):
                yield obj
            for value in obj.values():
                yield from postings(value)
        elif isinstance(obj, list):
            for value in obj:
                yield from postings(value)
    for node in soup.select(selectors['json_ld']):
        try:
            for posting in postings(json.loads(node.string or node.get_text())):
                desc = posting.get('description')
                if isinstance(desc, str) and desc.strip():
                    return clean_text(BeautifulSoup(desc, 'html.parser').get_text('\n')), 'json-ld'
        except (ValueError, TypeError):
            pass
    for node in soup.select(selectors['remove']):
        node.decompose()
    host = urlsplit(url).netloc.lower()
    if host == 'jobright.ai' or host.endswith('.jobright.ai'):
        # The JD spans sibling sections; the largest block alone loses content.
        sections = []
        for section in soup.select('[class*="index_jobDetailContent__"] section'):
            heading = section.find(['h2', 'h3'])
            if heading and re.fullmatch(r'responsibilities|qualifications?|requirements?|benefits',
                                        heading.get_text(' ', strip=True), re.I):
                sections.append(section.get_text('\n', strip=True))
        if sections:
            return clean_text('\n'.join(sections)), 'jobright'
    for name, selector in selectors['ats'].items():
        if name in host or (name == 'workday' and 'myworkdayjobs' in host):
            blocks = [n.get_text('\n', strip=True) for n in soup.select(selector)]
            if blocks:
                return clean_text(max(blocks, key=len)), name
    if host == 'jobright.ai' or host.endswith('.jobright.ai'):
        # A feed, login shell, or recommendations panel is not a description.
        return '', 'jobright'
    blocks = [n.get_text('\n', strip=True) for n in soup.select(selectors['main'])]
    return clean_text(max(blocks, key=len) if blocks else (soup.body or soup).get_text('\n', strip=True)), 'main'


def review_reasons(text, raw, url, http_status=None, login_visible=False, verification_visible=False):
    reasons = []
    path = urlsplit(url).path.lower()
    if login_visible or re.search(r'/(login|signin|sign-in|auth)(/|$)', path) or re.search(r'(sign in|log in) to (view|continue|access)', raw, re.I):
        reasons.append('Page requires login')
    if verification_visible or re.search(r'verify (you are|that you are) human|checking your browser|captcha|just a moment', raw, re.I):
        reasons.append('Captcha or verification; try --headed if headless')
    if (http_status and http_status >= 400) or re.search(r'access denied|request blocked|unusual traffic|paywall|subscribe to (read|view)', raw, re.I):
        reasons.append('Site blocked request or requires paid access; try --headed if headless')
    if len(text) < 400:
        reasons.append('Description under 400 characters')
    if not re.search(r'responsibilit|requirements?|qualifications?|what you.ll (do|bring)|what we.re looking for|you will|you have', text, re.I):
        reasons.append('No requirements or responsibilities section detected')
    return reasons

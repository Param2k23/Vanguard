"""Exact reviewed destination scopes. No global portal permissions are changed."""
from urllib.parse import urlsplit, parse_qs, urljoin
from .integration import canonical_url


def scope(url):
    url = canonical_url(url)
    p = urlsplit(url)
    if p.scheme != 'https' and p.hostname not in ('localhost', '127.0.0.1', '::1'):
        raise ValueError('Application destination requires HTTPS')
    host = p.hostname or ''
    if host == 'jobright.ai' or host.endswith('.jobright.ai'):
        raise ValueError('Jobright listing is not an application destination')
    parts = [x for x in p.path.split('/') if x]
    shared = host in ('boards.greenhouse.io', 'job-boards.greenhouse.io', 'jobs.lever.co', 'jobs.smartrecruiters.com', 'jobs.ashbyhq.com')
    if shared and not parts:
        raise ValueError('ATS tenant missing')
    prefix = '/' + parts[0] + '/' if shared else '/'
    tenant = parts[0] if shared else host
    query_tenant = ''
    if host in ('boards.greenhouse.io','job-boards.greenhouse.io') and parts and parts[0] == 'embed':
        values = parse_qs(p.query).get('for', [])
        if len(values) != 1 or not values[0]:
            raise ValueError('Embedded ATS tenant missing')
        tenant = values[0]
        query_tenant = 'for'
    if host == 'www.myworkdaysite.com':
        if len(parts) < 3 or parts[0] != 'recruiting':
            raise ValueError('Workday tenant missing')
        prefix = '/' + '/'.join(parts[:3]) + '/'
        tenant = '/'.join(parts[1:3])
    result = dict(url=url, origin=f'{p.scheme}://{p.netloc}', tenant=tenant, path_prefix=prefix)
    if query_tenant:
        result['tenant_query_key'] = query_tenant
    return result


def within(url, approved):
    try:
        p = urlsplit(canonical_url(url))
        key = approved.get('tenant_query_key')
        if key and parse_qs(p.query).get(key) != [approved['tenant']]:
            return False
        return f'{p.scheme}://{p.netloc}' == approved['origin'] and (p.path + '/').startswith(approved['path_prefix'])
    except ValueError:
        return False


def enforce(page, approved):
    """Block unexpected document redirects and cross-origin personal-data requests."""
    class GuardedRequests(list):
        pass
    blocked = GuardedRequests()
    def route(request_route):
        request = request_route.request
        protected = request.is_navigation_request() or request.method not in ('GET', 'HEAD', 'OPTIONS')
        popup = False
        if request.is_navigation_request():
            try:
                popup = request.frame.page != page
            except Exception:
                popup = True
        if protected and (popup or not within(request.url, approved)):
            blocked.append(request.url)
            request_route.abort()
        else:
            # Preserve browser-owned multipart uploads, including file bytes.
            request_route.continue_()

    # Request routing alone does not intercept every HTTP redirect hop. Chromium
    # response interception checks Location BEFORE the browser can forward a POST.
    # Using the actual browser transport also preserves multipart PDF bytes.
    context = page.context
    session = context.new_cdp_session(page)
    def response_paused(event):
        request_id = event['requestId']
        request = event['request']
        protected = event.get('resourceType') == 'Document' or request['method'] not in ('GET', 'HEAD', 'OPTIONS')
        status = event.get('responseStatusCode', 0)
        headers = {h['name'].lower(): h['value'] for h in event.get('responseHeaders', [])}
        if protected and status in (301, 302, 303, 307, 308):
            target = urljoin(request['url'], headers.get('location', ''))
            if not within(target, approved) or status in (307, 308) and request['method'] not in ('GET', 'HEAD'):
                blocked.append(target)
                session.send('Fetch.failRequest', dict(requestId=request_id, errorReason='BlockedByClient'))
                return
        session.send('Fetch.continueRequest', dict(requestId=request_id))
    session.on('Fetch.requestPaused', response_paused)
    session.send('Fetch.enable', dict(patterns=[dict(urlPattern='*', requestStage='Response')]))
    context.route('**/*', route)
    def close():
        context.unroute('**/*', route)
        session.send('Fetch.disable')
        session.detach()
    blocked.close = close
    return blocked

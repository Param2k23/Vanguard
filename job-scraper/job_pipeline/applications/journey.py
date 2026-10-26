"""Evidence-based navigation for one CSV-authorized application, never global trust."""
import re
import time
from urllib.parse import urlsplit, urljoin, parse_qsl, urlencode, urlunsplit
from .integration import canonical_url, normalize
from .destinations import scope, within
from .browser import Blocked, platform

ATS = ('myworkdayjobs.com','myworkdaysite.com','greenhouse.io','greenhouse.com','lever.co','eightfold.ai',
       'ashbyhq.com','icims.com','smartrecruiters.com','ultipro.com','ukg.com','paycomonline.net','isolvedhire.com','adp.com','jobvite.com','applytojob.com')

def display_url(url):
    p=urlsplit(url)
    query=[(k,v) for k,v in parse_qsl(p.query) if k.lower() in ('pid','jobid','job_id','opportunityid','gh_jid','for','reqid') or k.lower()=='token' and (p.hostname or '').endswith('greenhouse.io') and v.isdigit()]
    return urlunsplit((p.scheme,p.netloc,p.path,urlencode(query),''))


def journey_scope(url):
    """More detailed browser scope without changing existing CSV approval hashes."""
    result=scope(url)
    p=urlsplit(url);parts=[x for x in p.path.split('/') if x]
    host=p.hostname or ''
    if host.endswith('.paycomonline.net') and 'portal' in parts:
        index=parts.index('portal')
        if len(parts)>index+1:
            result.update(path_prefix='/'+'/'.join(parts[:index+2])+'/',tenant=parts[index+1],identified_tenant=True)
    elif host.startswith('recruiting') and host.endswith('.ultipro.com') and parts:
        result.update(path_prefix='/'+parts[0]+'/',tenant=parts[0],identified_tenant=True)
    return result


def requisitions(url):
    p=urlsplit(url); values=set()
    for k,v in parse_qsl(p.query):
        if (k.lower() in ('pid','jobid','job_id','opportunityid','gh_jid','reqid','requisitionid','listing_id') or k.lower()=='token' and 'greenhouse' in (p.hostname or '') and v.isdigit()) and re.fullmatch(r'[\w-]{3,}',v):
            values.add(v.casefold())
    values.update(x.casefold() for x in re.findall(r'/(?:jobs?|opportunities)/([0-9a-f-]{5,})(?=/|$)',p.path,re.I))
    values.update(x.casefold() for x in re.findall(r'[_/](R?-?\d{4,})(?=/|$)',p.path,re.I))
    return values


def title_match(expected, actual):
    ignore={'junior','entry','level','sr','senior','jr','the','a','and','of','i','ii','iii'}
    a=set(normalize(expected).split())-ignore;b=set(normalize(actual).split())-ignore
    return bool(a and b and len(a & b)/max(len(a),len(b))>=0.7)


class ApplicationJourney:
    def __init__(self,page,job,config,store):
        self.page=page;self.job=job;self.config=config;self.store=store
        self.seed=journey_scope(job['source_url']);self.trusted=[self.seed];self.edges=[];self.events=[];self.sessions=[]
        self.step='Opening approved application URL';self.verified=False;self.diagnostics=[]
        self.log('APPLICATION',job['title'])

    def log(self,kind,message,url=None):
        if kind in ('AUTH','RESUME','FIELD','QUESTIONS','VALIDATION'):self.step=message
        event=dict(kind=kind,step=self.step,message=message,url=display_url(url or self.page.url))
        self.events.append(event)
        print(f'[{kind}] {self.job["company"]} — {message}'+(f' — {event["url"]}' if url else ''),flush=True)
        if self.store.get(self.job['id']):
            self.store.update(self.job['id'],current_step=self.step,last_observed_url=event['url'])

    def secure(self,url):
        try:scope(url);return True
        except ValueError:return False

    def trusted_url(self,url):
        candidate=journey_scope(url)
        identified=[s for s in self.trusted if s.get('identified_tenant') and s['origin']==candidate['origin']]
        if candidate.get('identified_tenant') and identified and not any(s['tenant']==candidate['tenant'] for s in identified):return False
        return any(within(url,s) for s in self.trusted)

    def edge(self,source,target,kind):
        if self.secure(target):
            self.edges.append((source,target,kind))
            self.log('NAVIGATION',kind+': '+display_url(source)+' → '+display_url(target),target)

    def linked(self,url):
        return any(self.trusted_url(a) and (canonical_url(b)==canonical_url(url) or within(url,journey_scope(b))) for a,b,_ in self.edges)

    def authorize(self,page):
        """Observe the employer/requisition before allowing any personal-data request."""
        page.locator('body').wait_for(state='visible',timeout=15000)
        url=page.url
        if not self.secure(url):raise Blocked('NEEDS_REVIEW','Destination is not a secure employer/application URL: '+display_url(url))
        text=normalize(page.locator('body').inner_text())
        if 'verify you are human' in text or 'checking your browser' in text or 'security verification' in text:
            raise Blocked('BLOCKED_CAPTCHA','Observed browser verification/access challenge; manual action required')
        headings=page.locator('h1,h2,[data-job-title]').all_inner_texts()
        company=normalize(self.job['company']);words=company.split()
        company_seen=bool(re.search(r'(?<!\w)'+re.escape(company)+r'(?!\w)',text)) or (len(words)>1 and words[0] not in ('the','global','united') and re.search(r'\b'+re.escape(words[0])+r'\b',text) is not None)
        matching=any(title_match(self.job['title'],h) for h in headings)
        expected_ids=requisitions(self.job['source_url']);actual_ids=requisitions(url)
        same_id=bool(expected_ids & actual_ids)
        linked=self.linked(url)
        provider_changed=urlsplit(url).netloc!=urlsplit(self.job['source_url']).netloc
        if expected_ids and actual_ids and not same_id and not (provider_changed and linked and matching and company_seen):
            raise Blocked('NEEDS_REVIEW',f'Different requisition: approved {sorted(expected_ids)}; destination {sorted(actual_ids)} at {display_url(url)}')
        structured=page.locator('script[type="application/ld+json"]').all_text_contents()
        import json
        for raw in structured:
            try:
                data=json.loads(raw)
                entries=data if isinstance(data,list) else data.get('@graph',[data])
                for entry in entries:
                    if not isinstance(entry,dict) or entry.get('@type')!='JobPosting':continue
                    organization=entry.get('hiringOrganization',{})
                    name=organization.get('name','') if isinstance(organization,dict) else ''
                    normalized=normalize(name)
                    if normalized and not (company in normalized or normalized in company or words and words[0] in normalized.split()):
                        raise Blocked('NEEDS_REVIEW','Structured employer mismatch: destination identifies '+name+'; approved employer is '+self.job['company'])
            except Blocked:
                raise
            except (ValueError,TypeError,AttributeError):
                continue
        host=urlsplit(url).hostname or ''
        known_ats=any(host==a or host.endswith('.'+a) for a in ATS)
        tenant_company=bool(words and len(words[0])>=3 and words[0] in normalize(host).split())
        linked=self.linked(url)
        trusted=self.trusted_url(url)
        login=page.locator('input[type=password]:visible').count()>0 or re.search(r'login|sign in|create (?:an )?account',text)
        generic_headings={'apply','apply now','application','application form','personal information','my information','education','experience','sign in','login','create account','contact information'}
        specific=[h for h in headings if normalize(h) not in generic_headings and re.search(r'engineer|analyst|scientist|developer|manager|technician|designer',h,re.I)]
        if specific and not matching and not same_id:
            raise Blocked('NEEDS_REVIEW','Position differs from approved job: '+ ' / '.join(specific[:3])+'; expected '+self.job['title'])
        strong=(same_id and (trusted or company_seen or linked)) or (matching and (company_seen or tenant_company or trusted))
        continuation=self.verified and trusted
        current_scope=journey_scope(url)
        tenant_conflict=current_scope.get('identified_tenant') and any(s.get('identified_tenant') and s['origin']==current_scope['origin'] and s['tenant']!=current_scope['tenant'] for s in self.trusted) and not any(s.get('identified_tenant') and s['origin']==current_scope['origin'] and s['tenant']==current_scope['tenant'] for s in self.trusted)
        if tenant_conflict and not (matching and company_seen):raise Blocked('NEEDS_REVIEW','Different ATS tenant without matching employer/position evidence: '+display_url(url))
        auth_continuation=bool(login and (trusted or linked and known_ats) and (same_id or tenant_company or self.verified or linked))
        if not (strong or continuation or auth_continuation):
            raise Blocked('NEEDS_REVIEW','Cannot associate destination with approved employer/requisition: '+display_url(url)+'; employer evidence='+str(bool(company_seen))+'; matching title='+str(matching)+'; linked from verified page='+str(linked))
        if not trusted:
            self.trusted.append(journey_scope(url))
            self.log('NAVIGATION','Associated destination with intended employer/application',url)
        if strong:self.verified=True
        if matching and (actual_ids or platform(url) in ('greenhouse','lever')):
            from .integration import identity
            from ..company_policy import company_key
            key=('employer-requisition:'+company_key(self.job['company'])+':'+(urlsplit(url).hostname or '')+':'+','.join(sorted(actual_ids))) if actual_ids else identity(url)
            duplicate=self.store.duplicate(identity(url),self.job['id']) or self.store.duplicate(key,self.job['id'])
            if not duplicate and actual_ids:
                from .storage import PROTECTED
                for previous in self.store.rows():
                    old_url=previous.get('application_url','')
                    if previous['job_id']!=self.job['id'] and previous['status'] in PROTECTED and company_key(previous.get('company',''))==company_key(self.job['company']) and urlsplit(old_url).hostname==urlsplit(url).hostname and actual_ids.intersection(requisitions(old_url)):
                        duplicate=previous
                        break
            if duplicate:
                status='SUBMISSION_UNCONFIRMED' if duplicate['status']=='SUBMISSION_UNCONFIRMED' else 'ALREADY_APPLIED'
                raise Blocked(status,'Observed destination requisition already recorded for job '+duplicate['job_id'])
            self.store.update(self.job['id'],identity=key,observed_requisition_url=display_url(url))
        self.config['APPROVED_FORM_ORIGINS']=sorted({s['origin'] for s in self.trusted})
        self.config['APPROVED_SUBMIT_ORIGINS']=self.config['APPROVED_FORM_ORIGINS'][:]
        self.config['REVIEWED_DESTINATION']['destination']=journey_scope(url)
        return True

    def install(self):
        context=self.page.context
        def route(r):
            request=r.request
            if request.is_navigation_request() and request.method in ('GET','HEAD'):
                if not self.secure(request.url):r.abort();return
                redirected=request.redirected_from
                if redirected:self.edge(redirected.url,request.url,'Observed HTTP redirect')
                r.continue_();return
            if request.method not in ('GET','HEAD','OPTIONS') and not self.trusted_url(request.url):
                self.log('SECURITY','Blocked personal-data request to unrelated destination',request.url);r.abort();return
            r.continue_()
        self.route=route;context.route('**/*',route)
        def attach(page):
            session=context.new_cdp_session(page);self.sessions.append(session)
            def paused(e):
                request=e['request'];rid=e['requestId'];status=e.get('responseStatusCode',0)
                headers={h['name'].lower():h['value'] for h in e.get('responseHeaders',[])}
                if status in (301,302,303,307,308):
                    target=urljoin(request['url'],headers.get('location',''))
                    if e.get('resourceType')=='Document':self.edge(request['url'],target,'Observed HTTP redirect')
                    if status in (307,308) and request['method'] not in ('GET','HEAD') and not self.trusted_url(target):
                        self.log('SECURITY','Blocked credential/body forwarding redirect',target)
                        session.send('Fetch.failRequest',dict(requestId=rid,errorReason='BlockedByClient'));return
                session.send('Fetch.continueRequest',dict(requestId=rid))
            session.on('Fetch.requestPaused',paused)
            session.send('Fetch.enable',dict(patterns=[dict(urlPattern='*',requestStage='Response')]))
        self.attach=attach;attach(self.page)
        def newpage(page):
            if page!=self.page:attach(page)
        self.newpage=newpage;context.on('page',newpage)

    def locate(self):
        """Follow observed Apply controls, including popups, until form/auth controls exist."""
        for _ in range(self.config.get('MAX_STEPS',12)):
            page=self.page
            page.wait_for_timeout(700)
            self.authorize(page)
            if page.locator('input[type=password]:visible,input[autocomplete=one-time-code]:visible').count():return page
            if page.locator('input[type=file],input[type=email]:visible,input[autocomplete=given-name]:visible').count():
                self.log('FORM','Application controls detected',page.url);return page
            controls=page.get_by_role('link',name=re.compile(r'^(apply(?: now| for (?:this )?(?:job|position))?|apply with resume|start application|create account|sign in)$',re.I))
            buttons=page.get_by_role('button',name=re.compile(r'^(apply(?: now| for (?:this )?(?:job|position))?|start application|apply manually)$',re.I))
            nodes=[n for n in controls.all()+buttons.all() if n.is_visible()]
            # Prefer application entry over utility Sign In links.
            application_nodes=[n for n in nodes if re.match(r'^apply|^start application',n.inner_text().strip(),re.I)]
            if application_nodes:nodes=application_nodes
            if len(nodes)!=1:
                # SPA controls may arrive after the initial document.
                page.wait_for_timeout(1500)
                if page.locator('input:visible,select:visible,textarea:visible,[role=combobox]:visible').count():return page
                auth=page.get_by_role('button',name=re.compile(r'^(sign in|log in|create account)$',re.I))
                if auth.count() and page.get_by_role('dialog').count():return page
                if not nodes:continue
                raise Blocked('NOT_SUPPORTED',f'Analyzed page: {len(nodes)} ambiguous Apply controls; cannot choose intended form at '+display_url(page.url))
            node=nodes[0];href=node.get_attribute('href')
            old=page.url
            if href:self.edge(old,urljoin(old,href),'Observed Apply link')
            pages=set(page.context.pages);self.step='Following Apply control';self.log('NAVIGATION','Following observed Apply control',old)
            node.click();page.wait_for_timeout(700)
            new=[p for p in page.context.pages if p not in pages]
            if len(new)>1:raise Blocked('NEEDS_REVIEW','Apply opened multiple destinations; cannot identify intended application')
            if new:self.page=new[0];self.page.wait_for_load_state('domcontentloaded');self.edge(old,self.page.url,'Observed Apply popup')
            elif page.url!=old:self.edge(old,page.url,'Observed Apply navigation')
        raise Blocked('NOT_SUPPORTED','Analyzed rendered page through bounded waits; no uniquely actionable application controls at '+display_url(self.page.url))

    def close(self):
        self.page.context.unroute('**/*',self.route)
        self.page.context.remove_listener('page',self.newpage)
        for session in self.sessions:
            try:session.send('Fetch.disable');session.detach()
            except Exception:pass

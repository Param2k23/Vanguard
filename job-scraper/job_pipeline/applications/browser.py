"""Semantic native-form adapter. Complex ATS widgets fail closed, never guessed."""
import json
import re
import time
from urllib.parse import urljoin, urlsplit
from pathlib import Path

from .integration import application_url, canonical_url, identity, normalize
from .profile import answer_field
from .storage import digest, now, save_json


class Blocked(ValueError):
    def __init__(self, status, reason, question='', suggestion=None):
        self.status, self.question, self.suggestion = status, question, suggestion
        super().__init__(reason)


def platform(url):
    host = (urlsplit(url).hostname or '').lower()
    for name, suffixes in {
        'greenhouse': ('greenhouse.io', 'greenhouse.com'), 'lever': ('lever.co',),
        'ashby': ('ashbyhq.com',), 'workday': ('myworkdayjobs.com', 'myworkdaysite.com'),
        'smartrecruiters': ('smartrecruiters.com',), 'icims': ('icims.com',),
        'linkedin': ('linkedin.com',), 'jobright': ('jobright.ai',), 'eightfold': ('eightfold.ai',),
    }.items():
        if any(host == s or host.endswith('.' + s) for s in suffixes):
            return name
    return 'generic'


def origin(url):
    p = urlsplit(url)
    return f'{p.scheme}://{p.netloc}'


INSPECT = r'''() => { document.querySelectorAll('[data-pipeline3-field]').forEach(e=>e.removeAttribute('data-pipeline3-field')); return [...document.querySelectorAll('input,textarea,select,[role="combobox"],[role="checkbox"],[role="radio"],[contenteditable="true"]')]
.filter(el => el.getClientRects().length && !el.disabled && (!['hidden','submit','button','reset'].includes(el.type)||['combobox','checkbox','radio'].includes(el.getAttribute('role'))))
.map((el,i) => {
 const key='pipeline3-'+i; el.setAttribute('data-pipeline3-field',key);
 const named=el.getAttribute('aria-labelledby');
 const aria=named ? named.split(/\s+/).map(x=>document.getElementById(x)?.textContent||'').join(' ') : '';
 const labelText=x=>{const copy=x.cloneNode(true);copy.querySelectorAll('input,textarea,select,button').forEach(n=>n.remove());return copy.textContent;};
 const own=el.getAttribute('aria-label')||aria||[...(el.labels||[])].map(labelText).join(' ')||el.placeholder||(el.getAttribute('role')?el.textContent:'')||'';
 const group=el.closest('fieldset'); const legend=group?.querySelector('legend')?.textContent||'';
 const container=el.closest('[data-history-section],fieldset,[data-automation-id=education],[data-automation-id=workExperience]');
 const section=container?.getAttribute('data-history-section')||container?.querySelector('legend,h2,h3')?.textContent||'';
 const peers=container?[...document.querySelectorAll('[data-history-section],fieldset,[data-automation-id=education],[data-automation-id=workExperience]')].filter(x=>(x.getAttribute('data-history-section')||x.querySelector('legend,h2,h3')?.textContent||'')===section):[];
 return {key,section,section_index:Math.max(0,peers.indexOf(container)),role:el.getAttribute('role')||'',label:own.trim(),group:legend.trim(),type:el.getAttribute('role')||el.type||el.tagName.toLowerCase(),
 tag:el.tagName.toLowerCase(),required:el.required||el.getAttribute('aria-required')==='true',
 max_chars:el.maxLength>0?el.maxLength:null,value:el.value||'',checked:!!el.checked||el.getAttribute('aria-checked')==='true',
 options:el.options?[...el.options].map(x=>({label:x.label,value:x.value,disabled:x.disabled})):[],
 accept:el.accept||'',autocomplete:el.autocomplete||'',validation:el.validationMessage||''};
}) }'''


class NativeFormAdapter:
    """Only standard HTML controls; every interaction is read back and audited."""
    supported_platforms = {'generic', 'greenhouse', 'lever', 'eightfold'}

    def __init__(self, page, config, profile, store, job, resume, directory, answer_generator=None):
        self.page, self.config, self.profile, self.store = page, config, profile, store
        self.job, self.resume, self.directory = job, resume, Path(directory)
        self.answer_generator = answer_generator
        self.audit = []
        self.uploaded = False
        self.submission_attempted = False
        self.confirmation_before = False

    def inspect_fields(self):
        fields=self.page.evaluate(INSPECT)
        if hasattr(self,'form_scope'):
            keys=set(self.form_scope.locator('[data-pipeline3-field]').evaluate_all('(nodes)=>nodes.map(n=>n.getAttribute("data-pipeline3-field"))'))
            fields=[f for f in fields if f['key'] in keys]
        return fields

    def guard(self):
        p = self.page
        reviewed = self.config.get('REVIEWED_DESTINATION')
        journey=self.config.get('APPLICATION_JOURNEY')
        if journey:
            journey.authorize(p)
        if reviewed:
            from .destinations import within
            if not within(p.url, reviewed['destination']):
                raise Blocked('NEEDS_REVIEW', 'Unexpected application origin or ATS tenant')
            # Check native form destinations before any personal information is filled.
            scoped_forms=[self.form_scope] if hasattr(self,'form_scope') else p.locator('form:visible').all()
            for form in scoped_forms:
                if not form.locator('input[type=file],input[type=email],input[type=password]').count():continue
                if not within(urljoin(p.url, form.get_attribute('action') or p.url), reviewed['destination']):
                    if journey and journey.trusted_url(urljoin(p.url,form.get_attribute('action') or p.url)):
                        continue
                    raise Blocked('NEEDS_REVIEW', 'Application form posts to an unassociated destination: '+__import__('job_pipeline.applications.journey',fromlist=['display_url']).display_url(urljoin(p.url,form.get_attribute('action') or p.url)))
        if p.locator('iframe[src*="captcha"], .g-recaptcha, #challenge-running, [data-sitekey]').count():
            raise Blocked('BLOCKED_CAPTCHA', 'Verification challenge detected; solve manually in authenticated browser')
        if p.locator('input[type=password]:visible, input[autocomplete=one-time-code]:visible').count() or re.search(r'/login(?:[/?]|$)|/signin(?:[/?]|$)', p.url, re.I):
            raise Blocked('BLOCKED_AUTH', 'Login/MFA required; authenticate manually using login mode')
        text = p.locator('body').inner_text()
        if re.search(r'position (?:has been |is )?(?:closed|filled)|no longer accepting applications|job (?:is )?no longer available', text, re.I):
            raise Blocked('JOB_CLOSED', 'Page explicitly reports the position closed')
        if re.search(r'automated (?:access|applications|submissions) (?:is |are )?(?:not allowed|prohibited)', text, re.I):
            raise Blocked('NEEDS_REVIEW', 'Page prohibits automated access/applications')
        return text

    def validate_identity(self):
        journey=self.config.get('APPLICATION_JOURNEY')
        if journey:
            journey.authorize(self.page)
            self.store.update(self.job['id'],ats_platform=platform(self.page.url))
            return
        text = normalize(self.guard())
        headings = [normalize(x) for x in self.page.locator('h1,h2,[data-job-title]').all_inner_texts()]
        from .journey import title_match
        if not any(title_match(self.job['title'],h) for h in headings) or not re.search(r'(?<!\w)' + re.escape(normalize(self.job['company'])) + r'(?!\w)', text):
            raise Blocked('NEEDS_REVIEW', 'Destination company or exact position heading not verified')
        target = application_url(self.job)
        if platform(target) == 'jobright' or identity(target) != identity(self.page.url):
            raise Blocked('NEEDS_REVIEW', 'Redirect/requisition identity requires verification; save verified source_url before retrying')
        if origin(self.page.url) not in self.config.get('APPROVED_FORM_ORIGINS', []):
            raise Blocked('NEEDS_REVIEW', 'Destination origin has not been approved for form automation')
        self.store.update(self.job['id'], identity=identity(self.page.url), application_url=canonical_url(self.page.url), ats_platform=platform(self.page.url))

    def inspect_confirmation(self):
        # Whole visible confirmation element, not arbitrary JD body substring.
        nodes = self.page.locator('[role=status], [role=alert], h1, h2, .application-confirmation, [data-application-confirmation]')
        for node in nodes.all():
            if not node.is_visible():
                continue
            text = node.inner_text().strip()
            if re.fullmatch(r'(?:your )?application (?:has been |was )?(?:successfully )?(?:submitted|received)[.!]?(?:\s+(?:Confirmation|Reference)(?: ID| number)?[: #]+[\w-]+)?', text, re.I):
                return text[:500]
        return ''

    def inspect_errors(self):
        validation_scope=self.form_scope if hasattr(self,'form_scope') else self.page
        invalid = validation_scope.locator('input:invalid:visible, select:invalid:visible, textarea:invalid:visible, [aria-invalid=true]:visible')
        messages = self.page.locator('[role=alert]:visible, .field-error:visible, [data-error]:visible').all_inner_texts()
        return invalid.count() > 0 or any(m.strip() for m in messages)

    def verified_override(self, question):
        key = digest(json.dumps([self.job['id'], question, self.profile['version']], ensure_ascii=False).encode())
        record = self.store.cache('verified:' + key)
        if record and record.get('verification_status') == 'user_verified':
            return dict(answer=record['answer'], confidence=1.0, requires_review=False,
                        question_category='reviewed', evidence_sources=['user review ' + key], reason='')
        return None

    def expand_verified_history(self):
        for kind,pattern in [('education',r'^add (?:another )?education(?: entry)?$'),('employment',r'^add (?:another )?(?:work experience|employment)(?: entry)?$')]:
            records=self.profile.get(kind,[])
            for _ in range(min(len(records),8)):
                fields=self.inspect_fields()
                instances={f.get('section_index',0) for f in fields if kind in normalize(f.get('section','')) or kind=='employment' and 'work experience' in normalize(f.get('section',''))}
                if len(instances)>=len(records):break
                buttons=self.page.get_by_role('button',name=re.compile(pattern,re.I))
                if buttons.count()!=1 or not buttons.first.is_visible() or buttons.first.get_attribute('type')!='button':break
                before=len(fields)
                buttons.first.click()
                self.page.wait_for_timeout(200)
                if self.config.get('APPLICATION_JOURNEY'):self.config['APPLICATION_JOURNEY'].log('FIELD','Added visible '+kind+' section for verified profile history')
                if len(self.inspect_fields())<=before:break

    def fill_fields(self):
        blockers = []
        seen_radios = set()
        # Reinspect after every mutation to catch conditional questions and ATS parsing.
        for _ in range(8):
            fields = self.inspect_fields()
            signature = json.dumps([(f['label'], f['group'], f['type']) for f in fields])
            blockers = []
            seen_radios.clear()
            from collections import Counter
            repeated = Counter((normalize(f['label']),f.get('section',''),f.get('section_index',0)) for f in fields if f['type'] != 'radio' and f['label'])
            for field in fields:
                label = field['group'] if field['type'] == 'radio' else field['label']
                control = self.page.locator(f'[data-pipeline3-field="{field["key"]}"]')
                char_limit = re.search(r'(\d+)\s*(?:characters|chars)\b', label, re.I)
                word_limit = re.search(r'(\d+)\s*words\b', label, re.I)
                if char_limit:
                    field['max_chars'] = min(field.get('max_chars') or int(char_limit[1]), int(char_limit[1]))
                field['max_words'] = int(word_limit[1]) if word_limit else None
                result = dict(field, expected_answer_type=field['type'], question_category='unknown', data_source=None, proposed_answer=None, confidence=0.0, validation_outcome='unfilled')
                if not label:
                    if field['required']:
                        blockers.append(('Unlabelled required field', 'Required control has no reliable accessible label', None))
                    self.audit.append(result)
                    continue
                if repeated[(normalize(label),field.get('section',''),field.get('section_index',0))] > 1 and field['type'] != 'radio':
                    blockers.append((label, 'Repeated field label needs a scoped education/employment adapter', None))
                    self.audit.append(dict(result, validation_outcome='review required'))
                    continue
                if field['type'] == 'file':
                    if not re.fullmatch(r'(?:upload |attach |your )?resume(?: upload)?\s*\*?', label, re.I):
                        if field['required']:
                            blockers.append((label, 'Required document is unsupported or missing', None))
                        self.audit.append(dict(result, question_category='document', validation_outcome='review required' if field['required'] else 'optional, skipped'))
                        continue
                    accept = field['accept'].lower()
                    if accept and not any(x in accept for x in ('pdf', '*', 'application/octet-stream')):
                        blockers.append((label, 'Employer requires a format other than matching PDF', None))
                        continue
                    if digest(Path(self.resume['path']).read_bytes()) != self.resume['sha256']:
                        raise Blocked('NEEDS_REVIEW', 'Resume artifact changed before upload')
                    # Reuse already accepted file only after verifying this session selected it.
                    if not (self.uploaded and control.evaluate('(e)=>e.files?.length===1')):
                        if self.config.get('APPLICATION_JOURNEY'):self.config['APPLICATION_JOURNEY'].log('RESUME','Uploading matching job-specific resume')
                        control.set_input_files(self.resume['path'])
                    accepted = control.evaluate('(e)=>e.files?.length===1 && e.validity.valid')
                    busy = self.page.locator('[aria-busy=true]:visible, [role=progressbar]:visible')
                    for node in busy.all():
                        node.wait_for(state='hidden', timeout=15000)
                    if not accepted:
                        blockers.append((label, 'Resume upload not accepted by file control', None))
                    else:
                        self.uploaded = True
                        self.store.update(self.job['id'], notes='Matching PDF selected; final server acceptance requires confirmation')
                    self.audit.append(dict(result, question_category='document', confidence=1.0 if accepted else 0.0, data_source='verified resume artifact', proposed_answer=self.resume['sha256'], validation_outcome='file control accepted' if accepted else 'failed'))
                    continue
                if field['type'] == 'radio':
                    if label in seen_radios:
                        continue
                    seen_radios.add(label)
                from .form_fields import scoped_answer
                resolved = scoped_answer(dict(field,label=label), self.profile, self.config['EEO_AUTOFILL_ENABLED'])
                if resolved['requires_review']:
                    resolved = self.verified_override(label) or resolved
                if resolved['requires_review'] and field['type'] in ('textarea', 'text') and self.answer_generator and re.match(r'(why |describe |tell us |what (?:excites|do you enjoy|is your|are your)|explain )', label, re.I):
                    try:
                        if self.config.get('APPLICATION_JOURNEY'):self.config['APPLICATION_JOURNEY'].log('QUESTIONS','Generating answer for technical question: '+label)
                        resolved = self.answer_generator(label, field.get('max_chars'), field.get('max_words'))
                    except (ValueError, OSError, TimeoutError) as error:
                        resolved = dict(resolved, reason='Answer generation failed: ' + type(error).__name__)
                result.update(resolved, data_source=resolved.get('evidence_sources'), proposed_answer=resolved.get('answer'))
                if resolved['requires_review'] or resolved['answer'] is None:
                    if field['required'] or field['checked'] or (field['value'] and field['type'] != 'radio') or field['type'] == 'radio' and any(f['required'] for f in fields if f['group'] == label):
                        blockers.append((label, resolved['reason'], resolved.get('answer')))
                    # Existing parsed/pre-filled values are never trusted for unknown required fields.
                    self.audit.append(result)
                    continue
                answer = resolved['answer']
                text = ('Yes' if answer else 'No') if isinstance(answer, bool) else str(answer)
                if (field.get('max_chars') and len(text) > field['max_chars']) or (field.get('max_words') and len(text.split()) > field['max_words']):
                    blockers.append((label, 'Verified answer exceeds field character limit', text))
                    continue
                try:
                    if self.config.get('APPLICATION_JOURNEY'):self.config['APPLICATION_JOURNEY'].log('FIELD','Filling verified field: '+label)
                    if field.get('role')=='combobox' or field.get('autocomplete') and control.get_attribute('aria-autocomplete')=='list':
                        from .form_fields import fill_combobox
                        valid=fill_combobox(self.page,control,text)
                    elif field['tag'] == 'select':
                        options = [o for o in field['options'] if normalize(o['label']) == normalize(text) and not o['disabled']]
                        if len(options) != 1:
                            raise ValueError('No unique exact dropdown option')
                        if control.input_value() != options[0]['value']:
                            control.select_option(value=options[0]['value'])
                        valid = control.input_value() == options[0]['value']
                    elif field['type'] == 'radio':
                        candidates = [f for f in fields if f['group'] == label and normalize(f['label']) == normalize(text)]
                        if len(candidates) != 1:
                            raise ValueError('No unique exact radio option')
                        control = self.page.locator(f'[data-pipeline3-field="{candidates[0]["key"]}"]')
                        if candidates[0]['tag']=='input':
                            control.check()
                            valid=control.is_checked()
                        else:
                            control.click()
                            valid=control.get_attribute('aria-checked')=='true'
                    elif field['type'] == 'checkbox':
                        if not isinstance(answer, bool):
                            raise ValueError('Checkbox needs verified boolean')
                        if field['tag']=='input':
                            control.set_checked(answer)
                            valid=control.is_checked()==answer
                        else:
                            if (control.get_attribute('aria-checked')=='true')!=answer:control.click()
                            valid=(control.get_attribute('aria-checked')=='true')==answer
                    elif field['tag'] in ('input', 'textarea') and field['type'] not in ('password', 'hidden'):
                        if field['type'] == 'month' and re.fullmatch(r'\d{4}-\d{2}', text):
                            pass
                        if control.input_value() != text:
                            control.fill(text)
                            control.press('Tab')
                        valid = control.input_value() == text
                    else:
                        raise ValueError('Custom widget requires a tested platform adapter')
                    if not valid or not control.evaluate('(e)=>!e.validity || e.validity.valid'):
                        raise ValueError('Browser read-back or native validation failed')
                    result['validation_outcome'] = 'validated'
                except Exception as error:
                    blockers.append((label, 'Unsupported control/validation failure: ' + type(error).__name__, text))
                    result['validation_outcome'] = 'failed'
                self.audit.append(result)
            current = self.inspect_fields()
            next_signature = json.dumps([(f['label'], f['group'], f['type']) for f in current])
            changed = signature != next_signature
            self.persist_progress()
            if not changed:
                break
        else:
            blockers.append(('Dynamic form', 'Conditional form did not stabilize after bounded inspections', None))
        if blockers:
            for question, reason, suggestion in blockers:
                self.store.review(self.job['id'], question, reason, suggested_answer=suggestion,
                                  application_url=canonical_url(self.page.url), current_state='Form filling', sources='See fields.json')
            raise Blocked('NEEDS_REVIEW', '; '.join(b[1] for b in blockers), blockers[0][0])
        if self.config.get('APPLICATION_JOURNEY'):self.config['APPLICATION_JOURNEY'].log('VALIDATION','Checking required fields and browser validation')
        if self.inspect_errors():
            raise Blocked('NEEDS_REVIEW', 'Visible browser validation errors remain')

    def persist_progress(self):
        save_json(self.directory / 'fields.json', self.audit)
        self.store.update(self.job['id'], generated_answers_path=str(self.directory / 'fields.json'))

    def submit(self):
        if not self.uploaded:
            raise Blocked('NEEDS_REVIEW', 'Matching resume upload has not been confirmed in this session')
        buttons = self.page.get_by_role('button', name=re.compile(r'^(submit application|send application|submit)$', re.I))
        if buttons.count() != 1:
            raise Blocked('NEEDS_REVIEW', 'No unique explicit final submit button')
        if not self.config['AUTO_SUBMIT_ENABLED'] or self.config['DRY_RUN']:
            if self.config.get('APPLICATION_JOURNEY'):self.config['APPLICATION_JOURNEY'].log('DRY RUN','Final submission disabled; fields validated')
            return dict(status='READY_TO_APPLY', notes='Dry run: fields validated; final submit was not clicked')
        if origin(self.page.url) not in self.config.get('APPROVED_SUBMIT_ORIGINS', []):
            raise Blocked('NEEDS_REVIEW', 'Origin is not approved for production submission')
        self.guard()
        if self.inspect_errors():
            raise Blocked('NEEDS_REVIEW', 'Validation error before submission')
        if self.inspect_confirmation():
            raise Blocked('NEEDS_REVIEW', 'Confirmation existed before submit; reconcile application manually')
        # Commit before click. A crash here may conservatively lose an opportunity but cannot duplicate a submission.
        self.store.update(self.job['id'], status='SUBMISSION_UNCONFIRMED', notes='Irreversible boundary recorded before single submit click')
        self.submission_attempted = True
        buttons.click(timeout=10000)
        deadline = time.monotonic() + 15
        evidence = ''
        while time.monotonic() < deadline:
            evidence = self.inspect_confirmation()
            if evidence:
                break
            self.page.wait_for_timeout(150)
        if evidence:
            match = re.search(r'(?:Confirmation|Reference)(?: ID| number)?[: #]+([\w-]+)', evidence, re.I)
            return dict(status='APPLIED', submitted_at=now(), confirmation_evidence=evidence,
                        confirmation_id=match[1] if match else '')
        return dict(status='SUBMISSION_UNCONFIRMED', review_reason='One submit click attempted; no unambiguous confirmation. Inspect portal before retrying')

    def run(self):
        forms=self.page.locator('form:visible')
        if forms.count()>1:
            upload_forms=[f for f in forms.all() if f.locator('input[type=file]').count()]
            candidates=upload_forms or [f for f in forms.all() if f.locator('input[type=email]').count()]
            if len(candidates)!=1:
                raise Blocked('NOT_SUPPORTED','Analyzed multiple visible forms; application controls cannot be uniquely scoped')
            self.form_scope=candidates[0]
        self.guard()
        self.validate_identity()
        if self.inspect_confirmation():
            raise Blocked('NEEDS_REVIEW','Existing confirmation requires manual reconciliation')
        previous = None
        for step in range(self.config.get('MAX_STEPS', 12)):
            self.guard()
            if origin(self.page.url) not in self.config.get('APPROVED_FORM_ORIGINS', []):
                raise Blocked('NEEDS_REVIEW', 'Multi-step form moved to an unapproved origin')
            if not self.page.locator('input:visible,select:visible,textarea:visible,[role=combobox]:visible').count() and self.page.locator('iframe:visible').count():
                raise Blocked('NOT_SUPPORTED','Analyzed page: required application controls are inside an embedded frame, not the main form')
            fields = self.inspect_fields()
            if not fields and step == 0:
                raise Blocked('NOT_SUPPORTED', 'No supported application controls found; open the actual application form')
            journey=self.config.get('APPLICATION_JOURNEY')
            if journey:
                journey.step=f'Application form step {step + 1}'
                journey.log('FORM',f'Inspecting application form step {step + 1}',self.page.url)
            self.store.update(self.job['id'], notes=f'Inspecting step {step + 1}')
            self.expand_verified_history()
            self.fill_fields()
            # Next buttons are allowed only as explicit non-submit controls. A type=submit
            # next button could submit an application and is intentionally unsupported.
            next_buttons = self.page.get_by_role('button', name=re.compile(r'^(next|continue|save and continue)$', re.I))
            if next_buttons.count() == 1:
                button = next_buttons.first
                if button.get_attribute('type') != 'button' and not self.config.get('APPLICATION_JOURNEY'):
                    raise Blocked('NEEDS_REVIEW', 'Next control is not explicitly type=button; irreversible behavior is ambiguous')
                signature = self.page.locator('body').inner_text() + json.dumps(self.inspect_fields())
                button.click()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline and self.page.locator('body').inner_text() + json.dumps(self.inspect_fields()) == signature:
                    self.page.wait_for_timeout(100)
                current = self.page.locator('body').inner_text() + json.dumps(self.inspect_fields())
                if current == signature or current == previous:
                    raise Blocked('NEEDS_REVIEW', 'Next step did not advance')
                previous = signature
                continue
            if next_buttons.count() > 1:
                raise Blocked('NEEDS_REVIEW', 'Ambiguous next-step controls')
            return self.submit()
        raise Blocked('NEEDS_REVIEW', 'Application exceeds bounded step limit')


def navigate(page, job, timeout):
    url = application_url(job)
    # Only safe navigation is retried; no submit/action retries.
    for attempt in range(3):
        try:
            response = page.goto(url, wait_until='domcontentloaded', timeout=timeout)
            if response and response.status in (404, 410):
                raise Blocked('JOB_CLOSED', 'Application returned HTTP 404/410')
            if response and response.status in (401, 403):
                raise Blocked('BLOCKED_AUTH', 'Site denied access; manual authentication/review required')
            if response and response.status >= 500:
                raise OSError('Transient server failure')
            page.locator('body').wait_for(state='visible', timeout=timeout)
            return
        except Blocked:
            raise
        except Exception:
            if attempt == 2:
                raise
            page.wait_for_timeout(500 * 2 ** attempt)

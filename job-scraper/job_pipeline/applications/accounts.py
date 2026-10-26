"""Bounded account automation for explicitly approved employer/tenant native forms."""
import json
import re
import secrets
import string
import time
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from ..company_policy import CompanyPolicy, company_key
from .browser import Blocked, origin, platform
from .identity import CONFIRMED_EMAIL
from .integration import application_url, canonical_url
from .profile import answer_field
from .storage import digest, now
from .vault import CredentialVault, VaultError

EXISTS = re.compile(r'account already exists|email (?:already )?registered|user already registered|existing candidate|account found|an account with this email already exists', re.I)
PASSWORD_REJECTED = re.compile(r'password (?:must|requires|is too|does not meet|should|needs)|invalid password|password requirements', re.I)
VERIFY = re.compile(r'verify your email|check your (?:email|inbox)|verification (?:link|code)|email verification|phone verification|multi.factor|two.factor', re.I)
ACCOUNT_STATUSES = {'ACCOUNT_CREATED','EXISTING_ACCOUNT','LOGGED_IN','VERIFICATION_REQUIRED','LOGIN_FAILED','PASSWORD_RECOVERY_REQUIRED','ACCOUNT_CREATION_FAILED','NEEDS_REVIEW'}


def account_identity(company, portal, email=CONFIRMED_EMAIL):
    scope = [company_key(company), origin(canonical_url(portal['origin'])), portal['tenant'].strip(), email.casefold()]
    return digest(json.dumps(scope).encode())[:32]


def legacy_password(company, suffix):
    letter = next((c for c in company if c.isalpha()), None)
    if not letter or not isinstance(suffix, str) or not suffix:
        raise VaultError('Historical password convention is not configured')
    return letter.upper() + suffix


def random_password(minimum=16, maximum=128, symbols='!@#$%_-'):
    length = max(16, minimum)
    if maximum < length:
        length = maximum
    if length < max(4, minimum) or not symbols:
        raise ValueError('Unsupported password length/symbol policy')
    chars = [secrets.choice(string.ascii_uppercase), secrets.choice(string.ascii_lowercase), secrets.choice(string.digits), secrets.choice(symbols)]
    pool = string.ascii_letters + string.digits + symbols
    chars += [secrets.choice(pool) for _ in range(length - len(chars))]
    secrets.SystemRandom().shuffle(chars)
    return ''.join(chars)


class AccountManager:
    def __init__(self, root, config, profile, store, job, vault=None):
        self.root, self.config, self.profile, self.store, self.job = Path(root), config, profile, store, job
        self.vault = vault
        self.attempts = 0
        self.account_id = None
        self.portal = None
        self.current_created = False
        self.current_login = False

    def update(self, **changes):
        if changes.get('account_status') and changes['account_status'] not in ACCOUNT_STATUSES:
            raise ValueError('Invalid account status')
        account = self.store.update_account(self.account_id, **changes)
        self.store.update(self.job['id'], portal_account_id=self.account_id,
                          portal_account_status=account['account_status'], login_required=True,
                          login_successful=self.current_login, account_created=self.current_created,
                          verification_required=account['account_status'] == 'VERIFICATION_REQUIRED',
                          authentication_error=account.get('last_error',''))
        return account

    def block(self, code, status='NEEDS_REVIEW'):
        if self.account_id:
            self.update(account_status=status, last_error=code)
        else:
            self.store.update(self.job['id'], login_required=True, login_successful=False, verification_required=status == 'VERIFICATION_REQUIRED', portal_account_status=status, authentication_error=code)
        self.store.review(self.job['id'], 'Portal authentication', code,
                          application_url=application_url(self.job), required_action='Authenticate/verify manually or import corrected credentials; no password reset is initiated')
        raise Blocked('NEEDS_REVIEW', code, 'Portal authentication')

    def challenge(self, page):
        if page.locator('iframe[src*="captcha"],.g-recaptcha,[data-sitekey],#challenge-running').count():
            self.block('CAPTCHA_REQUIRES_MANUAL_ACTION', 'VERIFICATION_REQUIRED')
        if page.locator('input[autocomplete=one-time-code]:visible').count() or VERIFY.search(page.locator('body').inner_text()):
            self.block('EMAIL_PHONE_OR_MFA_VERIFICATION_REQUIRED', 'VERIFICATION_REQUIRED')

    def choose_portal(self, page):
        if self.config.get('APPLICATION_JOURNEY'):self.config['APPLICATION_JOURNEY'].authorize(page)
        self.config_origin = origin(page.url)
        company = CompanyPolicy.load(self.root).evaluate(self.job['company'], self.job['id'])['canonical_company']
        candidates = [p for p in self.config.get('ACCOUNT_PORTALS', [])
                      if company_key(p.get('company','')) == company_key(company)
                      and origin(p.get('origin','')) == self.config_origin
                      and urlsplit(application_url(self.job)).path.startswith(p.get('application_path_prefix','/'))]
        reviewed = self.config.get('REVIEWED_DESTINATION')
        if reviewed:
            from .destinations import within
            if reviewed['job_id'] != self.job['id'] or not within(page.url, reviewed['destination']):
                self.block('AUTH_REDIRECT_ORIGIN_OR_TENANT_MISMATCH')
            if not candidates:
                # Native forms only. Endpoints come from the current page, never
                # invented provider URLs. Successful auth must expose our email.
                destination = reviewed['destination']
                candidates = [dict(company=company, origin=destination['origin'], tenant=destination['tenant'],
                    application_path_prefix=destination['path_prefix'], login_url='', registration_url='',
                    authenticated_selector='[data-applicant-email],[data-email]',
                    authenticated_email_selector='[data-applicant-email],[data-email]')]
        if len(candidates) != 1 or self.config_origin not in self.config.get('APPROVED_FORM_ORIGINS', []):
            self.block('AUTH_PORTAL_EMPLOYER_TENANT_NOT_APPROVED')
        self.portal = candidates[0]
        if not self.portal.get('tenant') or not self.portal.get('authenticated_selector'):
            self.block('AUTH_PORTAL_SCOPE_OR_SUCCESS_MARKER_MISSING')
        if urlsplit(page.url).scheme != 'https' and urlsplit(page.url).hostname not in ('127.0.0.1','localhost','::1'):
            self.block('AUTH_REQUIRES_HTTPS')
        self.account_id = account_identity(company, self.portal)
        prior = self.store.account(self.account_id) or {}
        associated = set(json.loads(prior.get('associated_job_ids') or '[]'))
        associated.add(self.job['id'])
        self.update(company=company, portal_name=platform(page.url), portal_domain=urlsplit(page.url).netloc,
                    login_url=self.endpoint('login_url'), email=CONFIRMED_EMAIL,
                    associated_job_ids=json.dumps(sorted(associated)), account_status=prior.get('account_status') or 'NEEDS_REVIEW')

    def endpoint(self, name):
        value = self.portal.get(name)
        if not value:
            return ''
        target = canonical_url(urljoin(self.portal['origin'], value))
        reviewed = self.config.get('REVIEWED_DESTINATION')
        if reviewed:
            from .destinations import within
            if not within(target, reviewed['destination']):
                self.block('AUTH_ENDPOINT_TENANT_MISMATCH')
        if origin(target) != origin(self.portal['origin']):
            self.block('CROSS_ORIGIN_AUTH_ENDPOINT_BLOCKED')
        return target

    def authenticated(self, page):
        nodes = page.locator(self.portal['authenticated_selector'])
        if nodes.count() != 1 or not nodes.first.is_visible() or page.locator('input[type=password]:visible').count():
            return False
        email_nodes = page.locator(self.portal.get('authenticated_email_selector') or self.portal['authenticated_selector'])
        if email_nodes.count() != 1:
            return False
        email_node = email_nodes.first
        value = email_node.get_attribute('data-applicant-email') or email_node.get_attribute('data-email') or email_node.get_attribute('value') or email_node.inner_text()
        emails = re.findall(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', value)
        return len(emails) == 1 and emails[0].casefold() == CONFIRMED_EMAIL.casefold()

    def initialize_vault(self):
        if not self.config.get('SECURE_CREDENTIAL_STORAGE', True):
            self.block('SECURE_CREDENTIAL_STORAGE_REQUIRED')
        try:
            if self.vault is None:
                self.vault = CredentialVault.from_environment(self.root)
            # Never silently create a vault or overwrite one under a wrong passphrase.
            self.vault.get('__health_check__')
        except (VaultError, OSError):
            self.block('CREDENTIAL_VAULT_UNAVAILABLE')

    def historical(self):
        path = self.root / 'account_secrets.json'
        try:
            if path.stat().st_mode & 0o077:
                raise VaultError('Secret configuration must have private permissions')
            suffix = json.loads(path.read_text())['LEGACY_PASSWORD_SUFFIX']
            return legacy_password(self.job['company'], suffix)
        except (OSError, ValueError, KeyError):
            self.block('LEGACY_PASSWORD_NOT_CONFIGURED')

    def verified_auth_form(self, page):
        reviewed = self.config.get('REVIEWED_DESTINATION')
        if reviewed:
            from .destinations import within
            if not within(page.url, reviewed['destination']):
                self.block('AUTH_REDIRECT_ORIGIN_OR_TENANT_MISMATCH')
        if origin(page.url) != origin(self.portal['origin']):
            self.block('AUTH_REDIRECT_ORIGIN_MISMATCH')
        forms = page.locator('form:visible')
        if forms.count() != 1:
            self.block('UNSUPPORTED_AUTH_FORM_LAYOUT')
        form = forms.first
        action = form.get_attribute('action') or page.url
        if reviewed and not within(urljoin(page.url, action), reviewed['destination']):
            self.block('CREDENTIAL_POST_TENANT_MISMATCH')
        if origin(urljoin(page.url, action)) != origin(self.portal['origin']):
            self.block('CROSS_ORIGIN_CREDENTIAL_POST_BLOCKED')
        return form

    def auth_form(self, page, password, registration):
        form = self.verified_auth_form(page)
        inputs = form.locator('input:visible,select:visible,textarea:visible')
        email_filled = password_filled = False
        for control in inputs.all():
            kind = control.get_attribute('type') or 'text'
            if kind in ('submit','button','hidden','reset'):
                continue
            label = control.evaluate("e=>e.getAttribute('aria-label')||[...(e.labels||[])].map(l=>l.textContent).join(' ')||e.name||''")
            autocomplete = control.get_attribute('autocomplete') or ''
            required = control.get_attribute('required') is not None or control.get_attribute('aria-required') == 'true'
            if kind == 'password':
                control.fill(password)
                if control.input_value() != password:
                    return False
                password_filled = True
            elif kind == 'email' or autocomplete in ('email','username') or re.fullmatch(r'(?:confirm )?email(?: address)?|username', label.strip(), re.I):
                control.fill(CONFIRMED_EMAIL)
                email_filled = True
            elif registration:
                answer = answer_field(label, self.profile)
                if not answer['requires_review'] and kind in ('text','tel'):
                    control.fill(str(answer['answer']))
                elif required or kind == 'checkbox' and control.is_checked():
                    self.block('UNSUPPORTED_REGISTRATION_FIELD_OR_LEGAL_CONSENT')
            elif required:
                self.block('UNSUPPORTED_LOGIN_FIELD')
        if not email_filled or not password_filled:
            self.block('AUTH_EMAIL_OR_PASSWORD_FIELD_NOT_VERIFIED')
        return form.evaluate('(f)=>f.checkValidity()')

    def compatible_password(self, page, preferred=None):
        self.verified_auth_form(page)
        controls = page.locator('input[type=password]:visible')
        if not 1 <= controls.count() <= 2:
            self.block('UNSUPPORTED_PASSWORD_CONTROLS')
        minimum = max(int(c.get_attribute('minlength') or 0) for c in controls.all())
        visible_policy = page.locator('body').inner_text()
        limits = re.findall(r'(?:at least|minimum(?: of)?|min(?:imum)? length[: ]*)\s*(\d+)\s*(?:characters|chars)?', visible_policy, re.I)
        if limits:
            minimum = max(minimum, max(map(int, limits)))
        maximum = min(int(c.get_attribute('maxlength') or 128) for c in controls.all())
        # Browser validity enforces the actual HTML pattern, without interpreting it as Python regex.
        symbols = next((c.get_attribute('data-allowed-symbols') for c in controls.all() if c.get_attribute('data-allowed-symbols')), None)
        if not symbols:
            allowed = re.search(r'allowed (?:special characters|symbols)\s*:\s*([^\nA-Za-z0-9]+)', visible_policy, re.I)
            symbols = allowed[1].strip() if allowed else '!@#$%_-'
        for attempt in range(21):
            try:
                candidate = preferred if attempt == 0 and preferred else random_password(minimum, maximum, symbols)
            except ValueError:
                self.block('PASSWORD_POLICY_NOT_SUPPORTED', 'ACCOUNT_CREATION_FAILED')
            for control in controls.all():
                control.fill(candidate)
            if all(c.input_value() == candidate and c.evaluate('(e)=>e.checkValidity()') for c in controls.all()):
                return candidate, 'LEGACY_PATTERN' if attempt == 0 and preferred else 'GENERATED'
        self.block('PASSWORD_POLICY_NOT_SUPPORTED', 'ACCOUNT_CREATION_FAILED')

    def click(self, page, registration):
        if self.attempts >= self.config.get('MAX_AUTH_ATTEMPTS', 2):
            self.block('AUTH_ATTEMPT_LIMIT_REACHED')
        regex = r'^(create account|register|sign up)$' if registration else r'^(sign in|log in|login)$'
        buttons = page.get_by_role('button', name=re.compile(regex, re.I))
        if buttons.count() != 1:
            self.block('AMBIGUOUS_AUTH_SUBMIT_CONTROL')
        # Persist before the one irreversible auth click. A crash must not recreate an account.
        self.update(auth_blocked=True, notes='Authentication action pending; inspect before retry after interruption')
        self.attempts += 1
        buttons.click(timeout=10000)
        deadline = time.monotonic() + self.config.get('AUTH_RESULT_TIMEOUT_MS', 10000) / 1000
        while time.monotonic() < deadline:
            if origin(page.url) != origin(self.portal['origin']):
                self.block('AUTH_REDIRECT_ORIGIN_MISMATCH')
            self.challenge(page)
            alerts = ' '.join(page.locator('[role=alert]:visible,[data-auth-error]:visible').all_inner_texts())
            if self.authenticated(page) or alerts.strip() or EXISTS.search(page.locator('body').inner_text()):
                return alerts
            page.wait_for_timeout(100)
        self.block('AUTH_OUTCOME_UNCONFIRMED')

    def save_credential(self, password, password_type, verified):
        reference = 'portal:' + self.account_id
        self.vault.put(reference, dict(password=password, email=CONFIRMED_EMAIL, verified=verified, updated_at=now()))
        self.update(credential_reference=reference, password_type=password_type,
                    verification_status='VERIFIED' if verified else 'PENDING')

    def login(self, page):
        if not self.config.get('ACCOUNT_LOGIN_ENABLED', True):
            self.block('ACCOUNT_LOGIN_DISABLED')
        prior = self.store.account(self.account_id)
        record = self.vault.get(prior.get('credential_reference') or 'portal:' + self.account_id)
        if record and (record.get('verified') or record.get('trusted_for_login') or prior['account_status'] in ('ACCOUNT_CREATED','EXISTING_ACCOUNT','LOGGED_IN')):
            password, password_type = record['password'], 'EXISTING_SAVED'
        else:
            if prior.get('legacy_login_attempted'):
                self.block('PASSWORD_RECOVERY_REQUIRED', 'PASSWORD_RECOVERY_REQUIRED')
            password, password_type = self.historical(), 'LEGACY_PATTERN'
        self.update(password_type=password_type)
        target = self.endpoint('login_url')
        if target and canonical_url(page.url) != target:
            page.goto(target, wait_until='domcontentloaded')
        self.challenge(page)
        if not self.auth_form(page, password, False):
            self.block('LOGIN_PASSWORD_OR_FORM_REJECTED', 'LOGIN_FAILED')
        if password_type == 'LEGACY_PATTERN':
            self.update(legacy_login_attempted=True)
        self.click(page, False)
        if not self.authenticated(page):
            self.block('LOGIN_REJECTED_NO_FURTHER_GUESSES', 'LOGIN_FAILED')
        self.save_credential(password, password_type, True)
        self.current_login = True
        self.update(account_status='EXISTING_ACCOUNT', last_login_at=now(), last_error='', auth_blocked=False, notes='Verified existing candidate login')

    def register(self, page):
        if not self.config.get('ACCOUNT_CREATION_ENABLED', True):
            self.block('ACCOUNT_CREATION_DISABLED')
        preferred = self.historical()
        password, password_type = self.compatible_password(page, preferred)
        if not self.auth_form(page, password, True):
            self.block('REGISTRATION_FORM_INVALID', 'ACCOUNT_CREATION_FAILED')
        if self.config.get('ACCOUNT_CREATION_DRY_RUN', True) or self.config.get('DRY_RUN', True):
            # Never leave credentials in a saved browser draft during dry run.
            page.locator('input[type=password]').evaluate_all("nodes=>nodes.forEach(n=>n.value='')")
            self.block('ACCOUNT_CREATION_DRY_RUN_NO_ACCOUNT_CREATED')
        for attempt in range(2):
            # Stage only encrypted bytes before POST to retain credentials if browser crashes.
            self.save_credential(password, password_type, False)
            alerts = self.click(page, True)
            if EXISTS.search(page.locator('body').inner_text()):
                self.update(auth_blocked=False, notes='Existing account detected; attempt one authorized login')
                self.login(page)
                return
            if self.authenticated(page):
                self.save_credential(password, password_type, True)
                self.current_created = self.current_login = True
                self.update(account_status='ACCOUNT_CREATED', created_at=now(), last_login_at=now(), last_error='', auth_blocked=False, notes='Registration and authenticated session verified')
                return
            if attempt == 0 and PASSWORD_REJECTED.search(alerts):
                password, password_type = self.compatible_password(page)
                if not self.auth_form(page, password, True):
                    self.block('PASSWORD_FALLBACK_REJECTED', 'ACCOUNT_CREATION_FAILED')
                continue
            self.block('ACCOUNT_REGISTRATION_REJECTED', 'ACCOUNT_CREATION_FAILED')
        self.block('ACCOUNT_REGISTRATION_ATTEMPT_LIMIT', 'ACCOUNT_CREATION_FAILED')

    def ensure_authenticated(self, page):
        # Defense in depth: caller cannot use this entry point to bypass policy.
        policy = CompanyPolicy.load(self.root).evaluate(self.job['company'], self.job['id'])
        if policy['company_policy'] != 'AUTO_APPLY':
            self.block(policy['review_reason'])
        passwords = page.locator('input[type=password]:visible')
        entry = page.get_by_role('link', name=re.compile(r'^(sign in|log in|create account|register)$', re.I))
        reviewed = self.config.get('REVIEWED_DESTINATION')
        if reviewed and not passwords.count() and page.locator('[data-applicant-email],[data-email]').count():
            self.choose_portal(page)
            if not self.authenticated(page):
                self.block('SESSION_APPLICANT_EMAIL_NOT_VERIFIED')
            self.current_login = True
            self.update(account_status='LOGGED_IN', last_login_at=now(), last_error='', auth_blocked=False, notes='Reviewed destination session email verified')
            return
        applicable = [p for p in self.config.get('ACCOUNT_PORTALS', []) if company_key(p.get('company','')) == company_key(policy['canonical_company']) and origin(p.get('origin','')) == origin(page.url) and urlsplit(application_url(self.job)).path.startswith(p.get('application_path_prefix','/'))]
        if len(applicable) == 1 and not passwords.count() and page.locator(applicable[0]['authenticated_selector']).count():
            self.choose_portal(page)
            if not self.authenticated(page):
                self.block('SESSION_APPLICANT_EMAIL_NOT_VERIFIED')
            self.current_login = True
            self.update(account_status='LOGGED_IN', last_login_at=now(), last_error='', auth_blocked=False, notes='Existing authenticated browser session reused')
            return
        direct_form = page.locator('input[type=file]:visible').count() > 0
        challenge_present = page.locator('input[autocomplete=one-time-code]:visible,.g-recaptcha,[data-sitekey]').count() or VERIFY.search(page.locator('body').inner_text())
        otp_auth=bool(self.config.get('APPLICATION_JOURNEY') and page.get_by_role('button',name=re.compile(r'continue with (?:phone|email)',re.I)).count())
        dialog_auth=bool(self.config.get('APPLICATION_JOURNEY') and page.get_by_role('dialog').count() and page.get_by_role('button',name=re.compile(r'^(sign in|create account|log in)$',re.I)).count())
        if not passwords.count() and (not entry.count() or direct_form) and not challenge_present and not dialog_auth and not otp_auth:
            self.store.update(self.job['id'], login_required=False, login_successful=False, account_created=False, verification_required=False)
            return
        if self.config.get('APPLICATION_JOURNEY'):
            self.config['APPLICATION_JOURNEY'].step='Login or account creation required'
            self.config['APPLICATION_JOURNEY'].log('AUTH','Observed login/account controls; checking employer-specific credential scope',page.url)
        self.choose_portal(page)
        self.challenge(page)
        if self.config.get('APPLICATION_JOURNEY') and page.get_by_role('button',name=re.compile(r'continue with (?:phone|email)',re.I)).count() and re.search(r'one.time (?:text )?code',page.locator('body').inner_text(),re.I):
            self.block('EMAIL_OR_SMS_ONE_TIME_CODE_REQUIRED','VERIFICATION_REQUIRED')
        if self.config.get('DRY_RUN', True):
            self.block('AUTH_DRY_RUN_NO_LOGIN_OR_REGISTRATION')
        prior = self.store.account(self.account_id)
        if prior.get('auth_blocked') or prior['account_status'] in ('LOGIN_FAILED','PASSWORD_RECOVERY_REQUIRED','VERIFICATION_REQUIRED'):
            self.block('PRIOR_AUTH_OUTCOME_REQUIRES_MANUAL_REVIEW', prior['account_status'] if prior['account_status'] in ACCOUNT_STATUSES else 'NEEDS_REVIEW')
        self.initialize_vault()
        try:
            if not passwords.count():
                if entry.count() != 1:
                    self.block('AMBIGUOUS_AUTH_ENTRY_LINK')
                target = urljoin(page.url, entry.get_attribute('href') or '')
                if reviewed:
                    from .destinations import within
                    if not within(target, reviewed['destination']):
                        self.block('AUTH_ENDPOINT_TENANT_MISMATCH')
                if origin(target) != origin(self.portal['origin']):
                    self.block('AUTH_ENTRY_ORIGIN_MISMATCH')
                page.goto(target, wait_until='domcontentloaded')
            self.challenge(page)
            saved = self.vault.get(prior.get('credential_reference') or 'portal:' + self.account_id)
            registration = page.locator('input[type=password]:visible').count() == 2 or page.get_by_role('button',name=re.compile(r'^(create account|register|sign up)$',re.I)).count() == 1
            if saved and (saved.get('verified') or saved.get('trusted_for_login')) or prior['account_status'] in ('ACCOUNT_CREATED','EXISTING_ACCOUNT','LOGGED_IN') or EXISTS.search(page.locator('body').inner_text()) or not registration:
                self.login(page)
            else:
                self.register(page)
            page.goto(application_url(self.job), wait_until='domcontentloaded')
            self.challenge(page)
            if page.locator('input[type=password]:visible').count():
                self.block('AUTH_SESSION_DID_NOT_OPEN_INTENDED_APPLICATION')
        except Blocked:
            raise
        except (VaultError, OSError):
            self.block('CREDENTIAL_VAULT_OR_AUTH_IO_ERROR')
        except Exception:
            self.block('AUTH_AUTOMATION_ERROR')

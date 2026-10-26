"""Shared employer-only policy. No description substring or parent-company inference."""
import json
import re
import unicodedata
from pathlib import Path
import yaml

DEFAULT_FILE = Path(__file__).resolve().parent.parent / 'config/company_policies.yaml'


def company_key(value):
    return ' '.join(re.findall(r'[^\W_]+', unicodedata.normalize('NFKC', value).casefold()))


class CompanyPolicy:
    def __init__(self, config):
        if config.get('default_application_policy', 'AUTO_APPLY') != 'AUTO_APPLY':
            raise ValueError('Only AUTO_APPLY is supported as the default company policy')
        self.config = config
        self.excluded = {company_key(c) for c in config['excluded_companies']}
        self.referral = {company_key(c) for c in config['referral_required_companies']}
        self.aliases = {}
        for alias, rule in config.get('company_aliases', {}).items():
            rule = dict(company=rule, requires_verification=False) if isinstance(rule, str) else rule
            key = company_key(alias)
            if key in self.aliases and self.aliases[key] != rule:
                raise ValueError('Conflicting normalized employer aliases')
            self.aliases[key] = rule

    @classmethod
    def load(cls, root=None):
        path = Path(root) / 'config/company_policies.yaml' if root else DEFAULT_FILE
        return cls(yaml.safe_load((path if path.exists() else DEFAULT_FILE).read_text()))

    def evaluate(self, company, job_id='', identity_verified=False):
        key = company_key(company)
        canonical = company
        if key in self.excluded:
            return dict(company_policy='EXCLUDED', application_method='BLOCKED', review_reason='USER_EXCLUDED_COMPANY', canonical_company=canonical)
        rule = self.aliases.get(key)
        if rule:
            canonical = rule['company']
            verified = self.config.get('matching_rules', {}).get('verified_employer_jobs', {}).get(job_id)
            if rule.get('requires_verification') and not (identity_verified or company_key(str(verified or '')) == company_key(canonical)):
                return dict(company_policy='IDENTITY_REVIEW', application_method='BLOCKED', review_reason='EMPLOYER_IDENTITY_UNVERIFIED', canonical_company=canonical)
            key = company_key(canonical)
        # Exclusion wins even if both lists accidentally contain this employer.
        if key in self.excluded:
            return dict(company_policy='EXCLUDED', application_method='BLOCKED', review_reason='USER_EXCLUDED_COMPANY', canonical_company=canonical)
        if key in self.referral:
            return dict(company_policy='REFERRAL_REQUIRED', application_method='REFERRAL', review_reason='REFERRAL_PENDING', canonical_company=canonical)
        return dict(company_policy='AUTO_APPLY', application_method='AUTOMATIC', review_reason='', canonical_company=canonical)


def classify_scraped_jobs(root, rows):
    """Sidecar preserves the original scraper CSV/JD interfaces and every job."""
    from .applications.storage import save_json, now
    engine = CompanyPolicy.load(root)
    save_json(Path(root) / 'data/company_policy_classifications.json', {
        row['id']: dict(engine.evaluate(row['company'], row['id']), policy_evaluated_at=now()) for row in rows})

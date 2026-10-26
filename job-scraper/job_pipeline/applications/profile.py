"""Explicit factual mappings. Ambiguous legal wording never goes to a model."""
import json
import re
from datetime import date
from .integration import normalize
from .storage import digest
from .identity import CONFIRMED_EMAIL


def load_profile(path):
    profile = json.loads(path.read_text())
    if CONFIRMED_EMAIL:
        profile.setdefault('facts', {})['email'] = CONFIRMED_EMAIL
    profile['version'] = digest(json.dumps(profile, sort_keys=True).encode())
    return profile


def answer_field(label, profile, eeo=False):
    q = normalize(label)
    facts = profile.get('facts', {})
    result = dict(answer=None, question_category='unknown', evidence_sources=[], confidence=0.0,
                  requires_review=True, reason='No verified mapping for this exact question')

    def value(answer, category, source):
        return dict(answer=answer, question_category=category, evidence_sources=[source], confidence=1.0,
                    requires_review=answer is None, reason='' if answer is not None else 'Missing verified fact')

    # Legal questions are deliberately narrow; no substring yes/no heuristics.
    legal = {
        'are you legally authorized to work in the united states': 'authorized_us',
        'are you authorized to work in the us': 'authorized_us',
        'are you legally eligible to work in the united states': 'authorized_us',
        'will you now or in the future require sponsorship for employment visa status': 'future_sponsorship',
        'will you require employer sponsorship in the future': 'future_sponsorship',
        'are you a u s citizen': 'us_citizen', 'are you a us citizen': 'us_citizen',
        'are you a permanent resident': 'permanent_resident',
        'do you currently hold an h 1b': 'current_h1b',
        'what is your current immigration status': 'immigration_status',
        'when does your current work authorization expire': 'authorization_end',
    }
    # Strip required marker, but not materially different wording.
    if q in legal:
        if date.today() > date.fromisoformat(facts.get('authorization_end') or '1900-01-01'):
            return dict(result, question_category='immigration', reason='Work authorization facts expired; update profile')
        return value(facts.get(legal[q]), 'immigration', 'profile.facts.' + legal[q])
    sensitive = r'sponsor|visa|immigra|citizen|permanent resident|work authoriz|legally|restriction|indefinitely|stem opt|h 1b|security clearance|background check|arbitration|signature|certif|attest|agree|consent|terms|acknowledge|authorize|passport|social security|ssn|date of birth'
    if re.search(sensitive, q):
        return dict(result, question_category='legal', reason='Material legal/authorization wording requires explicit human review')
    demographic = {'gender': 'gender', 'sex assigned at birth': 'sex_at_birth', 'race': 'race',
                   'ethnicity': 'ethnicity', 'are you hispanic or latino': 'hispanic',
                   'current disability status': 'disability'}
    if q in demographic:
        return value(profile.get('demographics', {}).get(demographic[q]) if eeo else 'I do not wish to answer', 'eeo', 'profile.demographics' if eeo else 'preference.eeo_disabled')
    if re.search(r'gender|disab|veteran|ethnic|racial|hispanic|latino|\brace\b', q):
        return dict(result, question_category='eeo', reason='Demographic question differs from supplied facts')
    latest_employment = {'most recent employer': 'employer', 'most recent job title': 'title', 'most recent employment start month': 'start_month', 'most recent employment end month': 'end_month'}
    if q in latest_employment:
        history = profile.get('employment', [])
        return value(history[0].get(latest_employment[q]) if history else None, 'experience', 'profile.employment[0].' + latest_employment[q])
    aliases = {
        'full name': 'full_name', 'full legal name': 'full_name', 'name': 'full_name',
        'first name': 'first_name', 'last name': 'last_name', 'preferred name': 'preferred_name',
        'email': 'email', 'email address': 'email', 'phone': 'phone', 'phone number': 'phone',
        'linkedin': 'linkedin', 'linkedin profile': 'linkedin', 'github': 'github',
        'portfolio': 'portfolio', 'website': 'portfolio', 'address': 'address', 'street address': 'address',
        'city': 'city', 'state': 'state', 'zip code': 'zip', 'postal code': 'zip', 'country': 'country',
        'university': 'university', 'school': 'university', 'degree': 'degree', 'field of study': 'field',
        'major': 'field', 'graduation date': 'graduation_month', 'gpa': 'gpa',
        'technical skills': 'technical_skills',
        'salary expectations': 'salary_expectations', 'earliest start date': 'start_date',
    }
    if q in aliases:
        key = aliases[q]
        category = ('education' if key in ('university', 'degree', 'field', 'graduation_month', 'gpa') else 'skills' if key == 'technical_skills' else 'contact' if key in ('email','phone','linkedin','github','portfolio','address','city','state','zip','country') else 'identity')
        return value((CONFIRMED_EMAIL or facts.get(key)) if key == 'email' else facts.get(key), category, 'confirmed applicant email' if key == 'email' else 'profile.facts.' + key)
    # A manually resolved answer is scoped by EXACT question, job, profile version elsewhere.
    return result

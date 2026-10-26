"""Scoped verified history answers and accessible custom controls."""
import re
from .integration import normalize
from .profile import answer_field


def scoped_answer(field, profile, eeo=False):
    section=normalize(field.get('section',''));label=normalize(field['label'])
    kind='education' if re.search(r'education|school|university',section) else 'employment' if re.search(r'employment|work experience|work history|professional experience',section) else None
    if not kind:return answer_field(field['label'],profile,eeo)
    index=field.get('section_index',0);history=profile.get(kind,[])
    mapping=({'school':'university','school or university':'university','university':'university','degree':'degree','field of study':'field','major':'field','gpa':'gpa','from':'start_month','to':'end_month','start date':'start_month','end date':'end_month','country':'country'} if kind=='education' else {'company':'employer','company name':'employer','employer':'employer','job title':'title','title':'title','from':'start_month','to':'end_month','start date':'start_month','end date':'end_month'})
    if label not in mapping:return answer_field(field['label'],profile,eeo)
    value=history[index].get(mapping[label]) if index<len(history) else None
    return dict(answer=value,question_category=kind,evidence_sources=[f'profile.{kind}[{index}].{mapping[label]}'],confidence=1.0 if value is not None else 0,requires_review=value is None,reason='Missing verified history field' if value is None else '')


def fill_combobox(page,control,text):
    if control.evaluate('(e)=>e.tagName.toLowerCase()') in ('input','textarea'):
        control.fill(text)
    else:
        control.click()
    page.wait_for_timeout(250)
    options=page.get_by_role('option')
    exact=[o for o in options.all() if o.is_visible() and normalize(o.inner_text())==normalize(text)]
    if len(exact)!=1:raise ValueError('Autocomplete has no unique exact verified option')
    exact[0].click()
    current=control.evaluate("e=>e.value||e.textContent||e.getAttribute('aria-valuetext')||''")
    return normalize(current)==normalize(text)

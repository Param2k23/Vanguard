"""Filter structured Jobright metadata, never job-description prose."""
import re


STATE_CODES = set('AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY'.split())
STATE_NAMES = (
    'Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|'
    'District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|'
    'Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|'
    'Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|'
    'New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|'
    'Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|'
    'Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming'
)


def us_location(location):
    """Accept US countries or US state suffixes, not countryless foreign cities."""
    if re.search(r'\bUnited States(?: of America)?\b|\bUSA\b|\bU\.?S\.?(?:A\.?)?(?=\W|$)', location, re.I):
        return True
    # Require the state to end the geographic label, so London, ON, Canada
    # and Vancouver, BC, Canada cannot be mistaken for US state addresses.
    geographic = re.sub(r'\s*\((?:remote|hybrid|onsite|on-site)\)\s*$', '', location, flags=re.I).strip()
    state = re.search(r',\s*([A-Za-z]{2})(?:\s+\d{5}(?:-\d{4})?)?$', geographic)
    if state and state.group(1).upper() in STATE_CODES:
        return True
    return bool(re.search(r'(?:^|,\s*)(?:' + STATE_NAMES + r')$', geographic, re.I))


def skip_reason(badges, company_types, filters, locations=(), companies=()):
    if filters.get('skip_reposted', True) and any(
            re.search(r'\bre[\s-]?posted\b', label, re.I) for label in badges):
        return 'reposted'
    if filters.get('skip_consulting', True) and any(
            re.search(r'\bconsulting\b', label, re.I) for label in company_types):
        return 'consulting company'
    if filters.get('skip_educational', True) and any(
            re.search(r'\b(?:schools?|institutes?|institutions?|universit(?:y|ies)|colleges?|academ(?:y|ies)|higher education|education management|educational)\b', label, re.I)
            for label in [*company_types, *companies]):
        return 'educational institution'
    if filters.get('us_only', True):
        concrete = [label.strip() for label in locations if label.strip() and not re.fullmatch(
            r'(?:remote|hybrid|onsite|on-site|unknown|n/?a|not specified)', label.strip(), re.I)]
        if concrete and not any(us_location(label) for label in concrete):
            return 'outside US'
    return ''

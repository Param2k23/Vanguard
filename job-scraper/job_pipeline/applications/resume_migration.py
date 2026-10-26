"""Release only obsolete approval reviews; never discover or submit applications."""
import json

from ..company_policy import CompanyPolicy
from .integration import APPROVAL_REASON, jobs, resume_for, ReadinessError
from .storage import now


def migrate_approval_reviews(root, agent, profile, config, store, job_ids=None):
    result = dict(resolved=[], released=[], retained=[], historical_archived=[])
    if config.get('RESUME_APPROVAL_MODE', 'automatic') != 'automatic':
        return result
    from .sessions import keys
    path = root / 'data/applications/historical_baseline.json'
    baseline = json.loads(path.read_text())['records'] if path.exists() else []
    indexed = {j['id']: j for j in jobs(root)}
    policy = CompanyPolicy.load(root)
    # Snapshot all unresolved reviews so unrelated blockers remain visible.
    pending = store.reviews()
    for review in pending:
        identifier = review['job_id']
        payload = json.loads(review['payload'])
        if payload.get('reason') != APPROVAL_REASON or (job_ids is not None and identifier not in job_ids):
            continue
        job = indexed.get(identifier)
        if job is None:
            result['retained'].append(dict(job_id=identifier, reason='Stored job unavailable'))
            continue
        try:
            artifact = resume_for(root, agent, job, approval_mode='automatic')
        except (ReadinessError, OSError, ValueError, KeyError) as error:
            result['retained'].append(dict(job_id=identifier, reason=str(error) if isinstance(error, ReadinessError) else type(error).__name__))
            continue
        row = store.get(identifier)
        historical = any(keys(job).intersection(set(e['keys']) | keys(e['job'])) for e in baseline)
        other = [r for r in pending if r['job_id'] == identifier and json.loads(r['payload']).get('reason') != APPROVAL_REASON]
        allowed = policy.evaluate(job['company'], identifier)['company_policy'] == 'AUTO_APPLY'
        # Do not clear mixed text, authentication/errors, history, referrals, exclusions,
        # or statuses representing another application blocker.
        if row and row['status'] == 'NEEDS_REVIEW' and row['review_reason'] == APPROVAL_REASON and not row.get('error_message') and not row.get('authentication_error') and not historical and allowed:
            changes = dict(resume_path=artifact['path'], resume_version=artifact['sha256'],
                           resume_status='READY_FOR_APPLICATION', resume_acceptance_mode='automatic')
            if other:
                changes['review_reason'] = json.loads(other[0]['payload']).get('reason', 'Other review item remains')
            else:
                changes.update(status='READY_TO_APPLY', review_reason='')
                result['released'].append(identifier)
            store.update(identifier, **changes)
        payload.update(resolved_at=now(), resolution='Obsolete manual resume approval archived after technical validation',
                       accepted_resume_path=artifact['path'], accepted_resume_sha256=artifact['sha256'],
                       acceptance_mode='automatic', human_verified=False)
        with store.db:
            store.db.execute('UPDATE reviews SET resolved=1,payload=? WHERE id=?', (json.dumps(payload), review['id']))
            store.db.execute('INSERT INTO events(job_id,at,payload) VALUES (?,?,?)',
                             (identifier, now(), json.dumps(dict(event='RESUME_APPROVAL_REVIEW_ARCHIVED', review_id=review['id'], historical_exclusion_preserved=historical))))
        result['resolved'].append(identifier)
        if historical:
            result['historical_archived'].append(identifier)
    return result

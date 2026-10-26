"""Automatic acceptance is artifact validation, never a claim of human approval."""
import json
import tempfile
import unittest
from pathlib import Path

from apply_jobs import configuration
from job_pipeline.applications.integration import APPROVAL_REASON, ReadinessError, resume_for
from job_pipeline.applications.resume_migration import migrate_approval_reviews
from job_pipeline.applications.sessions import keys
from job_pipeline.applications.storage import Store, digest, save_json
from tests.test_applications import setup_data


class AutomaticResumeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.agent, self.job, self.pdf, self.profile = setup_data(self.root)
        self.profile['resume_approvals'] = {}
        self.store = Store(self.root / 'data/applications')
        self.addCleanup(self.store.close)
        self.config = {'RESUME_APPROVAL_MODE':'automatic'}

    def old_review(self):
        self.store.update('one',status='NEEDS_REVIEW',review_reason=APPROVAL_REASON)
        self.store.review('one','Resume/readiness',APPROVAL_REASON)

    def migrate(self):
        return migrate_approval_reviews(self.root,self.agent,self.profile,self.config,self.store)

    def test_automatic_valid_without_approval_and_audit(self):
        result=resume_for(self.root,self.agent,self.job)
        self.assertEqual(result['path'],str(self.pdf))
        audit=json.loads((self.root/'data/resumes/acceptance.json').read_text())
        row=next(iter(audit.values()))
        self.assertEqual(row['status'],'READY_FOR_APPLICATION')
        self.assertEqual(row['mode'],'automatic')
        self.assertIs(row['human_verified'],False)
        self.assertEqual(row['sha256'],digest(self.pdf.read_bytes()))
        with self.assertRaisesRegex(ReadinessError,'factual approval'):
            resume_for(self.root,self.agent,self.job,approval_mode='manual')

    def test_corrupt_pdf_even_matching_recorded_hash_rejected(self):
        self.pdf.write_bytes(b'%PDF-1.4\ncorrupt but header looks plausible')
        verifier=self.agent/'jobs/example/.verified.json'
        data=json.loads(verifier.read_text());data['pdf_sha256']=digest(self.pdf.read_bytes());save_json(verifier,data)
        with self.assertRaisesRegex(ReadinessError,'corrupt'):
            resume_for(self.root,self.agent,self.job)

    def test_missing_and_mismatched_files_rejected(self):
        wrong=dict(self.job,company='Another Company')
        with self.assertRaisesRegex(ReadinessError,'mismatch'):
            resume_for(self.root,self.agent,wrong)
        self.pdf.unlink()
        with self.assertRaises(ReadinessError):
            resume_for(self.root,self.agent,self.job)

    def test_release_approval_only_idempotently(self):
        self.old_review()
        before=self.pdf.read_bytes()
        result=self.migrate()
        self.assertEqual(result['released'],['one'])
        self.assertEqual(self.store.get('one')['status'],'READY_TO_APPLY')
        self.assertEqual(self.store.get('one')['resume_status'],'READY_FOR_APPLICATION')
        self.assertEqual(self.store.reviews(),[])
        archived=self.store.db.execute('SELECT payload FROM reviews').fetchone()[0]
        self.assertEqual(json.loads(archived)['reason'],APPROVAL_REASON)
        self.assertEqual(self.pdf.read_bytes(),before)
        self.assertEqual(self.migrate()['released'],[])

    def test_unrelated_review_and_mixed_reason_preserved(self):
        self.old_review()
        self.store.review('one','Legal question','Uncertain legal answer')
        self.migrate()
        self.assertEqual(self.store.get('one')['status'],'NEEDS_REVIEW')
        self.assertEqual(self.store.get('one')['review_reason'],'Uncertain legal answer')
        self.assertEqual(len(self.store.reviews()),1)
        self.store.update('one',review_reason=APPROVAL_REASON+'; other blocker')
        self.store.review('one','Resume/readiness',APPROVAL_REASON+'; other blocker')
        self.assertEqual(self.migrate()['resolved'],[])

    def test_historical_and_verified_never_reactivated(self):
        self.old_review()
        save_json(self.root/'data/applications/historical_baseline.json',dict(records=[dict(job=self.job,keys=sorted(keys(self.job)))]))
        result=self.migrate()
        self.assertEqual(result['released'],[])
        self.assertEqual(result['historical_archived'],['one'])
        self.assertEqual(self.store.get('one')['status'],'NEEDS_REVIEW')
        self.store.update('one',status='APPLIED',confirmation_evidence='Local test evidence')
        self.store.review('one','Resume/readiness',APPROVAL_REASON)
        before=self.store.get('one')
        self.migrate()
        self.assertEqual(self.store.get('one'),before)

    def test_failed_validation_does_not_release(self):
        self.old_review(); self.pdf.unlink()
        self.assertEqual(self.migrate()['released'],[])
        self.assertEqual(len(self.store.reviews()),1)
        self.assertEqual(self.store.get('one')['status'],'NEEDS_REVIEW')

    def test_production_defaults_unchanged_and_manual_optional(self):
        config=configuration(self.root/'missing.json')
        self.assertEqual(config['RESUME_APPROVAL_MODE'],'automatic')
        self.assertIs(config['DRY_RUN'],True)
        self.assertIs(config['AUTO_SUBMIT_ENABLED'],False)
        self.assertIs(config['ACCOUNT_CREATION_DRY_RUN'],True)
        self.old_review();self.config['RESUME_APPROVAL_MODE']='manual'
        self.assertEqual(self.migrate()['resolved'],[])

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

import apply_jobs
from job_pipeline.applications.storage import Store, save_json
from job_pipeline.applications.vault import CredentialVault
from test_applications import ROOT, setup_data
from test_account_policy import MASTER


class AccountCliTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)
        self.agent,self.job,self.pdf,self.profile=setup_data(self.root)
        (self.root/'application_config.json').write_text((ROOT/'application_config.json').read_text())
    def tearDown(self):self.temp.cleanup()
    def test_boolean_validation_rejects_string_false(self):
        save_json(self.root/'local.json',{'ACCOUNT_CREATION_DRY_RUN':'false'})
        with patch.object(apply_jobs,'ROOT',self.root):
            with self.assertRaises(ValueError):apply_jobs.configuration(self.root/'local.json')
    def test_production_requires_config_and_per_run_opt_ins(self):
        save_json(self.root/'application_config.local.json',dict(AUTO_SUBMIT_ENABLED=True,DRY_RUN=False,ACCOUNT_CREATION_DRY_RUN=False))
        captured=[]
        def run(root,agent,profile,config,**kwargs):
            captured.append(config);return dict(counts={})
        with patch.object(apply_jobs,'ROOT',self.root),patch.object(apply_jobs,'process',side_effect=run):
            self.assertEqual(apply_jobs.main(['apply','--profile',str(self.root/'profile.json')]),0)
            self.assertTrue(captured[-1]['ACCOUNT_CREATION_DRY_RUN']);self.assertFalse(captured[-1]['AUTO_SUBMIT_ENABLED'])
            self.assertEqual(apply_jobs.main(['apply','--profile',str(self.root/'profile.json'),'--enable-auto-submit','--enable-account-creation']),0)
            self.assertFalse(captured[-1]['ACCOUNT_CREATION_DRY_RUN']);self.assertTrue(captured[-1]['AUTO_SUBMIT_ENABLED'])
    def test_password_retrieval_refuses_redirected_output(self):
        vault=CredentialVault(self.root/'data/credentials/vault.json',MASTER);vault.initialize()
        password='Synthetic-Private-Password-89!'
        vault.put('portal:example',dict(password=password,verified=True))
        store=Store(self.root/'data/applications');store.update_account('example',credential_reference='portal:example',account_status='EXISTING_ACCOUNT');store.close()
        out,err=io.StringIO(),io.StringIO()
        with patch.object(apply_jobs,'ROOT',self.root),patch.dict(os.environ,JOB_PORTAL_VAULT_PASSPHRASE=MASTER),redirect_stdout(out),redirect_stderr(err):
            with self.assertRaises(SystemExit):apply_jobs.main(['vault-get','--account-id','example'])
        self.assertNotIn(password,out.getvalue()+err.getvalue())
        self.assertIn('private interactive terminal',err.getvalue())
    def test_referral_update_retains_policy_block(self):
        store=Store(self.root/'data/applications')
        store.update('one',status='NEEDS_REVIEW',company_policy='REFERRAL_REQUIRED',application_method='REFERRAL',review_reason='REFERRAL_PENDING')
        store.close()
        save_json(self.root/'referral.json',dict(referral_contact='User supplied fixture contact',referral_notes='User supplied fixture note'))
        with patch.object(apply_jobs,'ROOT',self.root),redirect_stdout(io.StringIO()):
            self.assertEqual(apply_jobs.main(['referral-update','--job-id','one','--referral-status','REFERRAL_RECEIVED','--referral-file',str(self.root/'referral.json')]),0)
        store=Store(self.root/'data/applications');row=store.get('one');store.close()
        self.assertEqual(row['status'],'NEEDS_REVIEW');self.assertEqual(row['review_reason'],'REFERRAL_PENDING')
        self.assertEqual(row['referral_status'],'REFERRAL_RECEIVED')

# Auto-applier development

The auto-applier is experimental. It is preserved separately from the working daily
scrape/resume/export pipeline. `python vanguard.py` never submits applications.

## Entry points

From `job-scraper/` with the environment active:

```bash
python apply_jobs.py --help
python apply_jobs.py inspect --limit 1
python apply_jobs.py dry-run --limit 1
python apply_jobs.py report
```

First copy `applicant_profile.example.json` to `applicant_profile.json` and fill in
your own verified facts. Set `VANGUARD_EMAIL` to that same email when running the
application CLI directly. Copy `application_config.json` to
`application_config.local.json` for local overrides. The checked-in defaults keep
`DRY_RUN=true`, `AUTO_SUBMIT_ENABLED=false`, and account creation dry-run enabled.
`AI_ANSWERS_ENABLED` is disabled by default. Do not use someone else's profile.
The optional encrypted credential vault is initialized explicitly through the CLI.

The legacy application implementation shares a process-level contact identity;
changing applicant requires a fresh process. `VANGUARD_EMAIL` overrides the profile
email; keep them consistent. Multi-applicant execution in one process is not supported.

## Where to contribute

| Area | Source |
| --- | --- |
| CLI commands and configuration | `apply_jobs.py` |
| Bounded full-session orchestration | `job_pipeline/applications/runner.py` |
| Browser navigation and form handling | `job_pipeline/applications/browser.py` |
| Application journey and origin checks | `job_pipeline/applications/journey.py` |
| Account creation/login | `job_pipeline/applications/accounts.py` |
| Verified personal facts and field mapping | `job_pipeline/applications/profile.py` |
| Generated answers and evidence | `job_pipeline/applications/answers.py` |
| State, reviews and deduplication | `job_pipeline/applications/storage.py` |
| Encrypted account credentials | `job_pipeline/applications/vault.py` |
| Resume preparation/review integration | `job_pipeline/applications/runner.py` and `resume_migration.py` |
| Employer exclusions/referral policy | `config/company_policies.yaml` |

Inspect the actual module list when adding a feature; the system also includes
review batches, resume migration, submissions, and integration helpers.

## Development priorities

- Improve adapters against sanitized fixtures for supported ATS forms and multi-step pages.
- Expand coverage for navigation, file uploads, interrupted sessions and duplicate submissions.
- Keep uncertain legal/eligibility fields in review and require evidence before marking APPLIED.
- Improve recovery/reporting for unsupported layouts, expired sessions and account challenges.
- Keep authenticated browser state, screenshots, profiles and credentials out of commits.

Preparation, approved-batch application and reconciliation are distinct CLI modes.
Read the mode implementation before changing submission behavior. Login challenges,
captchas and verification codes require manual action. A successful click alone is
not submission evidence. Existing test fixtures use local fabricated HTML and mock
servers; they do not establish reliability on real employer sites.

## Test locally

From the repository root:

```bash
VANGUARD_EMAIL=applicant@example.test python -m unittest discover -s tests -v
(cd job-scraper && VANGUARD_EMAIL=applicant@example.test python -m unittest discover -s tests -v)
python -m unittest discover -s job-agent/tests -v
# Include local Playwright fixtures after installing Chromium:
(cd job-scraper && RUN_BROWSER_TESTS=1 VANGUARD_EMAIL=applicant@example.test python -m unittest discover -s tests -v)
```

These commands do not submit real applications or send email. The browser suite
needs a usable browser sandbox and localhost socket support.

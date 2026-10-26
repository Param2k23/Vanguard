# Setup

Vanguard currently targets Linux and WSL2. Use Python 3.11 or newer. Resume generation
uses your installed Codex CLI and sign-in, with no model API key configured by this
project. You also need LaTeX (`pdflatex`) and Poppler (`pdfinfo`, `pdftotext`).

## 1. Install dependencies

```bash
git clone https://github.com/Param2k23/Vanguard.git
cd Vanguard
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
# Ubuntu / WSL2 system dependencies:
sudo apt-get update
sudo apt-get install texlive-latex-extra texlive-fonts-recommended poppler-utils
sudo .venv/bin/python -m playwright install-deps chromium
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" python -m playwright install chromium
```

The runner automatically uses `.playwright-browsers/` when present. For direct
component commands also set `export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"`
from the repository root. Install and authenticate Codex using the
[official CLI instructions](https://developers.openai.com/codex/cli/).
Check `codex --version` and `codex login status`. The resume batch consumes your
configured Codex account's usage allowance. The workflow invokes
[`codex exec`](https://developers.openai.com/codex/noninteractive/).

## 2. Configure your pipeline

```bash
cp config/pipeline.example.yaml config/pipeline.local.yaml
```

Edit `config/pipeline.local.yaml`:

```yaml
scraper_dir: job-scraper
agent_dir: job-agent
export_dir: exports
run_dir: var/runs
limit: 15
email: YOUR_REAL_EMAIL
resume_prefix: Applicant
github_user: YOUR_GITHUB_USERNAME
```

Paths resolve from the Vanguard root, regardless of your current terminal directory.
`export_dir` can be a local directory or `/mnt/d/Resume` on WSL. WSL drive destinations
are checked for a mounted writable drive before scraping. You can also explicitly
set `export_mount: /mnt/d`. Export creates a dated subdirectory automatically.
Optional `model` selects a Codex model; omit it to use your configured default.
`browser_path` overrides the browser installation directory.

Edit `job-scraper/config.yaml` to set your logged-in Jobright feed URL and filters.
Also review `job-scraper/config/company_policies.yaml`: the supplied policy includes
employer exclusions and referral preferences inherited from the original workflow.
The supplied selectors depend on Jobright's page structure and may need adjustment.

## 3. Add your resume bases

Create these private files using your real LaTeX resumes:

```text
job-agent/base/Applicant_Data.tex
job-agent/base/Applicant_Fullstack.tex
```

If you set a different `resume_prefix`, replace `Applicant` in both filenames.
The `.example.tex` files demonstrate the required macros and section structure;
they contain fictional facts and must be fully rewritten before real use. Both
bases should compile to one page. Preserve the `\section`, `\resumeProjectHeading`,
`\resumeItem` and `Technical Skills` structure used by the verifier. Project link
labels must be `\underline{YOUR_USERNAME/project-slug}`. The verifier expects
section headings to appear as standalone rendered lines.

Read `job-agent/base/system_prompt.md` to understand tailoring constraints. The
current generation workflow retains the original Strategy B assumed-completion
project drafting: generated projects and metrics may be hypothetical and are recorded
in each job's report. Review these assumptions before using a resume. Layout verification
does not establish factual accuracy or an ATS score.

Optional: copy `voice.example.md` to `voice.md` and describe your tone for outreach
and application answers. The daily pipeline does not send outreach.

## 4. Log in once and run

```bash
python vanguard.py login
python vanguard.py doctor
python vanguard.py --dry-run
python vanguard.py --limit 1 --headed
# Each morning (no environment activation needed):
./run
```

Login opens a browser. Sign in manually and press Enter in the terminal to save the
session. A later expired session stops scraping; run `login` again. Login and
verification challenges require manual action. Daily execution has no job/resume
review gate. It never runs the auto-applier.

Outputs appear in `exports/YYYY-MM-DD/` by default. Run summaries live in `var/runs/`.
PDFs and LaTeX leftovers are removed from the individual job folders only after the
exporter verifies destination hashes. JD, report and resume source remain locally.

## Reuse an existing installation

Your current job-scraper and job-agent directories can remain in place. Set
`scraper_dir` and `agent_dir` to their absolute paths and retain their resume prefix,
GitHub username and email in your local configuration. The runner uses the bundled
portable exporter against that agent directory. Existing scripts still determine
generation behavior; use the bundled scraper for the configurable identity and
explicit deferred export behavior.

Do not move an existing resume state file independently of its job folders. Generation
keys include the resolved agent path, and state entries store absolute folder paths.
If migrating data to the bundled scraper, retain the same agent directory and copy
`data/` and `browser-profile/` while no pipeline or browser is running. Preserve the
original files until you have checked a run. Do not copy these files to GitHub.

## Troubleshooting

- Missing commands: activate `.venv`, install requirements and the listed system tools,
  and make sure Codex is on `PATH`.
- No display on WSL: use WSLg or a working Linux display for initial login. See the
  [scraper reference](scraper-reference.md) for manual browser attach options. The full
  pipeline uses Linux file locking and is not supported by native Windows Python.
- Login/verification wall: run `python vanguard.py login`; inspect the scrape review
  artifacts. The scraper does not bypass challenges.
- No new jobs: all visible cards may already be known or filtered out. The resume
  stage still processes pending fetched JDs.
- Generation failed: inspect `job-scraper/data/resumes/` logs and the run report. Run
  `python vanguard.py run --from-stage resumes` to retry. Completed entries are skipped.
- Export failed: check the destination/mount and receipts, then run
  `python vanguard.py run --from-stage export`. Stale/missing receipts require verification;
  PDFs are never deleted on a failed copy/hash check. An interrupted partial export may
  leave destination files; inspect and relocate that incomplete job's copies before retry.
- Another command is running: wait for it to exit. OS locks release on process exit;
  deleting lock files while a process is alive does not safely unlock the workspace.

`doctor` checks local files, tools and destination availability. It cannot establish
live selectors, browser installation usability, authentication, model access or usage limits.

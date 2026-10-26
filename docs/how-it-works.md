# How it works

```mermaid
flowchart LR
    A[Morning command] --> B[Preflight and lock]
    B --> C[Collect and deduplicate jobs]
    C --> D[Fetch job descriptions]
    D --> E[Generate pending resumes]
    E --> F[Verify layout and record hashes]
    F --> G[Export PDFs, JDs and reports]
    G --> H[Verify copies and clean build files]
```

## The daily runner

`vanguard.py` loads local YAML, checks required tools/files and the export destination,
locks the scraper workspace and runs each stage as a subprocess. Stage output streams
to the terminal. A timestamped JSON report records stage results and exit codes.
The command stops on a nonzero result and returns failure to the terminal. Interrupted
runs retain an `interrupted` report. `--dry-run` prints commands without invoking them
or creating files. `--from-stage` lets you restart generation/export after a failure.

`--limit` controls new jobs collected, not the number of pending resumes. This avoids
stranding jobs from earlier runs. Each component remains usable independently.
Avoid running component scripts concurrently with the daily pipeline; the outer lock
coordinates Vanguard invocations, while resume generation also has its own batch lock.

## Scrape and fetch

`job-scraper/scrape.py run` collects cards using the saved Jobright browser session,
filters configured categories, deduplicates links and company/title pairs, and writes
`data/jobs.csv`. It fetches descriptions after successful collection. Descriptions land
in `data/jds/`; blocked or incomplete pages get review artifacts and `needs_review`.
The resume stage selects only `jd_fetched` rows, respecting excluded company policy.
Selector and scroll details are in the [scraper reference](scraper-reference.md).

## Resume generation

`generate_resumes.py` processes jobs sequentially through `codex exec`. It uses
`job-agent/scripts/new_job.py` to create a folder with the unchanged JD and metadata.
Codex reads the agent instructions and bases, tailors `resume.tex`, compiles/verifies,
writes `report.md`, and logs tracker metadata. The Python batch verifies the result
again and checks project spacing before marking the entry completed.

State in `job-scraper/data/resumes/state.json` is keyed by resolved agent directory,
scraper job ID and JD hash. Completed entries remain completed after export removes
local PDFs. Changed JD text creates a new generation entry. Failed entries reuse their
job folders on retry. A deliberate regeneration uses the component CLI's `--force`
and optional `--job-id`; adding notes alone does not invalidate completion.

Mechanical verification checks one-page compilation, section line counts, skills
character budgets, project link formatting and replacement bullet constraints. It
cannot establish individual qualifications or measured claims. The existing Strategy B
assumed-completion project behavior is retained; reports distinguish assumptions and
implementation work. The pipeline has no interactive resume approval gate.

## Export

The outer runner disables automatic export inside tracker logging and instructs Codex
to defer export. The bundled exporter then runs once after the resume stage succeeds.
Only jobs with a `.verified.json` receipt matching both the PDF and LaTeX hashes are
eligible. It copies the verified PDF, `jd.txt` and `report.md` into the configured dated
folder, verifies sizes/SHA-256 hashes, records export metadata and updates matching tracker
rows. It then removes only the source PDF and allowed LaTeX leftovers. Source, JD,
report and answers remain. The daily runner never requests job-directory purging.

Export markers prevent duplicate exports on reruns. Missing/stale verification and copy
failures are reported with a nonzero exit; valid jobs may still export in that batch.
An interrupted copy can leave a partial destination needing manual inspection before retry.

## Auto-applier boundary

`job-scraper/apply_jobs.py` and `job_pipeline/applications/` remain available for
experimental development. They have their own preparation/review/account/submission
state and tests. The morning runner does not invoke them. See the
[development guide](auto-applier.md) for entry points and limitations.

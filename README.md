# Vanguard

One morning command to collect jobs, fetch descriptions, tailor and verify resumes,
and export the finished PDFs with their job descriptions and reports.

```bash
./run
```

The daily pipeline runs automatically after setup, without job or resume approval
prompts. It processes all pending fetched descriptions, including unfinished work
from earlier runs. It stops when a stage fails so you can fix the issue and rerun.
The auto-applier is work in progress and is separate from the daily command.

## Start here

1. Follow the [setup guide](docs/setup.md) to install dependencies, add your resume
   bases and contact details, choose an export folder, and log into Jobright once.
2. Check your setup with `./run doctor`.
3. Run `./run --limit 15` each morning.

## Commands

```bash
python vanguard.py --dry-run                  # Show commands without running them
python vanguard.py --limit 10                 # Collect up to 10 new jobs, then process pending JDs
python vanguard.py --headed                   # Show the scraping browser
python vanguard.py run --from-stage resumes   # Retry generation and export without scraping
python vanguard.py run --from-stage export    # Retry only export
python vanguard.py login                      # Save or refresh your Jobright session
```

## Automatic daily runs

Use the [scheduling guide](docs/scheduling.md) to install a Windows Task Scheduler
job at 6 AM and noon Arizona time, every day. It starts WSL automatically, requests
wake from sleep, catches up missed times and retries unfinished stages.

## Project layout

```text
Vanguard/
├── run                     # Launcher using the local Python environment
├── vanguard.py             # Daily command, preflight, locking and run reports
├── config/                 # Shared example and ignored local pipeline settings
├── scripts/                # Scheduled launcher and Windows task setup
├── job-scraper/
│   ├── scrape.py           # Collection and JD fetching
│   ├── generate_resumes.py # Sequential Codex resume generation
│   ├── apply_jobs.py       # Experimental auto-applier CLI
│   ├── job_pipeline/       # Extraction, filtering and application modules
│   └── tests/              # Offline regression tests and browser fixtures
├── job-agent/
│   ├── base/               # Public examples; private resume bases stay local
│   ├── scripts/            # Intake, verification, tracker, export, outreach helpers
│   └── tests/
├── docs/                   # Setup, architecture and auto-applier development
└── tests/                  # Pipeline orchestration tests
```

`job-scraper/data/`, browser profiles, job folders, tracker records, local settings,
run reports and exports are excluded from Git. Example resumes contain fictional
placeholder content. Add your own facts before generating real applications.

See [how it works](docs/how-it-works.md), [troubleshooting](docs/setup.md#troubleshooting),
and [contributing](CONTRIBUTING.md). Contributors can start with the
[auto-applier guide](docs/auto-applier.md).

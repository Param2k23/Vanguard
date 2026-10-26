# Job scraper

Jobright collection, JD extraction, resume batch integration and the experimental
application pipeline live here. Use `python vanguard.py` from the repository root
for daily runs. See [setup](../docs/setup.md), [how it works](../docs/how-it-works.md),
and [scraper reference](../docs/scraper-reference.md).

Component commands (run from this directory with the root environment activated):

```bash
python scrape.py --login
python scrape.py run --limit 15
python scrape.py status
python generate_resumes.py --dry-run
python apply_jobs.py --help
```

`python scrape.py export` produces a CSV of ready jobs. The daily pipeline's export
stage instead exports verified PDFs, descriptions and reports.

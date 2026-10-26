# Resume agent

`base/` holds tailoring instructions and example LaTeX templates. Your actual resume
bases are local ignored files. `scripts/` contains job intake, verification, tracker
logging, export and optional outreach helpers. `jobs/`, `tracker/` and `outreach/`
are generated locally and excluded from Git.

The daily entry point is `python vanguard.py` in the repository root. See
[setup](../docs/setup.md) for base filenames and prerequisites. Verification checks
compilation, page count, section budgets, project links and replacement bullet budgets.
It writes a hash receipt required by the exporter. Export copies and hashes files before
removing the local PDF and LaTeX build leftovers; it retains source, JD and report.

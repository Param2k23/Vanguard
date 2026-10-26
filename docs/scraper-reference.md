# Local Jobright scraper

A standalone Python + Playwright scraper with an optional resume batch script. The scraper makes no model calls and requires no API keys. All scraper paths are relative to this repository, regardless of the shell working directory. Run only one scraper process at a time; CSV updates and browser profiles are not designed for concurrent writers.

## WSL2 Ubuntu setup

Run these commands yourself from this folder. The scraper does not install system packages or run sudo:

```bash
cd Vanguard/job-scraper
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
sudo .venv/bin/python -m playwright install-deps chromium
PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers" python -m playwright install chromium
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.playwright-browsers"
```

If `venv` is unavailable, first run `sudo apt-get update` and `sudo apt-get install python3-venv`. Browser binaries are deliberately installed inside this folder. Set that same `PLAYWRIGHT_BROWSERS_PATH` in each new terminal before running the scraper. System dependency installation is the only setup command that changes system directories; you run it explicitly yourself.

Browser installation reference: [Playwright browsers](https://playwright.dev/python/docs/browsers).

## Configure and log in once

Edit `config.yaml`: replace `feed_url: REPLACE_WITH_YOUR_JOBRIGHT_FEED_URL` with the complete URL from your logged-in Jobright feed. Feed selectors were checked against the saved logged-in feed HTML from 2026-10-12. Detail/ATS selectors remain guesses requiring live verification.

```bash
python scrape.py --login
```

A visible browser opens Jobright. Log in manually, navigate as needed, then press Enter in the terminal. Closing the context saves the session in `browser-profile/`. No passwords are read, stored, or filled by this program. The profile contains sensitive session data: do not share it. Expired Jobright sessions stop the run and request `--login` again.

WSL needs a working display (normally WSLg on supported Windows installations). If the browser cannot open, use the Windows-native alternative below. Do not try to solve verification by bypassing it. If headless shows verification or blocking, try:

```bash
python scrape.py run --limit 15 --headed --debug
```

## Daily use

```bash
python scrape.py --limit 15
python scrape.py status
python scrape.py export
```

`python scrape.py` runs the whole pipeline: collect up to 15 new jobs, then automatically fetch descriptions for collected jobs and retry Jobright jobs marked `needs_review`. Use `--limit` to change the collection limit. The explicit `run` command still works. Fetching starts only after collection finishes successfully. Successful descriptions are saved in `data/jds/`; pages that need manual review are marked in `data/jobs.csv` and saved under `data/review/`.

Individual stages:

```bash
python scrape.py collect --limit 15
python scrape.py fetch
```

Normal runs default to headless. `collect` saves at most N **new** jobs, skipping existing links and normalized company/title pairs. It scans a finite number of scroll rounds (`max_scroll_attempts`, default 25); it may return fewer than N. Card data is read before visiting detail pages. On returning to the feed the previous scroll position is restored, though virtualized or dynamically reordered feeds may still need selector/scroll tuning. Original application URLs are read from Jobright’s embedded posting data (`applyLink` or `originalUrl`), with anchor links matching `apply_links` as a fallback; no Apply buttons are clicked. The original URL is saved in `source_url` for application/export use. JD fetching reads the Jobright page whenever the row has a Jobright URL, waits for its description sections, and combines responsibilities, qualifications, and benefits. It does not visit the employer site for those rows. Rows without a Jobright URL use the original posting.

All CSS selectors, including extraction and wall detection, are in `config.yaml`. The program uses one page and one browser session, with randomized 2–6 second delays between navigation attempts. There are no automatic retries of failed pages. Logs at `logs/run-YYYY-MM-DD.log` contain job IDs, statuses, and exception classes; cookies, tokens, URLs, and page contents are not logged. CSVs, screenshots and HTML may contain private page data: inspect and redact before sharing.

`data/jobs.csv` holds `id,collected_at,company,title,jobright_url,source_url,status,jd_path,notes`. Successful JDs have a company/title/link/date header and plain description in `data/jds/`. `data/ready.csv` contains company, title, preferred link and JD path for successful rows. JD paths are relative to this repository; another project should resolve them against this folder. UTF-8 CSV quoting preserves commas and newlines.

## Review failed pages

`needs_review` means login/verification, blocking, an HTTP error, a description under 400 characters, missing requirements/responsibilities language, or a page error. Raw rendered text and a screenshot are saved under `data/review/<id>/`, best effort even if navigation fails. Review reasons appear in `notes`. A Jobright session failure also stops further jobs immediately. Other failed jobs do not stop the batch.

Inspect the review files and the real page manually. After resolving access legitimately, fixing selectors, or correcting the row URL, run `fetch` again: existing `needs_review` rows with Jobright URLs are retried automatically through Jobright. For rows without a Jobright URL, change the status back to `collected` in `data/jobs.csv` before retrying. Do not mark a row `jd_fetched` unless its JD file exists and you have checked it. Captchas, login walls and paywalls are never bypassed. Unknown errors are recorded by exception class to avoid leaking credentials from browser error messages.

Extraction order is JSON-LD JobPosting, host-specific Greenhouse/Lever/Ashby/Workday/SmartRecruiters/Jobright selectors, then the largest main/article block (body text if none exists). These heuristics are imperfect. Cleaning removes navigation, dialogs, recognizable cookie controls and standalone EEO/legal lines. Ambiguous benefits and legal text is retained to avoid deleting real job requirements. Section detection accepts common English synonyms; other languages and unusual layouts may need review.

## Debug selectors

```bash
python scrape.py collect --limit 1 --headed --debug
python scrape.py fetch --headed --debug
```

`--debug` saves rendered HTML, raw text and a screenshot under `debug/` for collection, successful fetches and failed collection; failed fetches always save review text/screenshots. A zero-result collection also saves feed artifacts. Check `cards`, then selectors relative to each card (`title`, `company`, `link`). A card link selector must match an anchor, not a container. If a panel scrolls instead of the window, set `scroll_container`. Tune `settle_ms` if descriptions render slowly. Share redacted debug artifacts when requesting selector fixes. Feed selectors match all seven cards in the saved Jobright HTML. The scroll panel uses a readable CSS module prefix without its generated hash. Detail-page selectors still need live verification.

## Optional Chrome attach

Attach uses a separate Chrome profile and its own saved login, rather than `browser-profile/`. Launch Chrome yourself in a terminal on Ubuntu with a working display:

```bash
google-chrome --remote-debugging-port=9222 --user-data-dir="$PWD/browser-profile/chrome-attach"
python scrape.py --login --attach
python scrape.py run --limit 15 --attach 9222
```

Do not launch the scraper's persistent context against the attach profile. The scraper opens and closes its own tab and leaves existing Chrome tabs open. `--headed` has no effect on an attached browser. Keep DevTools accessible only locally. Chrome requires a separate non-default data directory for remote debugging: [Chrome documentation](https://developer.chrome.com/blog/remote-debugging-port). Connection reference: [Playwright CDP](https://playwright.dev/python/docs/api/class-browsertype#browser-type-connect-over-cdp).

### Windows-native fallback when WSL has no display

Use Windows Python and Chrome to run this same project from its WSL filesystem path; no other project is required. Find your distro name using `wsl.exe -l -q` in PowerShell. Replace `Ubuntu` below if needed. Create the Windows environment inside this project (Linux `.venv` cannot be used by Windows Python):

```powershell
Set-Location '\\wsl.localhost\Ubuntu\home\param\job-scraper'
py -m venv .venv-win
.\.venv-win\Scripts\python.exe -m pip install -r requirements.txt
$env:PLAYWRIGHT_BROWSERS_PATH = "$PWD\.playwright-browsers-win"
.\.venv-win\Scripts\python.exe -m playwright install chromium
.\.venv-win\Scripts\python.exe scrape.py --login
.\.venv-win\Scripts\python.exe scrape.py run --limit 15 --headed
```

Alternatively attach to Windows Chrome, running the scraper from that same PowerShell terminal:

```powershell
& 'C:\Program Files\Google\Chrome\Application\chrome.exe' --remote-debugging-port=9222 --user-data-dir="\\wsl.localhost\Ubuntu\home\param\job-scraper\browser-profile\windows-chrome-attach"
.\.venv-win\Scripts\python.exe scrape.py --login --attach 9222
.\.venv-win\Scripts\python.exe scrape.py run --limit 15 --attach 9222
```

If Chrome is installed under a different path, substitute its executable path. This avoids WSL-to-Windows localhost/network forwarding differences. Use a different profile for Windows and Linux; never open the same profile from both browsers. UNC profile support can vary by local Chrome policy; if that prevents Chrome from opening, use the Windows-native Playwright commands above, which manage their own profile. Close Linux browsers first, and do not alternate Windows and Linux against the same profile; rename the old profile within this project and log in again when changing platforms. No external profile directory is required by these instructions.

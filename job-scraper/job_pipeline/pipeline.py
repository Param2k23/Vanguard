import csv
import logging
import random
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from .content import duplicate, extract, original_posting_url, review_reasons, slug
from .filters import skip_reason

ROOT = Path(__file__).resolve().parent.parent
FIELDS = 'id collected_at company title jobright_url source_url status jd_path notes'.split()


def now():
    return datetime.now(timezone.utc).isoformat()


def read_rows():
    path = ROOT / 'data/jobs.csv'
    if not path.exists():
        return []
    with path.open(newline='', encoding='utf-8') as f:
        return list(csv.DictReader(f))


def write_rows(rows):
    path = ROOT / 'data/jobs.csv'
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    with temp.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    temp.replace(path)
    from .company_policy import classify_scraped_jobs
    classify_scraped_jobs(ROOT, rows)


def safe_url(url):
    return urlsplit(url).scheme in ('http', 'https') and bool(urlsplit(url).hostname)


class SessionExpired(RuntimeError):
    pass


class Pipeline:
    def __init__(self, page, config, debug=False):
        self.page, self.config, self.debug = page, config, debug
        self.loaded = False

    def goto(self, url):
        if not safe_url(url):
            raise ValueError('Expected an absolute HTTP(S) URL')
        if self.loaded:
            time.sleep(random.uniform(2, 6))
        self.loaded = True
        response = self.page.goto(url, wait_until='domcontentloaded', timeout=self.config['navigation_timeout_ms'])
        self.page.wait_for_timeout(self.config['settle_ms'])
        return response.status if response else None

    def visible(self, key):
        locator = self.page.locator(self.config['selectors'][key])
        return any(locator.nth(i).is_visible() for i in range(min(locator.count(), 10)))

    def inspect(self, status=None, text=''):
        raw = self.page.locator('body').inner_text(timeout=5000)
        reasons = review_reasons(text, raw, self.page.url, status, self.visible('login_wall'), self.visible('verification'))
        host = urlsplit(self.page.url).hostname or ''
        if (host == 'jobright.ai' or host.endswith('.jobright.ai')) and any('requires login' in r for r in reasons):
            raise SessionExpired('Jobright session expired. Run python scrape.py --login again.')
        return raw, reasons

    def artifacts(self, folder, html=False):
        folder.mkdir(parents=True, exist_ok=True)
        # Best effort: a crashed/closed page must not prevent CSV persistence.
        for name, action in [('page.txt', lambda: self.page.locator('body').inner_text(timeout=3000)),
                             *([('page.html', self.page.content)] if html else [])]:
            try:
                (folder / name).write_text(action(), encoding='utf-8')
            except Exception:
                logging.warning('Could not save %s', name)
        try:
            self.page.screenshot(path=str(folder / 'page.png'), full_page=True, timeout=10000)
        except Exception:
            logging.warning('Could not save screenshot')

    def wait_for_cards(self, attempt):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        cards = self.page.locator(self.config['selectors']['cards'])
        try:
            cards.first.wait_for(state='attached', timeout=self.config.get('card_wait_timeout_ms', 15000))
        except PlaywrightTimeoutError:
            pass
        count = cards.count()
        logging.info('Collection scroll step %d: %d cards found', attempt, count)
        print(f'Scroll step {attempt}: {count} cards found')
        if count == 0:
            raw = self.page.locator('body').inner_text(timeout=5000)
            reasons = review_reasons('', raw, self.page.url,
                                     login_visible=self.visible('login_wall'),
                                     verification_visible=self.visible('verification'))
            login = any('requires login' in reason for reason in reasons)
            title = self.page.title()
            self.artifacts(ROOT / 'debug' / f'zero-cards-{uuid.uuid4().hex[:8]}', True)
            message = (f'Zero job cards found after waiting. URL: {self.page.url}\n'
                       f'Page title: {title}\nLooks like a login page: {"yes" if login else "no"}. ')
            if login:
                raise SessionExpired(message + 'Run python scrape.py --login again.')
            walls = [r for r in reasons if 'verification' in r or 'blocked' in r]
            raise RuntimeError(message + ('; '.join(walls) if walls else
                               'Check debug artifacts and configured card selectors.'))
        return cards, count

    def feed_scroll(self, action='measure', top=0):
        return self.page.locator(self.config['selectors']['cards']).first.evaluate(
            (ROOT / 'job_pipeline/scroll.js').read_text(),
            dict(container=self.config['selectors'].get('scroll_container'), action=action, top=top))

    def card_keys(self):
        return frozenset(self.page.locator(self.config['selectors']['cards']).evaluate_all(
            "(cards, linkSelector) => cards.map(card => card.id || "
            "card.querySelector(linkSelector)?.getAttribute('href') || card.textContent)",
            self.config['selectors']['link']))

    def advance_feed(self, previous_keys):
        state = self.feed_scroll('advance')
        logging.info('Feed scroll target=%s top=%s->%s viewport=%s total=%s bottom=%s',
                     state['target'], state['before'], state['top'], state['height'],
                     state['total'], state['bottom'])
        print(f"Feed scroll ({state['target']}): {state['before']} -> {state['top']} "
              f"of {state['total']} (viewport {state['height']})")
        self.page.wait_for_timeout(random.randint(2000, 6000))
        # Virtualized lists can keep the same count while replacing every card.
        deadline = time.monotonic() + self.config.get('scroll_wait_timeout_ms', 6000) / 1000
        while self.card_keys() == previous_keys and time.monotonic() < deadline:
            self.page.wait_for_timeout(250)

    def filter_reason(self, scope, detail=False):
        selectors = self.config['selectors']
        prefix = 'detail' if detail else 'card'
        def labels(key):
            # Missing optional labels do not imply a company classification.
            selector = selectors.get(key)
            if not selector:
                return []
            nodes = scope.locator(selector)
            return nodes.evaluate_all("nodes => nodes.map(n => "
                                      "[...new Set([n.textContent, n.getAttribute('title'), n.getAttribute('aria-label')]"
                                      ".filter(Boolean).map(s => s.trim()))].join(' '))")
        return skip_reason(labels(f'{prefix}_badges'), labels(f'{prefix}_company_types'),
                           self.config.get('filters', {}), locations=labels(f'{prefix}_locations'),
                           companies=labels('detail_company' if detail else 'company'))

    def collect(self, limit, session=None):
        self.collection_started = time.monotonic()
        feed = self.config['feed_url']
        if not safe_url(feed):
            raise ValueError('Set feed_url in config.yaml to your logged-in Jobright feed URL.')
        rows = read_rows()
        added, seen, parsed_total = 0, set(), 0
        observed_keys, stale_steps = set(), 0
        filtered_total = 0
        try:
            status = self.goto(feed)
            selectors = self.config['selectors']
            for attempt in range(self.config['max_scroll_attempts'] + 1):
                if session and not session.page(attempt):
                    break
                cards, card_count = self.wait_for_cards(attempt)
                _, reasons = self.inspect(status, 'requirements ' * 40)
                if reasons:
                    raise RuntimeError('; '.join(reasons))
                keys = self.card_keys()
                new_keys = keys - observed_keys
                observed_keys.update(keys)
                stale_steps = stale_steps + 1 if not new_keys else 0
                logging.info('Collection step %d: %d new card identities, %d stalled steps',
                             attempt, len(new_keys), stale_steps)
                if stale_steps >= self.config.get('max_stalled_scrolls', 3):
                    print('Stopping collection: no new card identities after repeated scrolls; '
                          'continuing with collected jobs.')
                    logging.warning('Feed stalled or exhausted after %d scrolls without new cards', stale_steps)
                    self.artifacts(ROOT / 'debug' / f'scroll-stalled-{uuid.uuid4().hex[:8]}', True)
                    break
                scroll_position = self.feed_scroll()['top']
                candidates = []
                parsed_step, skipped_step = 0, 0
                for i in range(card_count):
                    if session and not session.scan():
                        break
                    card = cards.nth(i)
                    try:
                        reason = self.filter_reason(card)
                        if reason:
                            filtered_total += 1
                            logging.info('Collection step %d card %d filtered: %s', attempt, i, reason)
                            print(f'Skipped card: {reason}')
                            continue
                        title = card.locator(selectors['title']).first.inner_text(timeout=1500).strip()
                        company = card.locator(selectors['company']).first.inner_text(timeout=1500).strip()
                        href = card.locator(selectors['link']).first.get_attribute('href', timeout=1500)
                        link = urljoin(self.page.url, href or '')
                        if not title or not company or not href or not safe_url(link):
                            skipped_step += 1
                            continue
                        parsed_step += 1
                        parsed_total += 1
                        candidate = dict(company=company, title=title, jobright_url=link, source_url='')
                        if session and link in seen:
                            session.state['current_run_duplicates'] += 1
                        if link in seen or (not session and duplicate(candidate, rows)):
                            continue
                        seen.add(link)
                        candidates.append(candidate)
                        if not session and added + len(candidates) >= limit:
                            break
                    except Exception as exc:
                        skipped_step += 1
                        logging.info('Card %d: configured field extraction failed (%s)', i, type(exc).__name__)
                logging.info('Collection scroll step %d: %d parsed, %d invalid, %d new candidates',
                             attempt, parsed_step, skipped_step, len(candidates))
                # Read candidates before navigating: the feed may use virtualized cards.
                for candidate in candidates:
                    if session and time.monotonic() - session.started >= session.config['MAX_SCRAPE_DURATION_SECONDS']:
                        session.state['scrape_limit_reached'] = True
                        break
                    if not session and duplicate(candidate, rows):
                        continue
                    host = urlsplit(candidate['jobright_url']).hostname or ''
                    if not (host == 'jobright.ai' or host.endswith('.jobright.ai')):
                        candidate['source_url'] = candidate['jobright_url']
                    else:
                        try:
                            detail_status = self.goto(candidate['jobright_url'])
                            _, detail_reasons = self.inspect(detail_status, 'requirements ' * 40)
                            if not detail_reasons:
                                reason = self.filter_reason(self.page, detail=True)
                                if reason:
                                    filtered_total += 1
                                    logging.info('Detail job filtered: %s', reason)
                                    print(f'Skipped {candidate["company"]}: {reason}')
                                    continue
                                candidate['source_url'] = original_posting_url(self.page.content())
                                links = self.page.locator(selectors['apply_links'])
                                for j in range(links.count() if not candidate['source_url'] else 0):
                                    url = urljoin(self.page.url, links.nth(j).get_attribute('href') or '')
                                    target = urlsplit(url).hostname or ''
                                    if safe_url(url) and target != 'jobright.ai' and not target.endswith('.jobright.ai'):
                                        candidate['source_url'] = url
                                        break
                            elif self.debug:
                                self.artifacts(ROOT / 'debug' / f'detail-{uuid.uuid4().hex[:8]}', True)
                        except SessionExpired:
                            raise
                        except Exception:
                            logging.info('Original posting link unavailable; retaining Jobright URL')
                    if not session and duplicate(candidate, rows):
                        continue
                    record = dict(id=uuid.uuid4().hex[:12], collected_at=now(), **candidate,
                                  status='collected', jd_path='', notes='')
                    useful = session.accept(record) if session else True
                    if not session or session.last_new:
                        rows.append(record)
                        write_rows(rows)
                    added += int(useful)
                    if added >= limit:
                        break
                if session:
                    session.end_page()
                if added >= limit or attempt == self.config['max_scroll_attempts'] or (session and session.bounded()):
                    break
                if candidates and self.page.url != feed:
                    status = self.goto(feed)
                    self.wait_for_cards(attempt)
                    self.feed_scroll('restore', scroll_position)
                    self.page.wait_for_timeout(self.config['settle_ms'])
                _, reasons = self.inspect(status, 'requirements ' * 40)
                if reasons:
                    raise RuntimeError('; '.join(reasons))
                self.advance_feed(self.card_keys())
            if parsed_total == 0 and filtered_total == 0:
                self.artifacts(ROOT / 'debug' / f'invalid-cards-{uuid.uuid4().hex[:8]}', True)
                raise RuntimeError('Job cards matched, but no title/company/link could be extracted. '
                                   'Check field selectors and debug artifacts.')
            if self.debug or added == 0:
                self.artifacts(ROOT / 'debug' / f'collect-{uuid.uuid4().hex[:8]}', True)
            print(f'Collected {added} new jobs (scroll cap: {self.config["max_scroll_attempts"]}; '
                  f'{filtered_total} card/detail filter matches).')
        except Exception:
            if self.debug:
                self.artifacts(ROOT / 'debug' / f'collect-error-{uuid.uuid4().hex[:8]}', True)
            raise

    def fetch(self, job_ids=None):
        rows = read_rows()
        for row in rows:
            jobright_host = urlsplit(row['jobright_url']).hostname or ''
            has_jobright = jobright_host == 'jobright.ai' or jobright_host.endswith('.jobright.ai')
            retry_review = row['status'] == 'needs_review' and has_jobright
            if (row['status'] != 'collected' and not retry_review) or (job_ids is not None and row['id'] not in job_ids):
                continue
            # Application destinations are not prerequisites for JD collection.
            url = row['jobright_url'] if has_jobright else (row['source_url'] or row['jobright_url'])
            try:
                status = self.goto(url)  # One attempt, no automatic retries.
                if has_jobright:
                    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
                    try:
                        self.page.locator(
                            '[class*="index_jobDetailContent__"] section:has-text("Responsibilities"), '
                            '[class*="index_jobDetailContent__"] section:has-text("Qualification"), '
                            '[data-testid="job-description"], .job-description'
                        ).first.wait_for(state='visible', timeout=self.config.get('description_wait_timeout_ms', 10000))
                    except PlaywrightTimeoutError:
                        pass  # Inspection below records the actual page problem.
                text, method = extract(self.page.content(), self.page.url, self.config['selectors'])
                _, reasons = self.inspect(status, text)
                if reasons:
                    row.update(status='needs_review', notes='; '.join(reasons))
                    self.artifacts(ROOT / 'data/review' / row['id'])
                else:
                    relative = Path('data/jds') / f'{row["id"]}-{slug(row["company"])}-{slug(row["title"])}.txt'
                    path = ROOT / relative
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(f'Company: {row["company"]}\nTitle: {row["title"]}\nLink: {url}\nDate: {now()}\n\n{text}\n', encoding='utf-8')
                    row.update(status='jd_fetched', jd_path=relative.as_posix(), notes=f'Extracted via {method}')
                    if self.debug:
                        self.artifacts(ROOT / 'debug' / row['id'], True)
            except SessionExpired:
                row.update(status='needs_review', notes='Jobright session expired; run --login again')
                self.artifacts(ROOT / 'data/review' / row['id'])
                write_rows(rows)
                raise
            except Exception as exc:
                # Exception messages can contain URLs/tokens: record the class only.
                row.update(status='needs_review', notes=f'Page error: {type(exc).__name__}')
                self.artifacts(ROOT / 'data/review' / row['id'])
            write_rows(rows)
            logging.info('Job %s: %s', row['id'], row['status'])
            print(f'{row["company"]}: {row["title"]}: {row["status"]}')
            if row['status'] == 'needs_review':
                print(f'  {row["notes"]} (see data/review/{row["id"]}/)')

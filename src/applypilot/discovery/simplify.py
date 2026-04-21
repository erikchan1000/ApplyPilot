"""Simplify.jobs dedicated discovery handler.

Uses Playwright to navigate Simplify's SPA, intercepts the Typesense search
API responses that power the job listings, and extracts structured job data
directly — no LLM needed.

Config is read from the ``simplify:`` section of ``searches.yaml``.
"""

import json
import logging
import re
import shutil
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse, parse_qs

import requests as http_requests
from playwright.sync_api import sync_playwright

from applypilot import config
from applypilot.database import get_connection, init_db

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

SIMPLIFY_DETAIL_BASE = "https://simplify.jobs/p"


# -- Config helpers -----------------------------------------------------------

def _load_simplify_config() -> dict | None:
    """Load the ``simplify:`` block from searches.yaml. Returns None if absent."""
    search_cfg = config.load_search_config()
    return search_cfg.get("simplify")


def _build_url(cfg: dict) -> str:
    """Build the Simplify search URL from config values."""
    params: dict[str, str] = {}

    if state := cfg.get("state"):
        params["state"] = state

    if experience := cfg.get("experience"):
        params["experience"] = experience

    categories = cfg.get("categories", [])
    if categories:
        params["category"] = ";".join(categories)

    if min_salary := cfg.get("min_salary"):
        params["minSalary"] = str(min_salary)

    arrangements = cfg.get("work_arrangement", [])
    if arrangements:
        params["workArrangement"] = ";".join(arrangements)

    return f"https://simplify.jobs/jobs?{urlencode(params, quote_via=quote)}"


# -- Title exclusion (shared pattern) ----------------------------------------

def _load_exclude_titles(search_cfg: dict) -> list[str]:
    return [t.lower() for t in search_cfg.get("exclude_titles", [])]


def _title_ok(title: str | None, exclude_patterns: list[str]) -> bool:
    if not title or not exclude_patterns:
        return True
    title_lower = title.lower()
    return not any(pat in title_lower for pat in exclude_patterns)


# -- Typesense response parsing -----------------------------------------------

def _parse_typesense_hits(data: dict, allowed_categories: set[str] | None = None) -> list[dict]:
    """Extract job dicts from a Typesense search response.

    Typesense returns ``{"hits": [{"document": {...}}, ...], "found": N}``.
    Multi-search wraps this in ``{"results": [<search_response>, ...]}``.

    If *allowed_categories* is provided, only jobs whose ``categories`` field
    overlaps with the allowed set are included.  This filters out recommendation
    / trending / sidebar results that don't match the search filters.
    """
    jobs: list[dict] = []

    hit_lists: list[list] = []
    if "results" in data:
        for result in data["results"]:
            hit_lists.append(result.get("hits", []))
    elif "hits" in data:
        hit_lists.append(data["hits"])

    skipped_category = 0
    for hits in hit_lists:
        for hit in hits:
            doc = hit.get("document", hit)
            job_id = doc.get("id") or doc.get("_id") or ""
            title = doc.get("title") or ""
            company = doc.get("company_name") or ""

            if not title or not job_id:
                continue

            # Filter by function/category — the Typesense field is "functions"
            if allowed_categories:
                doc_funcs = set(doc.get("functions") or [])
                if not doc_funcs & allowed_categories:
                    skipped_category += 1
                    continue

            slug = re.sub(r"[^a-zA-Z0-9]+", "-", title).strip("-")
            detail_url = f"{SIMPLIFY_DETAIL_BASE}/{job_id}/{slug}"

            salary_parts = []
            for key in ("min_salary", "max_salary"):
                val = doc.get(key)
                if val and val > 0:
                    salary_parts.append(f"${val:,.0f}")
            salary = " - ".join(salary_parts) + "/yr" if salary_parts else None

            locations = doc.get("locations") or []
            location = locations[0] if locations else None

            apply_url = f"https://simplify.jobs/jobs/click/{job_id}"

            jobs.append({
                "url": detail_url,
                "title": title,
                "company": company,
                "salary": salary,
                "location": location,
                "description": doc.get("description") or doc.get("summary"),
                "application_url": apply_url,
            })

    if skipped_category:
        log.info("Filtered %d jobs (wrong category from recommendation API)", skipped_category)

    return jobs


# -- Playwright scraping ------------------------------------------------------

def _dismiss_overlays(page) -> None:
    """Close cookie consent, signup upsell, or any overlay blocking the page."""
    # Try Escape key first — works for most HeadlessUI modals
    overlay = page.query_selector("[data-testid='modal-overlay']")
    if overlay and overlay.is_visible():
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)

    for selector in [
        "button:has-text('Accept All')",
        "button:has-text('Reject All')",
        "[role='dialog'] button[aria-label='Close']",
        "[role='dialog'] button:has-text('Close')",
        "[role='dialog'] button:has-text('×')",
        "button:has-text('No thanks')",
        "button:has-text('Maybe later')",
    ]:
        try:
            el = page.query_selector(selector)
            if el and el.is_visible():
                el.click(force=True)
                page.wait_for_timeout(500)
        except Exception:
            pass

    # Final fallback: force-remove the overlay via JS
    overlay = page.query_selector("[data-testid='modal-overlay']")
    if overlay and overlay.is_visible():
        page.evaluate("document.querySelector('[data-testid=\"modal-overlay\"]')?.closest('[id^=\"headlessui-portal\"]')?.remove()")
        page.wait_for_timeout(300)


MIN_JOBS_BEFORE_PAGINATION = 20

_PROFILE_COPY_ITEMS = ["Default/Cookies", "Default/Login Data", "Default/Web Data",
                       "Default/Preferences", "Default/Secure Preferences",
                       "Local State"]


def _prepare_profile_dir() -> Path | None:
    """Copy essential Chrome session files to a temp dir for Playwright.

    Always refreshes cookies and session data from the system Chrome
    profile so the Simplify session stays logged-in.

    Returns the temp profile path, or None if no Chrome profile is found.
    """
    try:
        source = config.get_chrome_user_data()
    except Exception:
        return None
    if not source.exists():
        return None

    dest = config.APP_DIR / "simplify_profile"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "Default").mkdir(exist_ok=True)

    for item in _PROFILE_COPY_ITEMS:
        src_path = source / item
        dst_path = dest / item
        if src_path.exists():
            try:
                shutil.copy2(src_path, dst_path)
            except Exception:
                pass

    return dest


def _scrape_listings(url: str, max_pages: int = 10,
                     allowed_categories: set[str] | None = None) -> list[dict]:
    """Navigate to Simplify, intercept Typesense API, then paginate directly.

    1. Load the page with the user's Chrome profile to get a logged-in session.
    2. Intercept the initial Typesense multi_search request to capture the
       API key and search parameters.
    3. Use direct HTTP calls to the Typesense API for pagination — this
       bypasses the "View More" login gate entirely.
    """
    all_jobs: list[dict] = []
    captured_responses: list[dict] = []
    captured_requests: list[dict] = []

    def on_response(response):
        rurl = response.url
        if "simplify.jobs" not in rurl:
            return
        if "/multi_search" not in rurl:
            return
        try:
            data = response.json()
            if isinstance(data, dict) and "results" in data:
                captured_responses.append(data)
        except Exception:
            pass

    def on_request(request):
        rurl = request.url
        if "simplify.jobs" not in rurl or "/multi_search" not in rurl:
            return
        try:
            captured_requests.append({
                "url": rurl,
                "headers": dict(request.headers),
                "post_data": request.post_data,
            })
        except Exception:
            pass

    profile_dir = _prepare_profile_dir()

    with sync_playwright() as p:
        if profile_dir:
            log.info("Using Chrome profile from %s", profile_dir)
            context = p.chromium.launch_persistent_context(
                str(profile_dir),
                headless=True,
                user_agent=UA,
                args=["--disable-blink-features=AutomationControlled"],
            )
            page = context.pages[0] if context.pages else context.new_page()
        else:
            log.info("No Chrome profile found — using fresh browser")
            browser = p.chromium.launch(headless=True)
            context = browser
            page = browser.new_page(user_agent=UA)

        page.on("response", on_response)
        page.on("request", on_request)

        log.info("Navigating to %s", url)
        page.goto(url, timeout=60000)

        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            page.wait_for_load_state("domcontentloaded", timeout=5000)

        _dismiss_overlays(page)
        page.wait_for_timeout(3000)

        context.close()

    # Parse initial API responses
    for data in captured_responses:
        all_jobs.extend(_parse_typesense_hits(data, allowed_categories))

    log.info("Initial load: %d jobs from API interception", len(all_jobs))

    # --- Direct Typesense API pagination ---
    # Find the multi_search request that contained the job search
    api_url = None
    api_searches = None
    for req in captured_requests:
        if not req.get("post_data"):
            continue
        try:
            body = json.loads(req["post_data"])
            searches = body.get("searches", [])
            # Find the request with the most searches (the main job query)
            if len(searches) >= 2:
                api_url = req["url"]
                api_searches = searches
                break
        except Exception:
            continue

    if api_url and api_searches:
        # Extract API key from URL
        parsed = urlparse(api_url)
        qs = parse_qs(parsed.query)
        api_key = qs.get("x-typesense-api-key", [None])[0]
        base_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"

        if api_key:
            log.info("Typesense API key captured — paginating directly (%d searches)", len(api_searches))
            _paginate_typesense(
                base_url, api_key, api_searches, all_jobs,
                allowed_categories, max_pages,
            )
        else:
            log.warning("No Typesense API key found in URL — skipping pagination")
    else:
        log.warning("No multi_search request captured — skipping pagination")

    # Deduplicate by URL
    seen: set[str] = set()
    unique: list[dict] = []
    for job in all_jobs:
        if job["url"] not in seen:
            seen.add(job["url"])
            unique.append(job)

    return unique


def _paginate_typesense(
    base_url: str,
    api_key: str,
    searches: list[dict],
    all_jobs: list[dict],
    allowed_categories: set[str] | None,
    max_pages: int,
) -> None:
    """Paginate the Typesense multi_search API directly via HTTP.

    Replays the captured search queries with incrementing page numbers
    until no new results are returned or *max_pages* is reached.
    """
    headers = {
        "Content-Type": "application/json",
        "X-TYPESENSE-API-KEY": api_key,
    }
    full_url = f"{base_url}?x-typesense-api-key={api_key}"

    seen_ids: set[str] = {j["url"] for j in all_jobs}
    per_page = 250

    # Bump per_page and start from page 2 (page 1 was the initial load)
    for pg in range(2, max_pages + 2):
        paginated_searches = []
        for s in searches:
            s_copy = dict(s)
            s_copy["page"] = pg
            s_copy["per_page"] = per_page
            paginated_searches.append(s_copy)

        try:
            resp = http_requests.post(
                full_url,
                json={"searches": paginated_searches},
                headers=headers,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            log.warning("Typesense API page %d failed: %s", pg, e)
            break

        new_jobs = _parse_typesense_hits(data, allowed_categories)

        # Filter out already-seen URLs
        truly_new = [j for j in new_jobs if j["url"] not in seen_ids]
        for j in truly_new:
            seen_ids.add(j["url"])

        if not truly_new:
            log.info("API page %d: 0 new jobs — done paginating", pg)
            break

        all_jobs.extend(truly_new)
        log.info("API page %d: +%d jobs (total %d)", pg, len(truly_new), len(all_jobs))

        time.sleep(0.5)


# -- DB storage ---------------------------------------------------------------

def _store_jobs(
    conn: sqlite3.Connection,
    jobs: list[dict],
    exclude_titles: list[str],
) -> tuple[int, int]:
    """Store Simplify jobs in DB with title filtering. Returns (new, existing)."""
    now = datetime.now(timezone.utc).isoformat()
    new = 0
    existing = 0
    title_filtered = 0

    for job in jobs:
        url = job.get("url", "")
        if not url:
            continue
        if exclude_titles and not _title_ok(job.get("title"), exclude_titles):
            title_filtered += 1
            continue
        desc = job.get("description")
        app_url = job.get("application_url")
        try:
            conn.execute(
                "INSERT INTO jobs (url, title, salary, description, location, site, strategy, discovered_at, application_url) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (url, job.get("title"), job.get("salary"), desc,
                 job.get("location"), job.get("company", "Simplify"), "simplify", now, app_url),
            )
            new += 1
        except sqlite3.IntegrityError:
            updates = []
            params = []
            if desc:
                updates.append("description = CASE WHEN description IS NULL OR description = '' THEN ? ELSE description END")
                params.append(desc)
            if app_url:
                updates.append("application_url = CASE WHEN application_url IS NULL THEN ? ELSE application_url END")
                params.append(app_url)
            if updates:
                params.append(url)
                conn.execute(f"UPDATE jobs SET {', '.join(updates)} WHERE url = ?", params)
            existing += 1

    conn.commit()

    if title_filtered:
        log.info("Filtered %d jobs (excluded title)", title_filtered)

    return new, existing


# -- Entry point --------------------------------------------------------------

def run_simplify_discovery() -> dict:
    """Discover jobs from Simplify.jobs using the dedicated handler.

    Returns:
        Dict with stats: new, existing, total, filtered.
    """
    cfg = _load_simplify_config()
    if not cfg:
        log.info("No 'simplify:' config in searches.yaml — skipping Simplify discovery.")
        return {"new": 0, "existing": 0, "total": 0}

    search_cfg = config.load_search_config()
    exclude_titles = _load_exclude_titles(search_cfg)

    url = _build_url(cfg)
    log.info("Simplify URL: %s", url)

    allowed_categories = set(cfg.get("categories", []))
    if allowed_categories:
        log.info("Category filter: %s", ", ".join(sorted(allowed_categories)))

    init_db()
    jobs = _scrape_listings(url, max_pages=25, allowed_categories=allowed_categories or None)
    log.info("Scraped %d unique jobs from Simplify", len(jobs))

    if not jobs:
        log.warning("No jobs found — check Simplify config and URL")
        return {"new": 0, "existing": 0, "total": 0}

    conn = get_connection()
    new, existing = _store_jobs(conn, jobs, exclude_titles)

    log.info("Simplify: %d new, %d existing", new, existing)
    return {"new": new, "existing": existing, "total": len(jobs)}

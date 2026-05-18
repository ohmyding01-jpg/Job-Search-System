"""
LinkedIn job scanner.
Searches by keyword + location, extracts job data, returns Job objects.
Uses human-like delays and scroll patterns to avoid detection.
"""

import asyncio
import random
import re
import hashlib
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode
from playwright.async_api import Page
from rich.console import Console

from tracker import Job

console = Console()

LINKEDIN_JOBS_URL = "https://www.linkedin.com/jobs/search/"


def _random_delay(min_s: float, max_s: float):
    return asyncio.sleep(random.uniform(min_s, max_s))


def _make_job_id(title: str, company: str, url: str) -> str:
    """Deterministic job ID from title + company + URL."""
    raw = f"{title}|{company}|{url}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _extract_linkedin_job_id(url: str) -> Optional[str]:
    """Extract the numeric LinkedIn job ID from a URL."""
    match = re.search(r"/jobs/view/(\d+)", url)
    if match:
        return match.group(1)
    match = re.search(r"currentJobId=(\d+)", url)
    if match:
        return match.group(1)
    return None


async def _build_search_url(config: dict, keyword: str, page_num: int = 0) -> str:
    cfg = config["search"]
    params = {
        "keywords": keyword,
        "location": cfg["location"],
        "geoId": cfg["geo_id"],
        "f_TPR": cfg["date_posted"],
        "start": str(page_num * 25),
    }
    if cfg.get("easy_apply_only"):
        params["f_LF"] = "f_AL"   # LinkedIn Easy Apply filter

    experience_levels = cfg.get("experience_levels", [])
    if experience_levels:
        params["f_E"] = ",".join(str(e) for e in experience_levels)

    work_types = cfg.get("work_types", [])
    if work_types:
        params["f_WT"] = ",".join(str(w) for w in work_types)

    return f"{LINKEDIN_JOBS_URL}?{urlencode(params)}"


async def _extract_job_cards(page: Page) -> list[dict]:
    """Extract job card data from the current search results page."""
    # Wait for any recognisable results container
    try:
        await page.wait_for_selector(
            "li[data-occludable-job-id], .jobs-search__results-list, .scaffold-layout__list",
            timeout=15000,
        )
    except Exception:
        pass  # proceed anyway — will return empty if truly nothing loaded

    # Scroll to trigger lazy-loaded cards
    for _ in range(5):
        await page.evaluate("window.scrollBy(0, 700)")
        await _random_delay(0.6, 1.2)

    jobs_raw = await page.evaluate("""
        () => {
            // Strategy 1: data attribute (most stable)
            let cards = Array.from(document.querySelectorAll('li[data-occludable-job-id]'));

            // Strategy 2: class-based fallback
            if (cards.length === 0) {
                cards = Array.from(document.querySelectorAll(
                    '.job-card-container, .jobs-search-results__list-item, .job-card-wrapper'
                ));
            }

            return cards.map(card => {
                // Job URL / ID
                const linkEl = card.querySelector('a[href*="/jobs/view/"]');
                const url = linkEl ? linkEl.href.split('?')[0] : '';

                // Title — try multiple selector patterns
                const titleEl = card.querySelector([
                    '.job-card-list__title--link',
                    '.job-card-list__title',
                    '.base-search-card__title',
                    'a[href*="/jobs/view/"] strong',
                    '[aria-label]',
                ].join(', '));

                // Company
                const companyEl = card.querySelector([
                    '.job-card-container__primary-description',
                    '.artdeco-entity-lockup__subtitle',
                    '.base-search-card__subtitle',
                    '.job-card-container__company-name',
                ].join(', '));

                // Location
                const locationEl = card.querySelector([
                    '.job-card-container__metadata-item',
                    '.job-search-card__location',
                    '.artdeco-entity-lockup__caption',
                ].join(', '));

                // Easy Apply badge
                const bodyText = card.innerText || '';
                const easyApply = bodyText.includes('Easy Apply');

                return {
                    title: titleEl ? titleEl.innerText.trim() : '',
                    company: companyEl ? companyEl.innerText.trim() : '',
                    location: locationEl ? locationEl.innerText.trim() : '',
                    url,
                    easy_apply: easyApply,
                };
            }).filter(j => j.title && j.url);
        }
    """)

    return jobs_raw


async def _get_job_description(page: Page, job_url: str, config: dict) -> str:
    """Navigate to a job page and extract the full description."""
    try:
        for attempt in range(2):
            try:
                await page.goto(job_url, wait_until="domcontentloaded", timeout=20000)
                break
            except Exception:
                if attempt == 1:
                    raise
                await asyncio.sleep(2)
        await _random_delay(config["browser"]["min_delay"], config["browser"]["max_delay"])

        # Try to expand description — short timeout, non-fatal
        try:
            see_more = page.locator(
                '.jobs-description__footer-button, '
                'button[aria-label*="more"], '
                'button.inline-show-more-text__button'
            )
            if await see_more.count() > 0:
                await see_more.first.click(timeout=3000)
                await asyncio.sleep(0.5)
        except Exception:
            pass

        description = await page.evaluate("""
            () => {
                const selectors = [
                    '.jobs-description__content',
                    '.jobs-box__html-content',
                    '[class*="description"]',
                    '#job-details',
                ];
                for (const sel of selectors) {
                    const el = document.querySelector(sel);
                    if (el && el.innerText.length > 200) return el.innerText.trim();
                }
                return document.body.innerText.substring(0, 3000);
            }
        """)

        return description or ""
    except Exception as e:
        console.print(f"[yellow]Could not fetch description for {job_url}: {e}[/yellow]")
        return ""


async def _get_apply_url(page: Page) -> str:
    """Get the Easy Apply button URL or external apply URL from current job page."""
    try:
        apply_url = await page.evaluate("""
            () => {
                // Easy Apply stays on LinkedIn
                const easyBtn = document.querySelector(
                    'button[aria-label*="Easy Apply"], .jobs-apply-button, #jobs-apply-button-id'
                );
                if (easyBtn) return window.location.href;

                // External apply link — prefer the first non-LinkedIn URL
                const extSelectors = [
                    'a.jobs-apply-button',
                    'a[data-tracking-control-name*="apply"]',
                    'a[href*="greenhouse.io"]',
                    'a[href*="lever.co"]',
                    'a[href*="myworkday"]',
                    'a[href*="workday"]',
                    'a[href*="smartrecruiters"]',
                    'a[href*="bamboohr"]',
                    'a[href*="jazz.hr"]',
                    'a[href*="breezy.hr"]',
                    'a[href*="icims.com"]',
                    'a[href*="taleo"]',
                    'a[href*="indeed"]',
                    'a[href*="apply"]',
                ];
                for (const sel of extSelectors) {
                    const el = document.querySelector(sel);
                    if (el && el.href && !el.href.includes('linkedin.com')) {
                        return el.href;
                    }
                }
                return window.location.href;
            }
        """)
        return apply_url or page.url
    except Exception:
        return page.url


async def scan_jobs(page: Page, config: dict, existing_ids: set[str]) -> list[Job]:
    """
    Main scan entry point. Iterates all keywords and pages.
    Returns list of new Job objects not already in the database.
    """
    cfg = config["search"]
    keywords = cfg["keywords"]
    pages_per_keyword = cfg.get("pages_per_keyword", 2)
    min_d = config["browser"]["min_delay"]
    max_d = config["browser"]["max_delay"]

    all_new_jobs: list[Job] = []
    seen_ids: set[str] = set(existing_ids)
    # Cap new description fetches per scan to avoid browser overload.
    max_new = int(config.get("search", {}).get("max_new_per_scan", 20))

    for keyword in keywords:
        if len(all_new_jobs) >= max_new:
            console.print(f"[dim]New job cap ({max_new}) reached — skipping remaining keywords[/dim]")
            break
        console.print(f"[cyan]Scanning: '{keyword}'[/cyan]")

        for page_num in range(pages_per_keyword):
            search_url = await _build_search_url(config, keyword, page_num)

            nav_ok = False
            for attempt in range(3):
                try:
                    await page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
                    await _random_delay(min_d, max_d)
                    nav_ok = True
                    break
                except Exception as e:
                    backoff = 2 ** attempt
                    console.print(
                        f"[yellow]Nav attempt {attempt + 1}/3 failed for '{keyword}' page {page_num}: {e} "
                        f"(retry in {backoff}s)[/yellow]"
                    )
                    # Abort any in-progress navigation before retrying.
                    try:
                        await page.evaluate("window.stop()")
                    except Exception:
                        pass
                    await asyncio.sleep(backoff)
            if not nav_ok:
                console.print(f"[red]Skipping '{keyword}' page {page_num} after 3 failed attempts[/red]")
                continue

            # Check if we got blocked
            page_content = await page.content()
            if "authwall" in page.url or "Join LinkedIn" in page_content:
                console.print("[red]LinkedIn authwall hit — session may have expired[/red]")
                break

            cards = await _extract_job_cards(page)
            console.print(f"  Page {page_num + 1}: found {len(cards)} cards")

            if len(cards) == 0 and page_num == 0:
                shot_path = f"logs/debug_{keyword.replace(' ', '_')}.png"
                await page.screenshot(path=shot_path, full_page=False)
                console.print(f"  [dim]Debug screenshot saved: {shot_path}[/dim]")

            for card in cards:
                raw_url = card.get("url", "")
                linkedin_id = _extract_linkedin_job_id(raw_url)
                job_id = linkedin_id or _make_job_id(card["title"], card["company"], raw_url)

                if job_id in seen_ids:
                    continue
                seen_ids.add(job_id)

                # Skip non-Easy Apply if configured
                if cfg.get("easy_apply_only") and not card.get("easy_apply"):
                    continue

                if len(all_new_jobs) >= max_new:
                    break

                # Fetch full description
                await _random_delay(min_d * 0.8, max_d * 0.8)
                description = await _get_job_description(page, raw_url, config)
                apply_url = await _get_apply_url(page)

                job = Job(
                    job_id=job_id,
                    title=card["title"],
                    company=card["company"],
                    location=card.get("location", ""),
                    description=description,
                    apply_url=apply_url,
                    easy_apply=card.get("easy_apply", False),
                    posted_at=datetime.now().isoformat(),
                    source_keyword=keyword,
                )
                all_new_jobs.append(job)
                console.print(
                    f"  [green]+[/green] {job.title} @ {job.company} ({job.location})"
                )

                await _random_delay(min_d, max_d)

        # Pause between keywords to look human
        await _random_delay(3.0, 7.0)

    console.print(f"[bold green]Scan complete: {len(all_new_jobs)} new jobs found[/bold green]")
    return all_new_jobs

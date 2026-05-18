"""
Multi-board scraper via python-jobspy.
Covers: Indeed, ZipRecruiter, Glassdoor.
(Greenhouse, Lever, Ashby, Workable jobs surface here automatically
 since those companies cross-post to Indeed.)
"""

import asyncio
import hashlib
from datetime import datetime
from rich.console import Console
from tracker import Job

console = Console()

# Titles containing any of these are immediately discarded
_REJECT_TITLE_WORDS = [
    'engineer', 'developer', 'software', 'nurse', 'physician', 'doctor',
    'accountant', 'attorney', 'lawyer', 'paralegal', 'designer', 'architect',
    'analyst', 'scientist', 'researcher', 'technician', 'mechanic', 'electrician',
    'plumber', 'driver', 'warehouse', 'associate', 'coordinator', 'assistant',
    'receptionist', 'clerk', 'representative', 'specialist', 'advisor',
    'consultant', 'recruiter', 'hr ', 'human resources', 'marketing', 'sales',
    'finance', 'accounting', 'tax ', 'audit', 'controller', 'cfo', 'cto', 'ceo',
    'construction', 'scheduler', 'field service', 'supply chain', 'procurement',
    'network ', 'security engineer', 'sysadmin',
    'devops', 'cloud engineer', 'machine learning', 'ai engineer',
]

# Title must contain at least one of these to pass
_REQUIRE_TITLE_WORDS = [
    'project manager', 'program manager', 'programme manager',
    'project management', 'program management',
    'delivery manager', 'delivery lead',
    'pmo', 'scrum master', 'agile coach', 'agile lead',
    'engagement manager', 'product manager', 'product owner',
    'operations manager', 'project lead', 'project director',
    'tpm', 'technical program', 'technical project',
    'it project', 'it program', 'it manager',
]


def _is_relevant(title: str) -> bool:
    t = title.lower()
    if not any(w in t for w in _REQUIRE_TITLE_WORDS):
        return False
    if any(w in t for w in _REJECT_TITLE_WORDS):
        return False
    return True


def _make_id(title: str, company: str, url: str) -> str:
    raw = f"{title}|{company}|{url}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _row_to_job(row, source_keyword: str) -> Job:
    title = str(row.get("title", "")).strip()
    company = str(row.get("company", "")).strip()
    location = str(row.get("location", "")).strip()
    url = str(row.get("job_url", "")).strip()
    description = str(row.get("description", "") or "").strip()[:6000]

    return Job(
        job_id=_make_id(title, company, url),
        title=title,
        company=company,
        location=location,
        description=description,
        apply_url=url,
        easy_apply=False,       # JobSpy boards are external apply links
        posted_at=datetime.now().isoformat(),
        source_keyword=source_keyword,
        source_platform="job_board",
    )


async def scan_jobspy(config: dict, existing_ids: set[str]) -> list[Job]:
    """
    Scrape Indeed, ZipRecruiter, and Glassdoor for all configured keywords.
    Returns new Job objects not already in existing_ids.
    """
    try:
        from jobspy import scrape_jobs
    except ImportError:
        console.print("[yellow]python-jobspy not installed — skipping Indeed/ZipRecruiter/Glassdoor[/yellow]")
        return []

    cfg = config["search"]
    jobspy_cfg = config.get("jobspy", {})
    sites = jobspy_cfg.get("sites", ["indeed", "zip_recruiter"])
    hours_old = jobspy_cfg.get("hours_old", 24)
    results_per_keyword = jobspy_cfg.get("results_per_keyword", 30)
    location = jobspy_cfg.get("location", cfg.get("location", "Washington DC"))

    keywords = cfg["keywords"]
    all_new: list[Job] = []
    seen: set[str] = set(existing_ids)

    for keyword in keywords:
        console.print(f"[cyan]JobSpy scanning '{keyword}' on {', '.join(sites)}[/cyan]")
        try:
            # Run blocking jobspy call in executor so we don't block the event loop
            loop = asyncio.get_event_loop()
            df = await loop.run_in_executor(
                None,
                lambda kw=keyword: scrape_jobs(
                    site_name=sites,
                    search_term=kw,
                    location=location,
                    results_wanted=results_per_keyword,
                    hours_old=hours_old,
                    country_indeed="USA",
                    verbose=0,
                ),
            )

            if df is None or df.empty:
                console.print(f"  No results for '{keyword}'")
                continue

            count = 0
            skipped = 0
            for _, row in df.iterrows():
                job = _row_to_job(row.to_dict(), keyword)
                if not job.title or not job.apply_url:
                    continue
                if not _is_relevant(job.title):
                    skipped += 1
                    continue
                if job.job_id in seen:
                    continue
                seen.add(job.job_id)
                all_new.append(job)
                count += 1
                console.print(f"  [green]+[/green] {job.title} @ {job.company} ({job.location})")

            console.print(f"  '{keyword}': {count} new jobs ({skipped} irrelevant filtered)")

        except Exception as e:
            console.print(f"  [red]JobSpy error for '{keyword}': {e}[/red]")

        # Polite delay between keywords
        await asyncio.sleep(3)

    console.print(f"[bold green]JobSpy scan complete: {len(all_new)} new jobs[/bold green]")
    return all_new

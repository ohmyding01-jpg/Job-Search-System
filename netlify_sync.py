"""
Netlify tracker integration.
Pushes discovered jobs to the existing tracker UI and fetches approved jobs for auto-apply.
"""

import os
import httpx
from datetime import datetime
from rich.console import Console
from tracker import Job

console = Console()

NETLIFY_URL = os.getenv("NETLIFY_SITE_URL", "https://precious-cupcake-3792b5.netlify.app")


def _local_date_from_iso(value: str) -> str:
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.date().isoformat()
    except Exception:
        return str(value)[:10]


def _count_applied_on_date(opportunities: list[dict], target_date: str) -> int:
    count = 0
    for opp in opportunities:
        if opp.get("status") != "applied":
            continue
        applied_at = opp.get("applied_date") or opp.get("last_action_date") or ""
        if _local_date_from_iso(applied_at) == target_date:
            count += 1
    return count


def _norm_text(value: str) -> str:
    return " ".join(str(value or "").lower().split())


def _norm_url(value: str) -> str:
    try:
        from urllib.parse import urlparse, urlunparse

        parsed = urlparse(value or "")
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")).lower()
    except Exception:
        return _norm_text(value)


def _matches_job(opp: dict, job: Job) -> bool:
    if _norm_text(opp.get("title")) != _norm_text(job.title):
        return False
    if _norm_text(opp.get("company")) != _norm_text(job.company):
        return False

    opp_url = opp.get("url") or opp.get("canonical_job_url") or opp.get("application_url") or ""
    if opp_url and job.apply_url and _norm_url(opp_url) == _norm_url(job.apply_url):
        return True

    return _norm_text(opp.get("location")) == _norm_text(job.location)


async def push_jobs_to_netlify(jobs: list[Job]) -> dict:
    """
    POST discovered jobs to the Netlify intake endpoint.
    Returns summary dict from the response.
    """
    if not jobs:
        return {}

    payload = {
        "source": "linkedin-agent",
        "sourceType": "manual",   # 'manual' bypasses the LIVE_INTAKE_ENABLED gate
        "jobs": [
            {
                "title": j.title,
                "company": j.company,
                "location": j.location,
                "url": j.apply_url or "",
                "description": (j.description or "")[:4000],
            }
            for j in jobs
        ],
    }

    url = f"{NETLIFY_URL}/.netlify/functions/intake"

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            try:
                data = resp.json()
            except Exception:
                console.print(
                    f"  [yellow]Netlify apply pack sync returned {resp.status_code}: "
                    f"{resp.text[:200]}[/yellow]"
                )
                return {}

            if resp.status_code == 200:
                summary = data.get("summary", {})
                console.print(
                    f"  [green]Netlify sync:[/green] {summary.get('new', 0)} new, "
                    f"{summary.get('deduped', 0)} deduped, "
                    f"{summary.get('errors', 0)} errors"
                )
                return summary
            else:
                console.print(f"  [yellow]Netlify intake returned {resp.status_code}: {data.get('error', data)}[/yellow]")
                return {}

    except Exception as e:
        console.print(f"  [red]Netlify sync failed: {e}[/red]")
        return {}


async def sync_apply_pack_to_netlify(
    job: Job,
    scoring: dict,
    resume_content: dict,
    cover_letter_text: str,
    resume_path: str,
    cover_letter_path: str,
) -> bool:
    """
    POST the locally generated tailored resume/cover letter content to the
    tracker so the Netlify Apply Pack page shows the finished review assets.
    """
    url = f"{NETLIFY_URL}/.netlify/functions/sync-apply-pack"
    payload = {
        "job_id": job.job_id,
        "title": job.title,
        "company": job.company,
        "location": job.location,
        "url": job.apply_url or "",
        "application_url": job.apply_url or "",
        "description": (job.description or "")[:4000],
        "score": job.score,
        "scoring": scoring or {},
        "resume_content": resume_content or {},
        "cover_letter_text": cover_letter_text or "",
        "resume_path": str(resume_path),
        "cover_letter_path": str(cover_letter_path),
        "recommended_resume_version": (scoring or {}).get("best_resume_variant", "general_pm"),
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            try:
                data = resp.json()
            except Exception:
                data = {}

            if resp.status_code == 200 and data.get("ok"):
                console.print(
                    f"  [green]Netlify apply pack synced:[/green] "
                    f"{data.get('pack_readiness_score', 0)}% ready"
                )
                return True

            console.print(
                f"  [yellow]Netlify apply pack sync returned {resp.status_code}: "
                f"{data.get('error', data)}[/yellow]"
            )
            return False

    except Exception as e:
        console.print(f"  [red]Netlify apply pack sync failed: {e}[/red]")
        return False


async def fetch_approved_opportunities() -> list[dict]:
    """
    GET opportunities that have been manually approved in the tracker UI.
    Returns list of opportunity dicts with fields: id, title, company, location, application_url, etc.
    """
    url = f"{NETLIFY_URL}/.netlify/functions/opportunities"
    active_statuses = {"apply_pack_generated", "ready_to_apply"}
    closed_statuses = {"applied", "rejected", "ghosted", "withdrawn", "stale", "needs_manual_apply", "archived_low_fit", "needs_apply_url"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            data = resp.json()

            if resp.status_code == 200:
                opps = [
                    opp for opp in data.get("opportunities", [])
                    if opp.get("approval_state") == "approved"
                    and opp.get("status") in active_statuses
                    and opp.get("status") not in closed_statuses
                    and "linkedin.com" in (opp.get("application_url") or opp.get("url") or "").lower()
                ]
                opps.sort(key=lambda opp: int(opp.get("fit_score") or 0), reverse=True)
                if opps:
                    console.print(f"  [cyan]Netlify approved queue:[/cyan] {len(opps)} job(s) ready to apply")
                return opps
            else:
                console.print(f"  [yellow]Netlify opportunities returned {resp.status_code}[/yellow]")
                return []

    except Exception as e:
        console.print(f"  [red]Netlify fetch failed: {e}[/red]")
        return []


async def fetch_applications_today(today: str | None = None) -> int | None:
    """
    Count applications already marked applied in the Netlify tracker today.
    Returns None when the tracker cannot be reached so callers can fall back to
    their local SQLite count.
    """
    target_date = today or datetime.now().date().isoformat()
    url = f"{NETLIFY_URL}/.netlify/functions/opportunities"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                console.print(f"  [yellow]Netlify daily application count returned {resp.status_code}[/yellow]")
                return None

            data = resp.json()
            return _count_applied_on_date(data.get("opportunities", []), target_date)

    except Exception as e:
        console.print(f"  [yellow]Netlify daily application count unavailable: {e}[/yellow]")
        return None


async def mark_applied_in_netlify(opp_id: str, applied_date: str) -> bool:
    """
    PATCH the opportunity in Netlify to mark it as applied.
    """
    url = f"{NETLIFY_URL}/.netlify/functions/opportunities"
    params = {"id": opp_id}
    payload = {"status": "applied", "applied_date": applied_date, "last_action_date": applied_date}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(url, params=params, json=payload)
            return resp.status_code == 200
    except Exception as e:
        console.print(f"  [red]Netlify mark-applied failed for {opp_id}: {e}[/red]")
        return False


async def mark_manual_apply_needed_in_netlify(opp_id: str, reason: str) -> bool:
    """
    Move an approved opportunity out of the auto-apply queue when the Python
    agent cannot submit it automatically.
    """
    url = f"{NETLIFY_URL}/.netlify/functions/opportunities"
    now = datetime.now().isoformat()
    payload = {
        "status": "needs_manual_apply",
        "last_action_date": now,
        "notes": reason,
    }

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.patch(url, params={"id": opp_id}, json=payload)
            return resp.status_code == 200
    except Exception as e:
        console.print(f"  [red]Netlify manual-apply mark failed for {opp_id}: {e}[/red]")
        return False


async def mark_job_manual_apply_needed_in_netlify(job: Job, reason: str) -> bool:
    """
    Mark a Netlify opportunity as needing manual application by matching a local job.
    Useful when local Easy Apply fails before the tracker-approved phase sees it.
    """
    url = f"{NETLIFY_URL}/.netlify/functions/opportunities"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url)
            data = resp.json()
            if resp.status_code != 200:
                console.print(f"  [yellow]Netlify manual sync fetch returned {resp.status_code}[/yellow]")
                return False

            match = next((opp for opp in data.get("opportunities", []) if _matches_job(opp, job)), None)
            if not match:
                console.print(f"  [yellow]Netlify manual sync: no matching opportunity for {job.title} @ {job.company}[/yellow]")
                return False

            return await mark_manual_apply_needed_in_netlify(match.get("id", ""), reason)

    except Exception as e:
        console.print(f"  [red]Netlify manual sync failed for {job.title} @ {job.company}: {e}[/red]")
        return False


async def mark_job_applied_in_netlify(job: Job, applied_date: str) -> bool:
    """
    Mark a locally auto-applied job as applied in Netlify by matching the
    opportunity title/company/location/url.
    """
    url = f"{NETLIFY_URL}/.netlify/functions/opportunities"

    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(url)
            data = resp.json()
            if resp.status_code != 200:
                console.print(f"  [yellow]Netlify applied sync fetch returned {resp.status_code}[/yellow]")
                return False

            match = next((opp for opp in data.get("opportunities", []) if _matches_job(opp, job)), None)
            if not match:
                console.print(f"  [yellow]Netlify applied sync: no matching opportunity for {job.title} @ {job.company}[/yellow]")
                return False

            patch_resp = await client.patch(
                url,
                params={"id": match.get("id")},
                json={
                    "status": "applied",
                    "applied_date": applied_date,
                    "last_action_date": applied_date,
                },
            )
            if patch_resp.status_code == 200:
                console.print("  [green]Netlify status synced:[/green] applied")
                return True

            console.print(f"  [yellow]Netlify applied sync returned {patch_resp.status_code}[/yellow]")
            return False
    except Exception as e:
        console.print(f"  [red]Netlify applied sync failed: {e}[/red]")
        return False

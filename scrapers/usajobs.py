"""
USAJobs.gov REST API scanner.
Free public API — ideal for Samiha's federal/clearance profile.
Register for a free API key at: https://developer.usajobs.gov/
"""

import hashlib
import os
from datetime import datetime
import httpx
from rich.console import Console
from tracker import Job

console = Console()

USAJOBS_API = "https://data.usajobs.gov/api/search"


def _make_id(position_id: str) -> str:
    return hashlib.md5(f"usajobs|{position_id}".encode()).hexdigest()[:16]


def _parse_position(pos: dict) -> Job | None:
    try:
        matched = pos.get("MatchedObjectDescriptor", {})
        position_id = matched.get("PositionID", "")
        title = matched.get("PositionTitle", "").strip()
        org = matched.get("OrganizationName", "").strip()
        locations = matched.get("PositionLocation", [])
        location = locations[0].get("LocationName", "") if locations else ""
        apply_url = matched.get("ApplyURI", [""])[0] if matched.get("ApplyURI") else ""
        description = matched.get("UserArea", {}).get("Details", {}).get("JobSummary", "")
        qualifications = matched.get("UserArea", {}).get("Details", {}).get("Qualifications", "")
        full_desc = f"{description}\n\n{qualifications}".strip()[:6000]
        salary_min = matched.get("PositionRemuneration", [{}])[0].get("MinimumRange", "")
        salary_max = matched.get("PositionRemuneration", [{}])[0].get("MaximumRange", "")
        if salary_min:
            full_desc += f"\n\nSalary: ${salary_min}–${salary_max}"

        if not title:
            return None

        return Job(
            job_id=_make_id(position_id or title + org),
            title=title,
            company=org,
            location=location,
            description=full_desc,
            apply_url=apply_url,
            easy_apply=False,
            posted_at=datetime.now().isoformat(),
            source_keyword="usajobs",
            source_platform="usajobs",
        )
    except Exception:
        return None


async def scan_usajobs(config: dict, existing_ids: set[str]) -> list[Job]:
    """
    Search USAJobs for PM/TPM roles relevant to Samiha's profile.
    Requires USAJOBS_API_KEY and USAJOBS_EMAIL in .env
    """
    api_key = os.getenv("USAJOBS_API_KEY", "")
    email = os.getenv("USAJOBS_EMAIL", "")

    if not api_key or not email:
        console.print(
            "[yellow]USAJobs: set USAJOBS_API_KEY + USAJOBS_EMAIL in .env to enable "
            "(free at developer.usajobs.gov)[/yellow]"
        )
        return []

    usajobs_cfg = config.get("usajobs", {})
    keywords = usajobs_cfg.get("keywords", [
        "Technical Project Manager",
        "IT Project Manager",
        "Program Manager",
        "Agile Project Manager",
        "Project Manager Information Technology",
    ])
    location_name = usajobs_cfg.get("location", "Washington, DC")
    radius = usajobs_cfg.get("radius_miles", 50)
    results_per_keyword = usajobs_cfg.get("results_per_keyword", 25)
    remote_filter = usajobs_cfg.get("include_remote", True)

    headers = {
        "Host": "data.usajobs.gov",
        "User-Agent": email,
        "Authorization-Key": api_key,
    }

    all_new: list[Job] = []
    seen: set[str] = set(existing_ids)

    async with httpx.AsyncClient(timeout=20) as client:
        for keyword in keywords:
            console.print(f"[cyan]USAJobs scanning '{keyword}'[/cyan]")
            try:
                # Location-based search (no RemoteIndicator — that filter excludes on-site results)
                params = {
                    "Keyword": keyword,
                    "LocationName": location_name,
                    "Radius": radius,
                    "ResultsPerPage": results_per_keyword,
                    "DatePosted": 7,
                    "Fields": "Min",
                    "SortField": "OpenDate",
                    "SortDirection": "Desc",
                }

                resp = await client.get(USAJOBS_API, params=params, headers=headers)

                if resp.status_code != 200:
                    console.print(f"  [red]USAJobs API error {resp.status_code}[/red]")
                    continue

                data = resp.json()
                positions = data.get("SearchResult", {}).get("SearchResultItems", [])
                count = 0

                for pos in positions:
                    job = _parse_position(pos)
                    if not job or job.job_id in seen:
                        continue
                    seen.add(job.job_id)
                    all_new.append(job)
                    count += 1
                    console.print(f"  [green]+[/green] {job.title} @ {job.company} ({job.location})")

                console.print(f"  '{keyword}': {count} new federal jobs")

                # Remote-only search — separate call without LocationName/Radius
                if remote_filter:
                    params_remote = {
                        "Keyword": keyword,
                        "RemoteIndicator": "True",
                        "ResultsPerPage": results_per_keyword,
                        "DatePosted": 7,
                        "Fields": "Min",
                        "SortField": "OpenDate",
                        "SortDirection": "Desc",
                    }
                    resp2 = await client.get(USAJOBS_API, params=params_remote, headers=headers)
                    if resp2.status_code == 200:
                        positions2 = resp2.json().get("SearchResult", {}).get("SearchResultItems", [])
                        for pos in positions2:
                            job = _parse_position(pos)
                            if not job or job.job_id in seen:
                                continue
                            seen.add(job.job_id)
                            all_new.append(job)
                            console.print(f"  [green]+[/green] {job.title} @ {job.company} (Remote)")

            except Exception as e:
                console.print(f"  [red]USAJobs error for '{keyword}': {e}[/red]")

    console.print(f"[bold green]USAJobs scan complete: {len(all_new)} new federal jobs[/bold green]")
    return all_new

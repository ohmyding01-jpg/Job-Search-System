import asyncio
import hashlib
import re
from datetime import datetime
from urllib.parse import quote_plus

from rich.console import Console

from boards.common import slugify_keyword
from tracker import Job

console = Console()


def _make_id(url: str) -> str:
    match = re.search(r"/job-details/([a-z0-9-]+)", url, re.I)
    if match:
        return f"careerbuilder-{match.group(1)}"
    return f"careerbuilder-{hashlib.md5(url.encode()).hexdigest()[:16]}"


async def scan_careerbuilder(page, config: dict, existing_ids: set[str]) -> list[Job]:
    cb_cfg = config.get("careerbuilder", {})
    if not cb_cfg.get("enabled", True):
        return []

    keywords = cb_cfg.get("keywords") or config.get("search", {}).get("keywords", [])
    location = cb_cfg.get("location", "United States")
    results_per_keyword = int(cb_cfg.get("results_per_keyword", 10))

    discovered: list[Job] = []
    seen = set(existing_ids)

    for keyword in keywords:
        slug = slugify_keyword(keyword)
        url = f"https://www.careerbuilder.com/jobs-{slug}?location={quote_plus(location)}"
        console.print(f"[cyan]CareerBuilder scanning '{keyword}'[/cyan]")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Wait for any CareerBuilder redirects to fully settle before the
            # next keyword's navigation starts (prevents "interrupted by another
            # navigation" errors on slow/redirect-heavy pages).
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(1)
            rows = await page.evaluate(
                """
                (limit) => {
                    const articles = Array.from(document.querySelectorAll('article'));
                    const items = [];
                    for (const article of articles) {
                        const link = article.querySelector('a[href*="/job-details/"]');
                        if (!link) continue;
                        const text = (article.innerText || '').trim();
                        if (!text) continue;
                        items.push({
                            href: link.href,
                            title: (link.textContent || '').trim(),
                            text,
                            quickApply: /quick apply/i.test(text),
                        });
                        if (items.length >= limit) break;
                    }
                    return items;
                }
                """,
                results_per_keyword,
            )
        except Exception as exc:
            console.print(f"  [red]CareerBuilder scan failed for '{keyword}': {exc}[/red]")
            continue

        for row in rows:
            job_id = _make_id(row["href"])
            if job_id in seen:
                continue
            seen.add(job_id)
            lines = [line.strip() for line in row["text"].splitlines() if line.strip()]
            title = row["title"] or (lines[0] if lines else keyword)
            company = lines[1] if len(lines) > 1 else "CareerBuilder Employer"
            location_text = lines[2] if len(lines) > 2 else location
            job = Job(
                job_id=job_id,
                title=title,
                company=company,
                location=location_text,
                description=row["text"][:6000],
                apply_url=row["href"],
                easy_apply=bool(row["quickApply"]),
                posted_at=datetime.now().isoformat(),
                source_keyword=keyword,
                source_platform="careerbuilder",
            )
            discovered.append(job)
            console.print(f"  [green]+[/green] {job.title} @ {job.company}")

    console.print(f"[bold green]CareerBuilder scan complete: {len(discovered)} new jobs[/bold green]")
    return discovered

import asyncio
import hashlib
import re
from datetime import datetime
from urllib.parse import quote_plus

from rich.console import Console

from tracker import Job

console = Console()


def _make_id(url: str) -> str:
    match = re.search(r"/job-detail/([a-z0-9-]+)", url, re.I)
    if match:
        return f"dice-{match.group(1)}"
    return f"dice-{hashlib.md5(url.encode()).hexdigest()[:16]}"


async def scan_dice(page, config: dict, existing_ids: set[str]) -> list[Job]:
    dice_cfg = config.get("dice", {})
    if not dice_cfg.get("enabled", True):
        return []

    keywords = dice_cfg.get("keywords") or config.get("search", {}).get("keywords", [])
    location = dice_cfg.get("location", "United States")
    results_per_keyword = int(dice_cfg.get("results_per_keyword", 10))

    discovered: list[Job] = []
    seen = set(existing_ids)

    for keyword in keywords:
        url = f"https://www.dice.com/jobs?q={quote_plus(keyword)}&location={quote_plus(location)}"
        console.print(f"[cyan]Dice scanning '{keyword}'[/cyan]")
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            try:
                await page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            await asyncio.sleep(1)
            rows = await page.evaluate(
                """
                (limit) => {
                    const anchors = Array.from(document.querySelectorAll('a[href*="/job-detail/"]'));
                    const items = [];
                    const seen = new Set();
                    for (const anchor of anchors) {
                        const href = anchor.href;
                        if (!href || seen.has(href)) continue;
                        const card = anchor.closest('article, li, div');
                        const text = (card?.innerText || anchor.innerText || '').trim();
                        if (!text) continue;
                        seen.add(href);
                        items.push({
                            href,
                            title: (anchor.innerText || anchor.getAttribute('aria-label') || '').trim(),
                            text,
                            easyApply: /easy apply|apply now/i.test(text),
                        });
                        if (items.length >= limit) break;
                    }
                    return items;
                }
                """,
                results_per_keyword,
            )
        except Exception as exc:
            console.print(f"  [red]Dice scan failed for '{keyword}': {exc}[/red]")
            continue

        for row in rows:
            job_id = _make_id(row["href"])
            if job_id in seen:
                continue
            seen.add(job_id)
            lines = [line.strip() for line in row["text"].splitlines() if line.strip()]
            title = row["title"] or (lines[0] if lines else keyword)
            company = lines[1] if len(lines) > 1 else "Dice Employer"
            location_text = next((line for line in lines if any(token in line.lower() for token in ["remote", ",", "united states"])) , location)
            job = Job(
                job_id=job_id,
                title=title,
                company=company,
                location=location_text,
                description=row["text"][:6000],
                apply_url=row["href"],
                easy_apply=bool(row["easyApply"]),
                posted_at=datetime.now().isoformat(),
                source_keyword=keyword,
                source_platform="dice",
            )
            discovered.append(job)
            console.print(f"  [green]+[/green] {job.title} @ {job.company}")

    console.print(f"[bold green]Dice scan complete: {len(discovered)} new jobs[/bold green]")
    return discovered

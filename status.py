"""
Quick status check — shows what's in the local DB vs Netlify.
Run: python status.py
"""

import asyncio
import aiosqlite
import httpx
import os
from pathlib import Path
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table
from rich.rule import Rule

load_dotenv(Path(__file__).parent / ".env")

DB_PATH = Path(__file__).parent / "data" / "jobs.db"
NETLIFY_URL = os.getenv("NETLIFY_SITE_URL", "https://precious-cupcake-3792b5.netlify.app")

console = Console()


async def local_stats():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Total
        async with db.execute("SELECT COUNT(*) as n FROM jobs") as c:
            total = (await c.fetchone())["n"]

        # By status
        async with db.execute(
            "SELECT status, COUNT(*) as n FROM jobs GROUP BY status ORDER BY n DESC"
        ) as c:
            by_status = await c.fetchall()

        # By source
        async with db.execute(
            "SELECT source_keyword, COUNT(*) as n FROM jobs GROUP BY source_keyword ORDER BY n DESC LIMIT 10"
        ) as c:
            by_source = await c.fetchall()

        # Recent
        async with db.execute(
            "SELECT title, company, status, score FROM jobs ORDER BY rowid DESC LIMIT 10"
        ) as c:
            recent = await c.fetchall()

    return total, by_status, by_source, recent


async def netlify_stats():
    statuses = ["pending_review", "approved", "rejected", "applied", "discovered", "new"]
    counts = {}

    async with httpx.AsyncClient(timeout=15) as client:
        # Get all without filter first
        resp = await client.get(f"{NETLIFY_URL}/.netlify/functions/opportunities")
        if resp.status_code == 200:
            data = resp.json()
            counts["total_visible"] = data.get("count", 0)

        # Try each status
        for status in statuses:
            try:
                resp = await client.get(
                    f"{NETLIFY_URL}/.netlify/functions/opportunities",
                    params={"status": status}
                )
                if resp.status_code == 200:
                    n = resp.json().get("count", 0)
                    if n > 0:
                        counts[status] = n
            except Exception:
                pass

    return counts


async def main():
    console.print(Rule("[bold cyan]Job Agent Status Dashboard[/bold cyan]"))

    # Local DB
    console.print("\n[bold]Local Database[/bold]")
    total, by_status, by_source, recent = await local_stats()

    t1 = Table(show_header=True, header_style="bold")
    t1.add_column("Status")
    t1.add_column("Count", justify="right")
    for row in by_status:
        t1.add_row(row["status"] or "—", str(row["n"]))
    t1.add_row("[bold]TOTAL[/bold]", f"[bold]{total}[/bold]")
    console.print(t1)

    console.print("\n[bold]Top Sources (local)[/bold]")
    t2 = Table(show_header=True, header_style="bold")
    t2.add_column("Keyword")
    t2.add_column("Jobs", justify="right")
    for row in by_source:
        t2.add_row(row["source_keyword"] or "—", str(row["n"]))
    console.print(t2)

    # Netlify
    console.print("\n[bold]Netlify Tracker[/bold]")
    try:
        netlify = await netlify_stats()
        t3 = Table(show_header=True, header_style="bold")
        t3.add_column("Status")
        t3.add_column("Count", justify="right")
        for k, v in netlify.items():
            t3.add_row(k, str(v))
        console.print(t3)
    except Exception as e:
        console.print(f"[red]Netlify unreachable: {e}[/red]")

    # Recent
    console.print("\n[bold]Last 10 Jobs Added[/bold]")
    t4 = Table(show_header=True, header_style="bold")
    t4.add_column("Title")
    t4.add_column("Company")
    t4.add_column("Status")
    t4.add_column("Score", justify="right")
    for row in recent:
        score = str(row["score"]) if row["score"] else "—"
        t4.add_row(row["title"][:45], row["company"][:25], row["status"] or "—", score)
    console.print(t4)


asyncio.run(main())

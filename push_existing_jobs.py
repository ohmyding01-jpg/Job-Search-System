"""
One-off script: push all jobs already in local SQLite DB to the Netlify tracker.
Run once: python push_existing_jobs.py
"""

import asyncio
import aiosqlite
import httpx
from pathlib import Path
from dotenv import load_dotenv
import os

load_dotenv(Path(__file__).parent / ".env")

DB_PATH = Path(__file__).parent / "data" / "jobs.db"
NETLIFY_URL = os.getenv("NETLIFY_SITE_URL", "https://precious-cupcake-3792b5.netlify.app")
BATCH_SIZE = 25


async def main():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT job_id, title, company, location, description, apply_url FROM jobs"
        ) as cursor:
            rows = await cursor.fetchall()

    print(f"Found {len(rows)} jobs in local DB")

    jobs = [
        {
            "title": r["title"],
            "company": r["company"],
            "location": r["location"] or "",
            "url": r["apply_url"] or "",
            "description": (r["description"] or "")[:4000],
        }
        for r in rows
    ]

    total_new = 0
    total_deduped = 0

    async with httpx.AsyncClient(timeout=30) as client:
        for i in range(0, len(jobs), BATCH_SIZE):
            batch = jobs[i : i + BATCH_SIZE]
            payload = {
                "source": "linkedin-agent-backfill",
                "sourceType": "manual",
                "jobs": batch,
            }
            resp = await client.post(
                f"{NETLIFY_URL}/.netlify/functions/intake",
                json=payload,
            )
            data = resp.json()
            summary = data.get("summary", {})
            new = summary.get("new", 0)
            deduped = summary.get("deduped", 0)
            total_new += new
            total_deduped += deduped
            print(f"  Batch {i // BATCH_SIZE + 1}: {new} new, {deduped} already existed")

    print(f"\nDone — {total_new} added to Netlify, {total_deduped} already there")


asyncio.run(main())

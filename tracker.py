"""
SQLite job tracker — single source of truth for all discovered, scored,
and applied jobs. Thread-safe via aiosqlite for async use.
"""

import asyncio
import aiosqlite
import json
import hashlib
from datetime import datetime, date
import os
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

DB_PATH = Path(os.getenv("JOB_AGENT_DB_PATH", "data/jobs.db"))


@dataclass
class Job:
    job_id: str          # LinkedIn job ID
    title: str
    company: str
    location: str
    description: str
    apply_url: str
    easy_apply: bool
    posted_at: Optional[str]
    score: int = 0
    score_breakdown: dict = field(default_factory=dict)
    score_reasoning: str = ""
    status: str = "discovered"   # discovered | scored | pack_generated | applied | skipped | failed
    resume_path: Optional[str] = None
    cover_letter_path: Optional[str] = None
    applied_at: Optional[str] = None
    source_keyword: str = ""
    source_platform: str = "linkedin"
    discovered_at: str = field(default_factory=lambda: datetime.now().isoformat())
    is_auto_applied: bool = False
    notes: str = ""


async def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS jobs (
                job_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                company TEXT NOT NULL,
                location TEXT,
                description TEXT,
                apply_url TEXT,
                easy_apply INTEGER DEFAULT 0,
                posted_at TEXT,
                score INTEGER DEFAULT 0,
                score_breakdown TEXT DEFAULT '{}',
                score_reasoning TEXT DEFAULT '',
                status TEXT DEFAULT 'discovered',
                resume_path TEXT,
                cover_letter_path TEXT,
                applied_at TEXT,
                source_keyword TEXT DEFAULT '',
                source_platform TEXT DEFAULT 'linkedin',
                discovered_at TEXT,
                is_auto_applied INTEGER DEFAULT 0,
                notes TEXT DEFAULT ''
            )
        """)
        try:
            await db.execute("ALTER TABLE jobs ADD COLUMN source_platform TEXT DEFAULT 'linkedin'")
        except Exception:
            pass
        await db.execute("""
            CREATE TABLE IF NOT EXISTS run_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_at TEXT,
                jobs_discovered INTEGER DEFAULT 0,
                jobs_scored INTEGER DEFAULT 0,
                packs_generated INTEGER DEFAULT 0,
                applications_sent INTEGER DEFAULT 0,
                errors TEXT DEFAULT ''
            )
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_status ON jobs(status);
        """)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_score ON jobs(score);
        """)
        await db.commit()


async def job_exists(job_id: str) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)) as cur:
            return await cur.fetchone() is not None


async def upsert_job(job: Job):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO jobs (
                job_id, title, company, location, description, apply_url,
                easy_apply, posted_at, score, score_breakdown, score_reasoning,
                status, resume_path, cover_letter_path, applied_at,
                source_keyword, source_platform, discovered_at, is_auto_applied, notes
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(job_id) DO UPDATE SET
                score=excluded.score,
                score_breakdown=excluded.score_breakdown,
                score_reasoning=excluded.score_reasoning,
                status=excluded.status,
                resume_path=excluded.resume_path,
                cover_letter_path=excluded.cover_letter_path,
                applied_at=excluded.applied_at,
                source_platform=excluded.source_platform,
                is_auto_applied=excluded.is_auto_applied,
                notes=excluded.notes
        """, (
            job.job_id, job.title, job.company, job.location,
            job.description, job.apply_url, int(job.easy_apply),
            job.posted_at, job.score,
            json.dumps(job.score_breakdown), job.score_reasoning,
            job.status, job.resume_path, job.cover_letter_path,
            job.applied_at, job.source_keyword, job.source_platform, job.discovered_at,
            int(job.is_auto_applied), job.notes
        ))
        await db.commit()


async def update_job_status(job_id: str, status: str, **kwargs):
    """Update status and any extra fields by keyword.

    When status is 'failed', appends a fail marker to notes and promotes to
    'give_up' after 3 failures so the job is never retried again.
    """
    if status == "failed":
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute(
                "SELECT notes FROM jobs WHERE job_id=?", (job_id,)
            ) as cur:
                row = await cur.fetchone()
            existing_notes = (row[0] or "") if row else ""
        fail_count = existing_notes.count("FAIL|") + 1
        new_notes = f"{existing_notes} FAIL|{fail_count}".strip()
        if fail_count >= 3:
            status = "give_up"
        kwargs["notes"] = new_notes

    fields = {"status": status, **kwargs}
    set_clause = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [job_id]
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            f"UPDATE jobs SET {set_clause} WHERE job_id=?", values
        )
        await db.commit()


async def get_jobs_by_status(status: str) -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM jobs WHERE status=? ORDER BY score DESC", (status,)
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_applications_today() -> int:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM jobs WHERE applied_at LIKE ? AND status='applied'",
            (f"{today}%",)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def get_packs_generated_today() -> int:
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT COALESCE(SUM(packs_generated), 0) FROM run_log WHERE run_at LIKE ?",
            (f"{today}%",)
        ) as cur:
            row = await cur.fetchone()
    return row[0] if row else 0


async def log_run(jobs_discovered: int, jobs_scored: int,
                  packs_generated: int, applications_sent: int, errors: str = ""):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO run_log (run_at, jobs_discovered, jobs_scored, packs_generated, applications_sent, errors)
            VALUES (?,?,?,?,?,?)
        """, (datetime.now().isoformat(), jobs_discovered, jobs_scored,
              packs_generated, applications_sent, errors))
        await db.commit()


async def get_stats() -> dict:
    async with aiosqlite.connect(DB_PATH) as db:
        stats = {}
        for status in ["discovered", "scored", "pack_generated", "applied", "skipped", "failed"]:
            async with db.execute(
                "SELECT COUNT(*) FROM jobs WHERE status=?", (status,)
            ) as cur:
                row = await cur.fetchone()
                stats[status] = row[0] if row else 0
        async with db.execute("SELECT COUNT(*) FROM jobs") as cur:
            row = await cur.fetchone()
            stats["total"] = row[0] if row else 0
        async with db.execute(
            "SELECT AVG(score) FROM jobs WHERE score > 0"
        ) as cur:
            row = await cur.fetchone()
            stats["avg_score"] = round(row[0] or 0, 1)
    return stats

"""
Multi-Board Job Agent — Main Orchestrator
Scans continuously and applies on a separate schedule.
"""

import asyncio
import os
import sys
import yaml
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urlunparse
from dotenv import load_dotenv
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from rich.console import Console
from rich.rule import Rule

# --- Load env first ---
load_dotenv(Path(__file__).parent / ".env")

from tracker import (
    Job, init_db, job_exists, upsert_job, update_job_status,
    get_jobs_by_status, get_applications_today, get_packs_generated_today,
    log_run, get_stats
)
from linkedin.auth import load_or_create_session, save_cookies, validate_and_refresh_sessions
from linkedin.scanner import scan_jobs
from boards.dispatcher import auto_apply_job
from ai.scorer import batch_score_jobs, score_job
from ai.provider import has_required_api_key, missing_api_key_message, provider_name
from ai.resume_writer import generate_tailored_resume_content
from ai.cover_letter_writer import generate_cover_letter
from ai.cover_letter_writer import _fallback_cover_letter
from documents.generator import generate_apply_pack
from notifier import log_run_summary, notify_application, notify_pack_ready
from netlify_sync import (
    push_jobs_to_netlify,
    sync_apply_pack_to_netlify,
    fetch_approved_opportunities,
    fetch_applications_today as fetch_netlify_applications_today,
    mark_applied_in_netlify,
    mark_job_applied_in_netlify,
    mark_manual_apply_needed_in_netlify,
    mark_job_manual_apply_needed_in_netlify,
)
from scrapers.jobspy_scanner import scan_jobspy
from scrapers.usajobs import scan_usajobs
from scrapers.dice_scanner import scan_dice
from scrapers.careerbuilder_scanner import scan_careerbuilder

console = Console()

_config_cache: dict | None = None
_profile_cache: dict | None = None


def load_config() -> dict:
    global _config_cache
    cfg_path = Path(
        os.getenv("JOB_AGENT_CONFIG_PATH", str(Path(__file__).parent / "config.yaml"))
    )
    try:
        with open(cfg_path) as f:
            result = yaml.safe_load(f)
            _config_cache = result
            return result
    except FileNotFoundError:
        if _config_cache is not None:
            return _config_cache
        raise


def load_profile() -> dict:
    global _profile_cache
    profile_path = Path(
        os.getenv("JOB_AGENT_PROFILE_PATH", str(Path(__file__).parent / "resume_profile.yaml"))
    )
    try:
        with open(profile_path) as f:
            result = yaml.safe_load(f)
            _profile_cache = result
            return result
    except FileNotFoundError:
        if _profile_cache is not None:
            return _profile_cache
        raise


def _in_quiet_hours(config: dict) -> bool:
    if not config.get("scheduler", {}).get("quiet_hours_enabled", True):
        return False
    start = config["scheduler"]["quiet_hours_start"]
    end = config["scheduler"]["quiet_hours_end"]
    now_hour = datetime.now().hour
    if start > end:
        return now_hour >= start or now_hour < end
    return start <= now_hour < end


def _netlify_enabled(config: dict) -> bool:
    """Per-candidate flag — Stephen disables this; Samiha keeps it on."""
    return bool((config.get("netlify") or {}).get("enabled", True))


def _location_ok_for_auto_apply(location: str) -> bool:
    """
    Keep unattended submit broad enough for US nationwide SQL/DBA searches while
    still avoiding clearly out-of-scope international postings.
    """
    normalized = " ".join((location or "").lower().replace("-", " ").split())
    if not normalized:
        return False

    if "remote" in normalized:
        return True

    us_terms = (
        "united states", "usa", "u.s.",
        "alabama", " hoover ", " birmingham ",
    )
    return any(term in f" {normalized} " for term in us_terms)

def _default_resume_path(config: dict) -> Path:
    """Baseline resume used when budget guard blocks tailored document generation."""
    configured = Path(config.get("documents", {}).get("output_dir", "output/apply_packs")) / "resume_template.docx"
    if configured.exists():
        return configured

    profile_path = Path(
        os.getenv("JOB_AGENT_PROFILE_PATH", str(Path(__file__).parent / "resume_profile.yaml"))
    )
    if profile_path.exists():
        try:
            profile = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
            variants = profile.get("resume_variants", {}) if isinstance(profile, dict) else {}
            preferred_keys = ["default", "general", "sql_dba", "database_engineer"]
            for key in preferred_keys:
                candidate_file = variants.get(key)
                if not candidate_file:
                    continue
                candidate_path = (profile_path.parent.parent / str(candidate_file)).resolve()
                if candidate_path.exists() and candidate_path.is_file():
                    return candidate_path
            for candidate_file in variants.values():
                candidate_path = (profile_path.parent.parent / str(candidate_file)).resolve()
                if candidate_path.exists() and candidate_path.is_file():
                    return candidate_path
        except Exception:
            pass

    candidates = [
        Path(__file__).parent.parent / "MuliokelaStephen(Resume)_SQL Expert_ DBA.docx",
        Path(__file__).parent.parent / "resume.docx",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return configured


def _local_resume_content(profile: dict, scoring: dict) -> dict:
    """Build minimal resume content without any model calls."""
    skills = []
    for _, items in (profile.get("skills", {}) or {}).items():
        if isinstance(items, list):
            skills.extend(items)
    return {
        "summary": (
            "Experienced database and IT professional with strong SQL, cloud, and delivery "
            "experience. Focused on reliable execution, stakeholder communication, and "
            "production operations."
        ),
        "core_skills": skills[:20],
        "key_achievements": (scoring or {}).get("strengths", [])[:3],
    }

def _normalise_apply_url(value: str) -> str:
    try:
        parsed = urlparse(value or "")
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", "", "")).lower()
    except Exception:
        return " ".join(str(value or "").lower().split())


def _platform_host(platform: str) -> str:
    if platform == "linkedin":
        return "linkedin.com"
    if platform == "dice":
        return "dice.com"
    if platform == "careerbuilder":
        return "careerbuilder.com"
    return ""


async def _build_platform_pages(context, default_page):
    """Reuse existing domain tabs when possible; otherwise create a dedicated page per board."""
    pages = {"linkedin": default_page}
    for platform in ("dice", "careerbuilder"):
        host = _platform_host(platform)
        existing = None
        for candidate in context.pages:
            try:
                if host in (candidate.url or "").lower():
                    existing = candidate
                    break
            except Exception:
                continue
        pages[platform] = existing or await context.new_page()
    return pages


async def _run_scan_cycle() -> None:
    """Continuously scan, score, and generate packs without submitting applications."""
    config = load_config()
    profile = load_profile()

    if _in_quiet_hours(config):
        console.print("[dim]Quiet hours — skipping scan[/dim]")
        return

    console.print(Rule(f"[bold cyan]Scan Cycle — {datetime.now().strftime('%Y-%m-%d %H:%M')}[/bold cyan]"))

    ai_enabled = config.get("ai", {}).get("enabled", True)
    budget_config = config.get("budget", {})
    min_score = config["scoring"]["minimum_score"]
    generate_threshold = config["scoring"]["generate_pack_threshold"]

    errors: list[str] = []
    new_jobs_count = 0
    packs_generated = 0
    scored_results: list = []

    browser, context, page = await load_or_create_session(config)
    platform_pages = await _build_platform_pages(context, page)

    try:
        console.print("\n[bold]Phase 1: Scanning configured boards[/bold]")
        existing_ids = set()

        for status in ["discovered", "scored", "pack_generated", "applied", "skipped", "failed"]:
            jobs_in_status = await get_jobs_by_status(status)
            for j in jobs_in_status:
                existing_ids.add(j["job_id"])

        # Scan boards sequentially to avoid browser contention.
        # Apply cycle remains parallel — these scans share one browser context.
        per_board: dict[str, list] = {}

        try:
            per_board["linkedin"] = await scan_jobs(platform_pages["linkedin"], config, existing_ids)
        except Exception as e:
            console.print(f"[red]LinkedIn scan error: {e}[/red]"); per_board["linkedin"] = []

        if config.get("dice", {}).get("enabled", True):
            try:
                per_board["dice"] = await scan_dice(platform_pages["dice"], config, existing_ids)
            except Exception as e:
                console.print(f"[red]Dice scan error: {e}[/red]"); per_board["dice"] = []

        if config.get("careerbuilder", {}).get("enabled", True):
            try:
                per_board["careerbuilder"] = await scan_careerbuilder(platform_pages["careerbuilder"], config, existing_ids)
            except Exception as e:
                console.print(f"[red]CareerBuilder scan error: {e}[/red]"); per_board["careerbuilder"] = []

        if config.get("jobspy", {}).get("enabled", True):
            try:
                per_board["jobspy"] = await scan_jobspy(config, existing_ids)
            except Exception as e:
                per_board["jobspy"] = []

        if config.get("usajobs", {}).get("enabled", True):
            try:
                per_board["usajobs"] = await scan_usajobs(config, existing_ids)
            except Exception as e:
                per_board["usajobs"] = []

        linkedin_jobs = per_board.get("linkedin", [])
        dice_jobs = per_board.get("dice", [])
        careerbuilder_jobs = per_board.get("careerbuilder", [])
        jobspy_jobs = per_board.get("jobspy", [])
        usajobs_jobs = per_board.get("usajobs", [])

        new_jobs = linkedin_jobs + jobspy_jobs + usajobs_jobs + dice_jobs + careerbuilder_jobs
        new_jobs_count = len(new_jobs)

        if new_jobs:
            sources = []
            if linkedin_jobs: sources.append(f"LinkedIn: {len(linkedin_jobs)}")
            if jobspy_jobs: sources.append(f"Indeed/ZipRecruiter/Glassdoor: {len(jobspy_jobs)}")
            if usajobs_jobs: sources.append(f"USAJobs: {len(usajobs_jobs)}")
            if dice_jobs: sources.append(f"Dice: {len(dice_jobs)}")
            if careerbuilder_jobs: sources.append(f"CareerBuilder: {len(careerbuilder_jobs)}")
            console.print(f"  [bold]Total new: {new_jobs_count}[/bold] ({', '.join(sources)})")

        for job in new_jobs:
            await upsert_job(job)

        if new_jobs and _netlify_enabled(config):
            console.print(f"\n  Syncing {len(new_jobs)} jobs to Netlify tracker...")
            await push_jobs_to_netlify(new_jobs)

        if not new_jobs:
            console.print("[dim]No new jobs found this scan[/dim]")
            await log_run(0, 0, 0, 0)
            return

        discovered_rows = await get_jobs_by_status("discovered")
        new_job_ids = {j.job_id for j in new_jobs}
        retry_jobs = [
            Job(
                job_id=row["job_id"],
                title=row.get("title", ""),
                company=row.get("company", ""),
                location=row.get("location", ""),
                description=row.get("description", ""),
                apply_url=row.get("apply_url", ""),
                easy_apply=bool(row.get("easy_apply")),
                posted_at=row.get("posted_at"),
                status="discovered",
                source_keyword=row.get("source_keyword", ""),
                source_platform=row.get("source_platform", "linkedin"),
            )
            for row in discovered_rows
            if row.get("job_id") not in new_job_ids and int(row.get("score") or 0) == 0
        ]
        if retry_jobs:
            console.print(f"[dim]Retrying scoring for {len(retry_jobs)} previously unscored job(s)[/dim]")

        scoring_mode = provider_name(config.get("ai", {})).title() if ai_enabled else "local budget-safe"
        console.print(f"\n[bold]Phase 2: Scoring {len(new_jobs)} jobs with {scoring_mode} scoring[/bold]")

        scored_results = await batch_score_jobs(new_jobs + retry_jobs, profile, config)

        for job, scoring in scored_results:
            if scoring.get("scoring_failed"):
                continue
            await upsert_job(job)
            if job.score < min_score:
                await update_job_status(job.job_id, "skipped", notes="Below minimum score threshold")
                continue

        max_document_jobs_per_run = int(budget_config.get("max_document_jobs_per_run", 999))
        max_document_jobs_per_day = int(budget_config.get("max_document_jobs_per_day", 999))
        documents_generated_today = await get_packs_generated_today()
        document_slots_today = max(0, max_document_jobs_per_day - documents_generated_today)
        max_document_jobs_this_cycle = min(max_document_jobs_per_run, document_slots_today)
        documents_enabled = max_document_jobs_this_cycle > 0
        console.print(f"\n[bold]Phase 3: Generating apply packs[/bold]")

        if not documents_enabled:
            console.print("[yellow]Document generation disabled by budget guard this cycle[/yellow]")

        documents_generated_this_run = 0
        for job, scoring in scored_results:
            if job.score < generate_threshold:
                continue
            if not documents_enabled:
                continue
            if documents_generated_this_run >= max_document_jobs_this_cycle:
                console.print(
                    f"[yellow]Document generation cap reached ({max_document_jobs_this_cycle} available this cycle) — skipping the rest[/yellow]"
                )
                break

            console.print(f"  Generating pack for: [cyan]{job.title} @ {job.company}[/cyan] (Score: {job.score})")

            try:
                use_ai_tailoring = (
                    ai_enabled
                    and job.score >= config["scoring"]["auto_apply_threshold"]
                    and job.easy_apply
                    and _location_ok_for_auto_apply(job.location)
                )

                if use_ai_tailoring:
                    resume_content = await generate_tailored_resume_content(
                        profile=profile,
                        job_title=job.title,
                        company=job.company,
                        location=job.location,
                        scoring_result=scoring,
                        ai_config=config["ai"],
                    )
                    cl_text = await generate_cover_letter(
                        profile=profile,
                        job_title=job.title,
                        company=job.company,
                        job_location=job.location,
                        scoring_result=scoring,
                        ai_config=config["ai"],
                    )
                else:
                    resume_content = _local_resume_content(profile, scoring)
                    cl_text = _fallback_cover_letter(profile, job.title, job.company)

                resume_path, cl_path = generate_apply_pack(
                    profile=profile,
                    job_title=job.title,
                    company=job.company,
                    job_location=job.location,
                    job_id=job.job_id,
                    scoring_result=scoring,
                    resume_content=resume_content,
                    cover_letter_text=cl_text,
                    config=config,
                )

                job.resume_path = str(resume_path)
                job.cover_letter_path = str(cl_path)
                job.status = "pack_generated"
                await upsert_job(job)
                if _netlify_enabled(config):
                    await sync_apply_pack_to_netlify(
                        job=job,
                        scoring=scoring,
                        resume_content=resume_content,
                        cover_letter_text=cl_text,
                        resume_path=str(resume_path),
                        cover_letter_path=str(cl_path),
                    )
                packs_generated += 1
                documents_generated_this_run += 1

                if not use_ai_tailoring:
                    notify_pack_ready(
                        job.title,
                        job.company,
                        job.score,
                        str(resume_path),
                        str(cl_path),
                        source_platform=job.source_platform,
                    )

            except Exception as e:
                err = f"Pack generation failed for {job.job_id}: {e}"
                console.print(f"  [red]{err}[/red]")
                try:
                    import traceback
                    console.print(f"  [dim]{traceback.format_exc()}[/dim]")
                except Exception:
                    pass
                errors.append(err)

        # Save refreshed session cookies after scanning.
        await save_cookies(context, config)

    except Exception as e:
        err = f"Scan cycle error: {e}"
        console.print(f"[red]{err}[/red]")
        errors.append(err)
    finally:
        try:
            await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass

    await log_run(new_jobs_count, len(scored_results), packs_generated, 0, "; ".join(errors))
    stats = await get_stats()
    log_run_summary(stats, new_jobs_count, 0, packs_generated, errors)


async def _run_apply_cycle() -> None:
    """Apply queued jobs every 15 minutes without rescanning."""
    config = load_config()
    profile = load_profile()

    if _in_quiet_hours(config):
        console.print("[dim]Quiet hours — skipping apply[/dim]")
        return

    applications_enabled = config.get("applications", {}).get("enabled", True)
    if not applications_enabled:
        console.print("[bold yellow]Review-only mode: application submission is disabled.[/bold yellow]")
        await log_run(0, 0, 0, 0)
        return

    console.print(Rule(f"[bold magenta]Apply Cycle — {datetime.now().strftime('%Y-%m-%d %H:%M')}[/bold magenta]"))

    errors: list[str] = []
    failed_apply_urls: set[str] = set()
    applications_sent = 0

    local_apps_today = await get_applications_today()
    netlify_apps_today = await fetch_netlify_applications_today() if _netlify_enabled(config) else None
    apps_today = max(local_apps_today, netlify_apps_today or 0)
    max_per_day = config["scoring"]["max_applications_per_day"]
    max_per_run = max(3, int(config["scoring"]["max_applications_per_run"]))
    if netlify_apps_today is not None and netlify_apps_today != local_apps_today:
        console.print(
            f"[dim]Daily application count: local={local_apps_today}, tracker={netlify_apps_today}; using {apps_today}/{max_per_day}[/dim]"
        )
    if apps_today >= max_per_day:
        console.print(f"[yellow]Daily limit reached ({apps_today}/{max_per_day}) — skipping applications[/yellow]")
        await log_run(0, 0, 0, 0)
        return

    browser, context, page = await load_or_create_session(config)
    platform_pages = await _build_platform_pages(context, page)

    # Only check sessions for boards that are actually enabled in config.
    dice_on = config.get("dice", {}).get("enabled", True)
    cb_on = config.get("careerbuilder", {}).get("enabled", True)
    if dice_on or cb_on:
        console.print("[dim]Checking board sessions…[/dim]")
        session_status = await validate_and_refresh_sessions(context)
    else:
        session_status = {"dice": False, "careerbuilder": False}

    try:
        apps_remaining = min(max_per_run, max_per_day - apps_today)
        apply_queue: list[tuple[Job, dict, Path, Path]] = []

        # First pass: attempt one job per board (LinkedIn, Dice, CareerBuilder).
        for platform in ("linkedin", "dice", "careerbuilder"):
            if len(apply_queue) >= apps_remaining:
                break
            apply_queue = await _add_existing_packs_to_apply_queue(
                apply_queue,
                auto_apply_threshold=config["scoring"]["auto_apply_threshold"],
                max_per_run=min(apps_remaining, len(apply_queue) + 1),
                config=config,
                platform_filter=platform,
            )

        # Strict fallback: if any platform still has no queued job, force one
        # attempt from historical jobs regardless of score.
        queued_platforms = {str(job.source_platform or "").lower() for job, *_ in apply_queue}
        for platform in ("linkedin", "dice", "careerbuilder"):
            if len(apply_queue) >= apps_remaining:
                break
            if platform in queued_platforms:
                continue
            before = len(apply_queue)
            apply_queue = await _add_existing_packs_to_apply_queue(
                apply_queue,
                auto_apply_threshold=config["scoring"]["auto_apply_threshold"],
                max_per_run=min(apps_remaining, len(apply_queue) + 1),
                config=config,
                platform_filter=platform,
                statuses=["pack_generated", "scored", "discovered", "skipped"],
                force_any_score=True,
            )
            if len(apply_queue) == before:
                console.print(f"[yellow]No available {platform.title()} job to attempt this cycle[/yellow]")

        # Second pass: fill any remaining slots with best available jobs.
        if len(apply_queue) < apps_remaining:
            apply_queue = await _add_existing_packs_to_apply_queue(
                apply_queue,
                auto_apply_threshold=config["scoring"]["auto_apply_threshold"],
                max_per_run=apps_remaining,
                config=config,
            )

        if apply_queue and apps_remaining > 0:
            console.print(f"\n[bold]Auto-applying {len(apply_queue)} ready pack(s)[/bold]")
            applications_sent = await _run_apply_queue(
                page=page,
                platform_pages=platform_pages,
                profile=profile,
                config=config,
                apply_queue=apply_queue,
                apps_remaining=apps_remaining,
                errors=errors,
                failed_apply_urls=failed_apply_urls,
            )
        else:
            console.print("[dim]No existing ready Easy Apply packs[/dim]")

        applications_sent += await _apply_tracker_approved_jobs(
            page=page,
            platform_pages=platform_pages,
            profile=profile,
            config=config,
            apps_remaining=apps_remaining - applications_sent,
            errors=errors,
            failed_apply_urls=failed_apply_urls,
        )

        await save_cookies(context, config)

    except Exception as e:
        err = f"Apply cycle error: {e}"
        console.print(f"[red]{err}[/red]")
        errors.append(err)
    finally:
        try:
            await context.close()
        except Exception:
            pass
        try:
            await browser.close()
        except Exception:
            pass

    await log_run(0, 0, 0, applications_sent, "; ".join(errors))
    stats = await get_stats()
    log_run_summary(stats, 0, applications_sent, 0, errors)


async def _add_existing_packs_to_apply_queue(
    apply_queue: list[tuple[Job, dict, Path, Path]],
    auto_apply_threshold: int,
    max_per_run: int,
    config: dict,
    platform_filter: str | None = None,
    statuses: list[str] | None = None,
    force_any_score: bool = False,
) -> list[tuple[Job, dict, Path, Path]]:
    """Include high-score Easy Apply jobs, using a tailored pack or baseline resume."""
    queued_ids = {job.job_id for job, *_ in apply_queue}
    rows = []
    candidate_statuses = statuses or ["pack_generated", "scored"]
    for status in candidate_statuses:
        rows.extend(await get_jobs_by_status(status))
    rows.sort(key=lambda row: int(row.get("score") or 0), reverse=True)
    default_resume = _default_resume_path(config)

    for row in rows:
        if len(apply_queue) >= max_per_run:
            break
        if row.get("job_id") in queued_ids:
            continue
        source_platform = str(row.get("source_platform") or "").lower()
        if source_platform not in {"linkedin", "dice", "careerbuilder"}:
            continue
        if platform_filter and source_platform != platform_filter:
            continue
        if not force_any_score and int(row.get("score") or 0) < auto_apply_threshold:
            continue
        # Allow external ATS jobs (easy_apply=False) — dispatcher routes them correctly.
        # Only skip LinkedIn jobs that have NO apply URL at all.
        apply_url = row.get("apply_url") or ""
        if source_platform == "linkedin" and not row.get("easy_apply") and not apply_url:
            continue

        resume_path = Path(row.get("resume_path") or "")
        if not row.get("resume_path") or not resume_path.exists():
            resume_path = default_resume
        if not resume_path.exists():
            console.print(
                f"  [yellow]Auto-apply skipped missing resume:[/yellow] "
                f"{row.get('title')} @ {row.get('company')}"
            )
            continue

        cl_path = Path(row.get("cover_letter_path") or "")
        job = Job(
            job_id=row.get("job_id", ""),
            title=row.get("title", ""),
            company=row.get("company", ""),
            location=row.get("location", ""),
            description=row.get("description", ""),
            apply_url=row.get("apply_url", ""),
            easy_apply=bool(row.get("easy_apply")),
            posted_at=row.get("posted_at"),
            score=int(row.get("score") or 0),
            status=row.get("status", "pack_generated"),
            resume_path=row.get("resume_path"),
            cover_letter_path=row.get("cover_letter_path"),
            source_keyword=row.get("source_keyword", ""),
            source_platform=row.get("source_platform", "linkedin"),
        )
        apply_queue.append((job, {"from_existing_pack": bool(row.get("resume_path"))}, resume_path, cl_path))
        queued_ids.add(job.job_id)

    return apply_queue


async def _ensure_tailored_pack(
    job: Job,
    profile: dict,
    config: dict,
    scoring: dict | None = None,
) -> tuple[Path, Path]:
    """Generate a tailored resume + cover letter for this specific job now.

    Always runs the AI tailoring path (regardless of score) and overwrites any
    stale pack so each application uses fresh, job-specific content.
    """
    ai_config = config.get("ai", {})
    if not scoring or scoring.get("scoring_failed"):
        try:
            scoring = await score_job(job, profile, ai_config)
        except Exception:
            scoring = {"keywords_to_emphasise": [], "key_requirements": [], "gaps": [], "strengths": []}

    try:
        resume_content = await generate_tailored_resume_content(
            profile=profile,
            job_title=job.title,
            company=job.company,
            location=job.location,
            scoring_result=scoring,
            ai_config=ai_config,
        )
    except Exception as e:
        console.print(f"[yellow]Resume tailoring fell back for {job.title}: {e}[/yellow]")
        resume_content = _local_resume_content(profile, scoring)

    try:
        cl_text = await generate_cover_letter(
            profile=profile,
            job_title=job.title,
            company=job.company,
            job_location=job.location,
            scoring_result=scoring,
            ai_config=ai_config,
        )
    except Exception as e:
        console.print(f"[yellow]Cover letter fell back for {job.title}: {e}[/yellow]")
        cl_text = _fallback_cover_letter(profile, job.title, job.company)

    resume_path, cl_path = generate_apply_pack(
        profile=profile,
        job_title=job.title,
        company=job.company,
        job_location=job.location,
        job_id=job.job_id,
        scoring_result=scoring or {},
        resume_content=resume_content,
        cover_letter_text=cl_text,
        config=config,
    )
    job.resume_path = str(resume_path)
    job.cover_letter_path = str(cl_path)
    await upsert_job(job)
    return resume_path, cl_path


async def _run_apply_queue(
    page,
    platform_pages: dict,
    profile: dict,
    config: dict,
    apply_queue: list[tuple[Job, dict, Path, Path]],
    apps_remaining: int,
    errors: list[str],
    failed_apply_urls: set[str] | None = None,
) -> int:
    """Submit queued Easy Apply jobs IN PARALLEL across boards.

    Each board has its own page so concurrent submissions don't fight for the
    same browser tab. Within a single board, items still run sequentially to
    avoid two tabs racing on the same domain. A tailored resume + cover letter
    is generated for each job right before applying.
    """
    items = apply_queue[:apps_remaining]
    if not items:
        return 0

    # Group by platform so each board's queue runs on its dedicated page.
    by_platform: dict[str, list[tuple[Job, dict, Path, Path]]] = {}
    for entry in items:
        plat = str(entry[0].source_platform or "linkedin").lower()
        by_platform.setdefault(plat, []).append(entry)

    async def _run_one_board(platform: str, board_items: list[tuple[Job, dict, Path, Path]]) -> int:
        sent = 0
        apply_page = platform_pages.get(platform, page)
        for job, scoring, resume_path, cl_path in board_items:
            console.print(
                f"[cyan]→ {platform.upper()}[/cyan] tailoring + applying: {job.title} @ {job.company}"
            )
            # Always generate a fresh tailored pack for this exact job.
            try:
                resume_path, cl_path = await _ensure_tailored_pack(job, profile, config, scoring)
                console.print(f"  [dim]Tailored pack: {Path(resume_path).name}[/dim]")
            except Exception as e:
                console.print(f"  [yellow]Tailoring failed, using default resume: {e}[/yellow]")

            success = await auto_apply_job(
                page=apply_page,
                job=job,
                profile=profile,
                resume_path=resume_path,
                config=config,
            )
            if success:
                now = datetime.now().isoformat()
                await update_job_status(
                    job.job_id, "applied", applied_at=now, is_auto_applied=1
                )
                if _netlify_enabled(config):
                    await mark_job_applied_in_netlify(job, now)
                notify_application(
                    job.title,
                    job.company,
                    job.score,
                    str(resume_path),
                    source_platform=job.source_platform,
                    apply_url=job.apply_url,
                )
                sent += 1
            else:
                await update_job_status(
                    job.job_id, "failed", notes="Easy Apply did not complete"
                )
                if failed_apply_urls is not None:
                    failed_apply_urls.add(_normalise_apply_url(job.apply_url))
                if _netlify_enabled(config):
                    await mark_job_manual_apply_needed_in_netlify(
                        job, f"{platform.title()} auto-apply did not complete.",
                    )
                errors.append(f"Apply failed [{platform}]: {job.title} @ {job.company}")
        return sent

    # Apply with a small stagger between boards to reduce browser contention.
    # Full simultaneity was causing the browser to drop connections mid-navigation.
    BOARD_ORDER = ["dice", "careerbuilder", "linkedin"]
    ordered = sorted(by_platform.items(), key=lambda kv: BOARD_ORDER.index(kv[0]) if kv[0] in BOARD_ORDER else 99)

    console.print(
        f"[bold]Applying on {len(ordered)} board(s) (staggered): "
        f"{', '.join(p for p, _ in ordered)}[/bold]"
    )

    async def _staggered_board(idx: int, platform: str, board_items: list) -> int:
        await asyncio.sleep(idx * 4)  # 4s stagger between board starts
        return await _run_one_board(platform, board_items)

    counts = await asyncio.gather(
        *(_staggered_board(i, plat, lst) for i, (plat, lst) in enumerate(ordered)),
        return_exceptions=False,
    )
    return int(sum(counts))


async def _apply_tracker_approved_jobs(
    page,
    platform_pages: dict,
    profile: dict,
    config: dict,
    apps_remaining: int,
    errors: list[str],
    failed_apply_urls: set[str] | None = None,
) -> int:
    """Submit jobs approved in the Netlify tracker without making any AI calls."""
    if apps_remaining <= 0:
        return 0
    if not _netlify_enabled(config):
        return 0

    console.print(f"\n[bold]Phase 5: Applying jobs approved in tracker UI[/bold]")
    approved_opps = await fetch_approved_opportunities()
    applications_sent = 0
    attempts = 0
    max_attempts = max(apps_remaining * 5, apps_remaining)

    for opp in approved_opps:
        if applications_sent >= apps_remaining or attempts >= max_attempts:
            break

        opp_url = opp.get("application_url") or opp.get("url") or ""
        if not opp_url:
            console.print(f"  [yellow]Skipping '{opp.get('title')}' — no apply URL[/yellow]")
            await mark_manual_apply_needed_in_netlify(opp.get("id", ""), "No apply URL available for automatic submission.")
            continue
        platform = (opp.get("source_platform") or "").lower()
        if not platform:
            if "linkedin.com" in opp_url.lower():
                platform = "linkedin"
            elif "dice.com" in opp_url.lower():
                platform = "dice"
            elif "careerbuilder.com" in opp_url.lower():
                platform = "careerbuilder"
        if platform not in {"linkedin", "dice", "careerbuilder"}:
            console.print(f"  [yellow]Skipping '{opp.get('title')}' — unsupported auto-apply platform[/yellow]")
            await mark_manual_apply_needed_in_netlify(opp.get("id", ""), "Unsupported auto-apply platform; manual application needed.")
            continue
        if failed_apply_urls and _normalise_apply_url(opp_url) in failed_apply_urls:
            console.print(f"  [yellow]Skipping '{opp.get('title')}' — already failed Easy Apply this cycle[/yellow]")
            await mark_manual_apply_needed_in_netlify(
                opp.get("id", ""),
                "LinkedIn Easy Apply was attempted this cycle but did not progress; manual application needed.",
            )
            continue

        approved_job = Job(
            job_id=opp.get("id", ""),
            title=opp.get("title", ""),
            company=opp.get("company", ""),
            location=opp.get("location", ""),
            description=opp.get("description", ""),
            apply_url=opp_url,
            easy_apply=True,
            posted_at=opp.get("posted_at"),
            score=opp.get("fit_score", 0),
            source_platform=platform,
        )

        console.print(f"  Applying (tracker-approved): [cyan]{approved_job.title} @ {approved_job.company}[/cyan]")
        attempts += 1

        resume_path = _default_resume_path(config)

        success = await auto_apply_job(
            page=platform_pages.get(platform, page),
            job=approved_job,
            profile=profile,
            resume_path=resume_path,
            config=config,
        )

        if success:
            now = datetime.now().isoformat()
            await mark_applied_in_netlify(opp.get("id", ""), now)
            applications_sent += 1
            console.print(f"  [green]✓ Applied and marked in tracker[/green]")
            notify_application(
                approved_job.title,
                approved_job.company,
                int(approved_job.score or 0),
                str(resume_path),
                source_platform=approved_job.source_platform,
                apply_url=approved_job.apply_url,
            )
        else:
            console.print(f"  [red]✗ Apply failed for '{approved_job.title}'[/red]")
            await mark_manual_apply_needed_in_netlify(opp.get("id", ""), f"{platform.title()} auto-apply was not available or did not complete.")
            errors.append(f"Tracker-approved apply failed: {approved_job.title} @ {approved_job.company}")

    return applications_sent


async def main():
    """Entry point — initialise DB and start scheduler."""
    console.print(Rule("[bold green]LinkedIn Job Agent Starting[/bold green]"))

    # Validate env
    config = load_config()
    ai_enabled = config.get("ai", {}).get("enabled", True)
    if ai_enabled and not has_required_api_key(config.get("ai", {})):
        console.print(f"[red]{missing_api_key_message(config.get('ai', {}))}[/red]")
        sys.exit(1)

    # Init DB
    await init_db()
    console.print("[green]Database ready[/green]")

    scan_interval = int(config["scheduler"].get("scan_interval_minutes", 1))
    apply_interval = int(config["scheduler"].get("apply_interval_minutes", 15))

    # Run once immediately so the system starts working right away.
    console.print(f"[cyan]Running first scan now, then every {scan_interval} minutes...[/cyan]")
    try:
        await _run_scan_cycle()
    except Exception as e:
        console.print(f"[red]Initial scan cycle error (will retry on schedule): {e}[/red]")
    console.print(f"[cyan]Running first apply now, then every {apply_interval} minutes...[/cyan]")
    try:
        await _run_apply_cycle()
    except Exception as e:
        console.print(f"[red]Initial apply cycle error (will retry on schedule): {e}[/red]")

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_scan_cycle,
        "interval",
        minutes=scan_interval,
        id="job_scan",
        max_instances=1,
        coalesce=True,
    )
    scheduler.add_job(
        _run_apply_cycle,
        "interval",
        minutes=apply_interval,
        id="job_apply",
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()

    console.print(f"[green]Scheduler started — scanning every {scan_interval} minute(s) and applying every {apply_interval} minute(s)[/green]")
    console.print("[dim]Press Ctrl+C to stop[/dim]")

    try:
        while True:
            await asyncio.sleep(60)
    except (KeyboardInterrupt, SystemExit):
        console.print("\n[yellow]Shutting down...[/yellow]")
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
Tenant-safe runner for the job agent.

Usage:
  python3 run.py --candidate stephen
  python3 run.py --candidate stephen --once
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import yaml


def _build_runtime_config(candidate_dir: Path, root_dir: Path) -> tuple[Path, dict]:
    config_path = candidate_dir / "config.yaml"
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    (candidate_dir / "data").mkdir(parents=True, exist_ok=True)
    (candidate_dir / "session").mkdir(parents=True, exist_ok=True)
    (candidate_dir / "documents").mkdir(parents=True, exist_ok=True)

    cfg.setdefault("browser", {})["cookies_file"] = str((candidate_dir / "session" / "linkedin_cookies.json").resolve())
    cfg.setdefault("output", {})["db_path"] = str((candidate_dir / "data" / "jobs.db").resolve())
    cfg["output"]["resumes_dir"] = str((candidate_dir / "documents" / "resumes").resolve())
    cfg["output"]["cover_letters_dir"] = str((candidate_dir / "documents" / "cover_letters").resolve())

    runtime_path = root_dir / f".runtime.{candidate_dir.name}.config.yaml"
    runtime_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    return runtime_path, cfg


def _set_runtime_env(candidate_dir: Path, runtime_config: Path, cfg: dict) -> None:
    os.environ["JOB_AGENT_CONFIG_PATH"] = str(runtime_config.resolve())
    os.environ["JOB_AGENT_PROFILE_PATH"] = str((candidate_dir / "resume_profile.yaml").resolve())
    # Tell resume_sources where to find this candidate's resume files.
    os.environ["JOB_AGENT_RESUME_DIR"] = str(candidate_dir.resolve())
    os.environ["JOB_AGENT_DB_PATH"] = str((candidate_dir / "data" / "jobs.db").resolve())
    candidate_email = ((cfg.get("candidate", {}) if isinstance(cfg, dict) else {}).get("email", "") or "").strip()
    primary_notify = (os.getenv("NOTIFY_EMAIL", "") or "").strip()
    mirrored_notify = (os.getenv("NOTIFY_CC_EMAIL", "") or "").strip()

    if candidate_email and not primary_notify:
        os.environ["NOTIFY_EMAIL"] = candidate_email
    elif candidate_email and primary_notify and not mirrored_notify and candidate_email.lower() != primary_notify.lower():
        os.environ["NOTIFY_CC_EMAIL"] = candidate_email


async def run_for_candidate(candidate_name: str, once: bool) -> None:
    root = Path(__file__).parent
    candidate_dir = root / "candidates" / candidate_name
    if not candidate_dir.exists():
        available = sorted([d.name for d in (root / "candidates").iterdir() if d.is_dir()])
        print(f"Candidate directory not found: {candidate_dir}")
        print(f"Available candidates: {', '.join(available)}")
        raise SystemExit(1)

    runtime_config, runtime_cfg = _build_runtime_config(candidate_dir, root)
    _set_runtime_env(candidate_dir, runtime_config, runtime_cfg)

    print(f"Running candidate: {candidate_name}")
    print(f"Runtime config: {runtime_config}")

    try:
        if once:
            from main import init_db, run_cycle

            await init_db()
            await run_cycle()
        else:
            from main import main as orchestrator_main

            await orchestrator_main()
    finally:
        if runtime_config.exists():
            runtime_config.unlink()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run tenant-isolated job agent")
    parser.add_argument("--candidate", required=True, help="Candidate key under candidates/")
    parser.add_argument("--once", action="store_true", help="Run one cycle only")
    args = parser.parse_args()

    asyncio.run(run_for_candidate(args.candidate, args.once))

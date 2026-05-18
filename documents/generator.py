"""
Apply-pack document generator.
Builds per-job resume and cover-letter files into configured output folders.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path


def _safe_filename(value: str, max_len: int = 64) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in (value or "").strip())
    cleaned = "_".join(part for part in cleaned.split("_") if part)
    return (cleaned[:max_len] or "item").strip("_")


def _resolve_paths(config: dict) -> tuple[Path, Path]:
    output_cfg = config.get("output", {}) if isinstance(config, dict) else {}
    resumes_dir = Path(output_cfg.get("resumes_dir", "output/resumes"))
    cover_letters_dir = Path(output_cfg.get("cover_letters_dir", "output/cover_letters"))
    resumes_dir.mkdir(parents=True, exist_ok=True)
    cover_letters_dir.mkdir(parents=True, exist_ok=True)
    return resumes_dir, cover_letters_dir


def _build_resume_text(profile: dict, scoring_result: dict, resume_content: dict) -> str:
    personal = profile.get("personal", {}) if isinstance(profile, dict) else {}
    lines = [
        f"Name: {personal.get('name', '')}",
        f"Title: {personal.get('title', '')}",
        "",
        "SUMMARY",
        resume_content.get("summary", "") if isinstance(resume_content, dict) else "",
        "",
        "CORE SKILLS",
        ", ".join((resume_content.get("core_skills") or [])) if isinstance(resume_content, dict) else "",
        "",
        "KEY ACHIEVEMENTS",
    ]
    for item in (resume_content.get("key_achievements") or []) if isinstance(resume_content, dict) else []:
        lines.append(f"- {item}")

    lines.append("")
    lines.append("SCORE CONTEXT")
    lines.append(f"Recommended title: {(scoring_result or {}).get('recommended_title_for_resume', '')}")
    lines.append(f"Matched keywords: {', '.join((scoring_result or {}).get('keywords_to_emphasise', []))}")

    return "\n".join(lines).strip() + "\n"


def generate_apply_pack(
    profile: dict,
    job_title: str,
    company: str,
    job_location: str,
    job_id: str,
    scoring_result: dict,
    resume_content: dict,
    cover_letter_text: str,
    config: dict,
) -> tuple[Path, Path]:
    """Generate apply-pack artifacts and return absolute file paths."""
    resumes_dir, cover_letters_dir = _resolve_paths(config)

    candidate_name = ((profile or {}).get("personal", {}) or {}).get("name", "candidate")
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")

    base = "_".join(
        [
            _safe_filename(candidate_name, 40),
            _safe_filename(company, 40),
            _safe_filename(job_title, 48),
            _safe_filename(job_id, 24),
            ts,
        ]
    )

    resume_path = (resumes_dir / f"{base}.txt").resolve()
    cover_letter_path = (cover_letters_dir / f"{base}_cover_letter.txt").resolve()

    resume_body = _build_resume_text(profile, scoring_result or {}, resume_content or {})
    resume_path.write_text(resume_body, encoding="utf-8")
    cover_letter_path.write_text((cover_letter_text or "").strip() + "\n", encoding="utf-8")

    return resume_path, cover_letter_path

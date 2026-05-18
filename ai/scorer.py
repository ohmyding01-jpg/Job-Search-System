"""
AI-powered job relevance scorer.
Returns a 0-100 score with breakdown across multiple dimensions.
"""

from rich.console import Console

from ai.provider import generate_text, parse_json_response, provider_name
from tracker import Job

console = Console()

TARGET_TERMS = {
    "project manager",
    "program manager",
    "technical program manager",
    "technical project manager",
    "it project manager",
    "it program manager",
    "delivery manager",
    "implementation manager",
    "implementation project manager",
    "service delivery",
    "pmo",
    "scrum master",
    "agile",
    "change management",
    "business project manager",
}

LOW_FIT_TITLE_TERMS = {
    "construction",
    "water/wastewater",
    "wastewater",
    "water resources",
    "geotechnical",
    "civil engineer",
    "mechanical",
    "electrical",
    "estimator",
    "superintendent",
    "foreman",
    "nurse",
    "rn ",
    "clinical operations",
    "licensed",
    "residential program",
    "retail operations",
    "store manager",
    "sales manager",
    "account executive",
    "executive assistant",
    "legal project manager",
    "bid manager",
    "product manager",
}

LOW_FIT_TEXT_TERMS = {
    "construction management",
    "civil engineering",
    "water/wastewater",
    "geotechnical",
    "nursing license",
    "registered nurse",
    "retail store",
    "sales quota",
    "portfolio required",
    "architecture license",
    "pe license",
    "professional engineer",
}

HIGH_SIGNAL_TERMS = {
    "jira",
    "agile",
    "scrum",
    "pmp",
    "pmo",
    "stakeholder",
    "cross-functional",
    "implementation",
    "delivery",
    "servicenow",
    "cloud",
    "aws",
    "data center",
    "enterprise",
    "federal",
    "public trust",
    "program management",
    "project management",
}


def _clamp_score(score: int) -> int:
    return max(0, min(100, score))


def _contains_any(text: str, terms: set[str]) -> list[str]:
    return [term for term in terms if term in text]


def _hard_title_reject(job: Job, profile: dict) -> dict | None:
    """Reject jobs whose title contains a term the candidate explicitly can't do."""
    reject_terms = (profile or {}).get("reject_title_terms", [])
    if not reject_terms:
        return None
    title = (job.title or "").lower()
    for term in reject_terms:
        if term.lower() in title:
            return {
                "score": 2, "prefiltered": True,
                "breakdown": {}, "best_resume_variant": "default",
                "key_requirements": [], "gaps": [f"Title contains rejected term: '{term}'"],
                "strengths": [], "reasoning": f"Hard-rejected: title contains '{term}'.",
                "recommended_title_for_resume": job.title,
                "keywords_to_emphasise": [],
            }
    return None


def _hard_remote_filter(job: Job, profile: dict) -> dict | None:
    """If the candidate requires remote work, reject any clearly on-site job immediately."""
    if not (profile or {}).get("preferences", {}).get("requires_remote"):
        return None
    location = (job.location or "").lower()
    description = (job.description or "").lower()[:500]
    # Reject if the job location is clearly on-site
    on_site_signals = ("on-site", "onsite", "on site", "in-office", "in office",
                       "must be located", "must reside", "must live", "relocation required")
    for signal in on_site_signals:
        if signal in location or signal in description:
            return {
                "score": 5, "prefiltered": True,
                "breakdown": {}, "best_resume_variant": "default",
                "key_requirements": [], "gaps": [f"Requires physical presence ({signal})"],
                "strengths": [], "reasoning": f"Rejected: on-site requirement detected ({signal}).",
                "recommended_title_for_resume": job.title,
                "keywords_to_emphasise": [],
            }
    return None


def cheap_prefilter_score(job: Job, config: dict) -> dict | None:
    """Return a low-score result for obvious non-fits before spending API credits."""
    scoring_config = config.get("scoring", {})
    if scoring_config.get("prefilter_enabled", True) is False:
        return None

    title = (job.title or "").lower()
    company = (job.company or "").lower()
    description = (job.description or "").lower()
    text = f"{title} {company} {job.location or ''} {description[:2000]}".lower()

    target_hits = _contains_any(text, TARGET_TERMS)
    title_target_hits = _contains_any(title, TARGET_TERMS)
    title_low_hits = _contains_any(title, LOW_FIT_TITLE_TERMS)
    text_low_hits = _contains_any(text, LOW_FIT_TEXT_TERMS)
    high_signal_hits = _contains_any(text, HIGH_SIGNAL_TERMS)

    reason = None
    score = None

    if "product manager" in title and "program manager" not in title and "project manager" not in title and "technical program" not in title:
        reason = "plain product-manager role, not a project/program delivery role"
        score = 24
    elif title_low_hits and not title_target_hits and len(high_signal_hits) < 2:
        reason = f"title points outside target roles: {', '.join(title_low_hits[:3])}"
        score = 18
    elif text_low_hits and len(target_hits) <= 1 and len(high_signal_hits) < 2:
        reason = f"posting language points outside target roles: {', '.join(text_low_hits[:3])}"
        score = 22
    elif not title_target_hits and len(high_signal_hits) < 2:
        reason = "missing project/program/delivery signals in the title and description"
        score = 28

    if reason is None:
        return None

    result = {
        "score": score,
        "prefiltered": True,
        "breakdown": {
            "title_match": min(8, score // 4),
            "skills_match": min(8, score // 4),
            "seniority_match": 4,
            "location_match": 8,
            "industry_fit": 2,
        },
        "best_resume_variant": "general_pm",
        "key_requirements": [],
        "gaps": [reason],
        "strengths": high_signal_hits[:3],
        "reasoning": f"Local prefilter skipped Claude because this looks like a low-fit job: {reason}.",
        "recommended_title_for_resume": job.title,
        "keywords_to_emphasise": high_signal_hits[:5],
    }
    console.print(f"  [dim]Prefilter saved AI call: {score}/100 — {job.title} @ {job.company} ({reason})[/dim]")
    return result


def cheap_local_score(job: Job, config: dict) -> dict:
    """Budget-safe local score for plausible jobs when paid AI is disabled or capped."""
    prefiltered = cheap_prefilter_score(job, config)
    if prefiltered:
        return prefiltered

    title = (job.title or "").lower()
    description = (job.description or "").lower()
    location = (job.location or "").lower()
    text = f"{title} {job.company or ''} {location} {description[:2000]}".lower()

    title_target_hits = _contains_any(title, TARGET_TERMS)
    target_hits = _contains_any(text, TARGET_TERMS)
    high_signal_hits = _contains_any(text, HIGH_SIGNAL_TERMS)
    title_low_hits = _contains_any(title, LOW_FIT_TITLE_TERMS)
    text_low_hits = _contains_any(text, LOW_FIT_TEXT_TERMS)

    score = 42
    score += min(28, len(title_target_hits) * 14)
    score += min(18, len(high_signal_hits) * 3)
    score += min(8, len(target_hits) * 2)

    if any(term in title for term in ("senior", "sr.", "lead", "principal", "manager")):
        score += 5
    if any(term in location for term in ("remote", "washington", "dc", "virginia", "maryland", "new york", "ny")):
        score += 8
    if title_low_hits:
        score -= 18
    if text_low_hits:
        score -= 10

    score = min(88, _clamp_score(score))
    result = {
        "score": score,
        "local_only": True,
        "breakdown": {
            "title_match": min(25, len(title_target_hits) * 12),
            "skills_match": min(30, len(high_signal_hits) * 4),
            "seniority_match": 14 if any(term in title for term in ("senior", "sr.", "lead", "principal", "manager")) else 9,
            "location_match": 15 if "remote" in location else 12 if any(term in location for term in ("washington", "dc", "virginia", "maryland", "new york", "ny")) else 8,
            "industry_fit": min(10, len(high_signal_hits)),
        },
        "best_resume_variant": "tech_pm" if "technical" in title else "program_manager" if "program" in title else "general_pm",
        "key_requirements": high_signal_hits[:5],
        "gaps": ["AI scoring disabled or capped by budget guard; this is a local estimate."],
        "strengths": high_signal_hits[:5],
        "reasoning": "Local budget-safe score based on title, location, and PM/IT delivery keywords. No paid AI call was made.",
        "recommended_title_for_resume": job.title,
        "keywords_to_emphasise": high_signal_hits[:5],
    }
    console.print(f"  [dim]Local score saved AI call: {score}/100 — {job.title} @ {job.company}[/dim]")
    return result


SCORING_SYSTEM_PROMPT = """You are an expert career advisor and ATS screening specialist.
Your task is to evaluate how well a candidate's profile matches a job posting.
Return ONLY valid JSON — no markdown, no explanation outside the JSON."""


SCORING_PROMPT_TEMPLATE = """Evaluate this job against the candidate's profile.

## Candidate Profile
Name: {name}
Target roles: {target_roles}
Location: {candidate_location} (open to remote anywhere in the US)
Key skills: {skills}
Years of experience: Senior level (7+ years)
Certifications: {certs}
Preferred industries: {industries}
Available resume variants: {resume_variants}

## Job Posting
Title: {job_title}
Company: {company}
Location: {job_location}
Description:
{description}

## Scoring Instructions
Score this job 0-100 on overall fit for THIS candidate. Break down:

1. title_match (0-25): How closely does the job title match the candidate's target roles?
2. skills_match (0-30): How many of the required skills does the candidate have?
3. seniority_match (0-20): Is the seniority level right for a 7+ year senior professional?
4. location_match (0-15): Remote = 15 pts. Candidate's state/region = 12 pts. Other US = 8 pts. Outside US = 0 pts.
5. industry_fit (0-10): Is this a preferred industry for this candidate?

Select the best resume variant from the available variants listed above.

Return ONLY this exact JSON (no markdown, no prose outside the JSON):
{{
  "score": <0-100 integer>,
  "breakdown": {{
    "title_match": <0-25>,
    "skills_match": <0-30>,
    "seniority_match": <0-20>,
    "location_match": <0-15>,
    "industry_fit": <0-10>
  }},
  "best_resume_variant": "<one of the available variant keys>",
  "key_requirements": ["<req1>", "<req2>", "<req3>"],
  "gaps": ["<gap1>"],
  "strengths": ["<strength1>", "<strength2>"],
  "reasoning": "<2-3 sentence explanation of the score for THIS candidate>",
  "recommended_title_for_resume": "<exact job title to use in tailored resume>",
  "keywords_to_emphasise": ["<kw1>", "<kw2>", "<kw3>", "<kw4>", "<kw5>"]
}}"""


_SCOUT_SYSTEM = "You are a job-fit scorer. Return ONLY valid JSON, no prose."

_SCOUT_TEMPLATE = """Score this job 0-100 for a candidate targeting: {target_roles}
Candidate location: {candidate_location}. Key skills: {skills_brief}

Job title: {job_title}
Company: {company}
Location: {job_location}
Description (first 300 chars): {desc_brief}

Return ONLY: {{"score":<0-100>,"best_resume_variant":"<{variants}>","strengths":["s1","s2"],"keywords_to_emphasise":["k1","k2","k3"],"reasoning":"<1 sentence>"}}"""


async def scout_score_job(job: Job, profile: dict, scout_config: dict) -> dict:
    """Lightweight free scoring via local Ollama — uses a short prompt for speed."""
    target_roles = profile.get("target_role_summary") or profile["personal"].get("title", "")
    skills_brief = ", ".join(profile.get("high_signal_terms", [])[:8]) or "SQL, cloud, database"
    variants = "|".join(profile.get("resume_variants", {}).keys()) or "default"
    first_variant = next(iter(profile.get("resume_variants", {}).keys()), "default")

    prompt = _SCOUT_TEMPLATE.format(
        target_roles=target_roles,
        candidate_location=profile["personal"]["location"],
        skills_brief=skills_brief,
        job_title=job.title,
        company=job.company,
        job_location=job.location,
        variants=variants,
        desc_brief=(job.description or "")[:300],
    )
    try:
        raw = await generate_text(
            ai_config=scout_config,
            system_prompt=_SCOUT_SYSTEM,
            prompt=prompt,
            max_tokens=scout_config.get("max_tokens_scoring", 200),
            json_mode=True,
        )
        result = parse_json_response(raw)
        result.setdefault("score", 50)
        result.setdefault("breakdown", {})
        result.setdefault("best_resume_variant", first_variant)
        result.setdefault("key_requirements", [])
        result.setdefault("gaps", [])
        result.setdefault("recommended_title_for_resume", job.title)
        console.print(
            f"  Ollama scout: [{'green' if result['score'] >= 70 else 'yellow' if result['score'] >= 50 else 'red'}]"
            f"{result['score']}/100[/] — {job.title} @ {job.company}"
        )
        return result
    except Exception as e:
        console.print(f"[yellow]Ollama scout fallback for {job.job_id}: {e}[/yellow]")
        return {
            "score": 55, "scoring_failed": False,
            "breakdown": {}, "best_resume_variant": first_variant,
            "key_requirements": [], "gaps": [], "strengths": [],
            "reasoning": "Scout timeout — using neutral score.",
            "recommended_title_for_resume": job.title,
            "keywords_to_emphasise": profile.get("high_signal_terms", [])[:5],
        }


async def score_job(job: Job, profile: dict, ai_config: dict) -> dict:
    """Score a job against the candidate profile. Returns scoring result dict."""
    skills_flat = []
    for cat, items in profile.get("skills", {}).items():
        if isinstance(items, list):
            skills_flat.extend(items[:5])
    certs = ", ".join(profile.get("certifications", [])[:4])
    industries = ", ".join(profile.get("skills", {}).get("industries", []))

    target_roles = profile.get("target_role_summary") or profile["personal"].get("title", "")
    resume_variants = ", ".join(profile.get("resume_variants", {}).keys()) or "default"
    preferred_industries = ", ".join(
        profile.get("preferences", {}).get("preferred_industries", [])
    ) or industries
    prompt = SCORING_PROMPT_TEMPLATE.format(
        name=profile["personal"]["name"],
        target_roles=target_roles,
        candidate_location=profile["personal"]["location"],
        skills=", ".join(skills_flat[:20]),
        certs=certs,
        industries=preferred_industries,
        resume_variants=resume_variants,
        job_title=job.title,
        company=job.company,
        job_location=job.location,
        description=job.description[:3000] if job.description else "Not available",
    )

    first_default_variant = next(iter(profile.get("resume_variants", {}).keys()), "default")

    try:
        raw = await generate_text(
            ai_config=ai_config,
            system_prompt=SCORING_SYSTEM_PROMPT,
            prompt=prompt,
            max_tokens=ai_config["max_tokens_scoring"],
            json_mode=True,
        )
        result = parse_json_response(raw)
        tier = provider_name(ai_config)
        label = "Ollama" if tier == "ollama" else tier.title()
        console.print(
            f"  {label} score: [{'green' if result['score'] >= 70 else 'yellow' if result['score'] >= 50 else 'red'}]"
            f"{result['score']}/100[/] — {job.title} @ {job.company}"
        )
        return result
    except Exception as e:
        console.print(f"[red]Scoring failed for {job.job_id}: {e}[/red]")
        return {
            "score": 0,
            "scoring_failed": True,
            "breakdown": {},
            "best_resume_variant": first_default_variant,
            "key_requirements": [],
            "gaps": [],
            "strengths": [],
            "reasoning": f"Scoring error: {e}",
            "recommended_title_for_resume": job.title,
            "keywords_to_emphasise": [],
        }


async def batch_score_jobs(jobs: list[Job], profile: dict, config: dict) -> list[tuple[Job, dict]]:
    """Score multiple jobs. Returns list of (job, scoring_result) tuples.

    Uses scout_ai config (Ollama, free) if available; falls back to the tailor
    ai config if Ollama is not reachable.
    """
    results = []
    # Scout tier: cheap/free model for bulk scoring.
    scout_cfg = config.get("scout_ai", {})
    scout_enabled = scout_cfg.get("enabled", False) and bool(scout_cfg.get("model"))
    if scout_enabled:
        from ai.provider import _ollama_available
        if not _ollama_available():
            scout_enabled = False
            console.print("[yellow]Ollama not reachable — falling back to tailor model for scoring[/yellow]")

    ai_config = scout_cfg if scout_enabled else config.get("ai", {})
    ai_enabled = config.get("ai", {}).get("enabled", True)
    budget_config = config.get("budget", {})
    max_ai_calls = budget_config.get("max_ai_scoring_calls_per_run")
    if max_ai_calls is None:
        max_ai_calls = budget_config.get("max_claude_scoring_calls_per_run")
    if max_ai_calls is not None:
        max_ai_calls = max(0, int(max_ai_calls))
    ai_calls = 0
    credits_exhausted = False

    # Pass 1: score every job.
    # Scout tier (Ollama, free) handles bulk scoring; tailor model is the fallback.
    local_results: list[tuple[Job, dict]] = []
    ai_candidates: list[tuple[Job, dict]] = []

    for job in jobs:
        # Title reject runs first — cheapest possible check, no API call.
        hard = _hard_title_reject(job, profile)
        if hard is not None:
            local_results.append((job, hard))
            continue
        # Hard remote filter — reject on-site jobs for remote-only candidates.
        hard = _hard_remote_filter(job, profile)
        if hard is not None:
            local_results.append((job, hard))
            continue
        prefilter = cheap_prefilter_score(job, config)
        if prefilter is not None:
            # Prefilter says clear low-fit — skip AI scoring.
            local_results.append((job, prefilter))
        else:
            if scout_enabled:
                scoring = await scout_score_job(job, profile, ai_config)
            else:
                scoring = cheap_local_score(job, config)
            ai_candidates.append((job, scoring))
            local_results.append((job, scoring))

    ai_cap = 0
    tailor_ai_config = config.get("ai", {})
    if ai_enabled:
        ai_cap = len(ai_candidates) if max_ai_calls is None else min(len(ai_candidates), max_ai_calls)

    # Highest local-fit jobs first for tailor-model re-scoring.
    ai_candidates.sort(key=lambda item: int(item[1].get("score") or 0), reverse=True)
    ai_job_ids = {job.job_id for job, _ in ai_candidates[:ai_cap]}

    for job, scoring in local_results:
        final_scoring = scoring

        if credits_exhausted:
            results.append((job, final_scoring))
            continue

        if ai_enabled and not scout_enabled and job.job_id in ai_job_ids:
            ai_calls += 1
            ai_scoring = await score_job(job, profile, tailor_ai_config)
            if ai_scoring.get("scoring_failed"):
                final_scoring = scoring
            else:
                final_scoring = ai_scoring

            # Detect credit exhaustion — bail out early for the rest
            if ai_scoring.get("scoring_failed") and any(
                phrase in ai_scoring.get("reasoning", "").lower()
                for phrase in ["credit balance is too low", "quota", "billing", "api key"]
            ):
                credits_exhausted = True
                console.print("[yellow]⚠ AI provider billing/key/quota issue — using local scoring this cycle.[/yellow]")

        results.append((job, final_scoring))

        if not final_scoring.get("scoring_failed"):
            job.score = final_scoring["score"]
            job.score_breakdown = final_scoring.get("breakdown", {})
            job.score_reasoning = final_scoring.get("reasoning", "")
            job.status = "scored"

    return results

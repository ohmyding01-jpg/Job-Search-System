"""
AI-powered resume tailoring.
Takes the master resume profile + job scoring result and produces
a tailored resume text that's embedded into a .docx template.
"""

from rich.console import Console

from ai.provider import generate_text
from resume_sources import get_selected_resume_source

console = Console()


_RESUME_SYSTEM_TEMPLATE = """You are a professional resume writer specialising in {role_field} roles
in the United States. You write concise, ATS-optimised resumes that are truthful and impactful.
Never fabricate experience or credentials. Only strengthen and tailor what the candidate already has."""


RESUME_PROMPT_TEMPLATE = """Tailor this candidate's resume for the specific job below.

## Candidate Master Profile
{profile_yaml}

## Selected Source Resume
Variant: {source_variant}
File: {source_file}
Use this resume text as the factual source of truth. You may rephrase, reorder,
and emphasize relevant content, but do not invent experience, employers,
certifications, degrees, metrics, or tools that are not supported here or in the
master profile.

{source_resume_text}

## Target Job
Title: {job_title}
Company: {company}
Location: {location}
Key Requirements: {requirements}
Keywords to emphasise: {keywords}
Gaps to address: {gaps}
Recommended resume title: {recommended_title}

## Instructions
1. Write a tailored professional SUMMARY (3-4 sentences) that directly addresses this role's requirements.
2. Reorder and rephrase bullet points under each experience entry to front-load the most relevant achievements.
3. Move relevant skills to the top of the skills section.
4. Do NOT invent new experience, companies, or qualifications.
5. Prefer concrete accomplishments and metrics from the selected source resume.
6. Keep the output structured exactly as shown below — it will be parsed programmatically.

Return this EXACT structure (no markdown headers, just the content):

SUMMARY:
<tailored 3-4 sentence summary targeting this specific role>

CORE_SKILLS:
<comma-separated skills, most relevant first, max 20>

EXPERIENCE_BULLETS:
JOB_1:
- <tailored bullet 1>
- <tailored bullet 2>
- <tailored bullet 3>
- <tailored bullet 4>
JOB_2:
- <tailored bullet 1>
- <tailored bullet 2>
- <tailored bullet 3>
JOB_3:
- <tailored bullet 1>
- <tailored bullet 2>

KEY_ACHIEVEMENTS:
- <quantified achievement most relevant to this role>
- <quantified achievement 2>
- <quantified achievement 3>

KEYWORD_TAGS:
<10-15 ATS keywords from the job description that this resume should contain, comma-separated>"""


async def generate_tailored_resume_content(
    profile: dict,
    job_title: str,
    company: str,
    location: str,
    scoring_result: dict,
    ai_config: dict,
) -> dict:
    """
    Generate tailored resume content sections.
    Returns a dict with keys: summary, core_skills, experience_bullets, key_achievements, keyword_tags.
    """
    import yaml

    profile_str = yaml.dump(
        {k: v for k, v in profile.items() if k != "resume_variants"},
        default_flow_style=False,
        allow_unicode=True,
    )[:4000]
    source_resume = get_selected_resume_source(profile, scoring_result, char_limit=7000)

    prompt = RESUME_PROMPT_TEMPLATE.format(
        profile_yaml=profile_str,
        source_variant=source_resume.get("variant_key", ""),
        source_file=source_resume.get("filename", ""),
        source_resume_text=source_resume.get("text", ""),
        job_title=job_title,
        company=company,
        location=location,
        requirements=", ".join(scoring_result.get("key_requirements", [])[:6]),
        keywords=", ".join(scoring_result.get("keywords_to_emphasise", [])[:8]),
        gaps=", ".join(scoring_result.get("gaps", [])[:3]),
        recommended_title=scoring_result.get("recommended_title_for_resume", job_title),
    )

    role_field = profile.get("target_role_summary") or profile["personal"].get("title", "Technology")
    system_prompt = _RESUME_SYSTEM_TEMPLATE.format(role_field=role_field)

    try:
        raw = await generate_text(
            ai_config=ai_config,
            system_prompt=system_prompt,
            prompt=prompt,
            max_tokens=ai_config["max_tokens_resume"],
        )
        return _parse_resume_response(raw)
    except Exception as e:
        console.print(f"[red]Resume generation failed: {e}[/red]")
        return {}


def _parse_resume_response(raw: str) -> dict:
    """Parse the structured resume text output into a dict."""
    result = {
        "summary": "",
        "core_skills": [],
        "experience_bullets": {},
        "key_achievements": [],
        "keyword_tags": [],
    }

    current_section = None
    current_job = None
    lines = raw.split("\n")

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("SUMMARY:"):
            current_section = "summary"
            continue
        elif stripped.startswith("CORE_SKILLS:"):
            current_section = "core_skills"
            continue
        elif stripped.startswith("EXPERIENCE_BULLETS:"):
            current_section = "experience_bullets"
            continue
        elif stripped.startswith("KEY_ACHIEVEMENTS:"):
            current_section = "key_achievements"
            continue
        elif stripped.startswith("KEYWORD_TAGS:"):
            current_section = "keyword_tags"
            continue
        elif stripped.startswith("JOB_") and stripped.endswith(":"):
            current_job = stripped[:-1]
            result["experience_bullets"][current_job] = []
            continue

        if not stripped:
            continue

        if current_section == "summary":
            result["summary"] += (" " if result["summary"] else "") + stripped
        elif current_section == "core_skills":
            result["core_skills"] = [s.strip() for s in stripped.split(",") if s.strip()]
        elif current_section == "experience_bullets" and current_job:
            if stripped.startswith("- "):
                result["experience_bullets"][current_job].append(stripped[2:])
        elif current_section == "key_achievements":
            if stripped.startswith("- "):
                result["key_achievements"].append(stripped[2:])
        elif current_section == "keyword_tags":
            result["keyword_tags"] = [s.strip() for s in stripped.split(",") if s.strip()]

    return result

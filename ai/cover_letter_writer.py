"""
AI-powered cover letter generator.
Produces a professional, targeted cover letter tailored to each job.
"""

from rich.console import Console

from ai.provider import generate_text
from resume_sources import get_selected_resume_source

console = Console()


_COVER_LETTER_SYSTEM_TEMPLATE = """You are an expert career coach writing cover letters for {role_field}
professionals in the United States. You write natural, confident, non-generic cover letters that show
genuine understanding of the company and role. Never use clichés like 'I am writing to express
my interest'. Start with a strong, specific hook. Keep it to 3 paragraphs max."""


COVER_LETTER_TEMPLATE = """Write a tailored cover letter for this application.

## Candidate
Name: {name}
Email: {email}
Location: {location}
Title: {target_title}

## Job
Company: {company}
Role: {job_title}
Location: {job_location}
Key requirements: {requirements}
Company context: {company_context}

## Candidate Strengths for this Role
{strengths}

## Selected Source Resume
Variant: {source_variant}
File: {source_file}
Use this as factual grounding for examples, metrics, tools, and scope. Do not
invent achievements or credentials.

{source_resume_text}

## Instructions
- Open with a compelling 1-2 sentence hook that directly references the specific role and why the candidate is uniquely qualified
- Paragraph 2: Connect 2-3 specific achievements from the candidate's background to the role's key requirements. Use metrics where possible.
- Paragraph 3: Brief closing with enthusiasm for the company specifically (not generic), and call to action
- Professional American English
- Total length: 250-320 words
- NO subject line, NO date, NO address blocks — just the 3 paragraphs + sign-off
- End with: Regards, [Name]

Write the cover letter now:"""


async def generate_cover_letter(
    profile: dict,
    job_title: str,
    company: str,
    job_location: str,
    scoring_result: dict,
    ai_config: dict,
) -> str:
    """Generate a tailored cover letter. Returns the full text as a string."""
    personal = profile["personal"]
    strengths_text = "\n".join(
        f"- {s}" for s in scoring_result.get("strengths", [])[:5]
    )
    source_resume = get_selected_resume_source(profile, scoring_result, char_limit=5500)

    prompt = COVER_LETTER_TEMPLATE.format(
        name=personal["name"],
        email=personal["email"],
        location=personal["location"],
        target_title=scoring_result.get("recommended_title_for_resume", job_title),
        company=company,
        job_title=job_title,
        job_location=job_location,
        requirements=", ".join(scoring_result.get("key_requirements", [])[:5]),
        company_context=f"{company} — {job_location}",
        strengths=strengths_text or profile.get("summary", "")[:300] or "Strong technical background with deep expertise in the target domain.",
        source_variant=source_resume.get("variant_key", ""),
        source_file=source_resume.get("filename", ""),
        source_resume_text=source_resume.get("text", ""),
    )

    role_field = profile.get("target_role_summary") or profile["personal"].get("title", "technology")
    system_prompt = _COVER_LETTER_SYSTEM_TEMPLATE.format(role_field=role_field)

    try:
        text = await generate_text(
            ai_config=ai_config,
            system_prompt=system_prompt,
            prompt=prompt,
            max_tokens=ai_config["max_tokens_cover_letter"],
        )
        text = text.strip()
        console.print(f"  [green]Cover letter generated[/green] ({len(text)} chars)")
        return text
    except Exception as e:
        console.print(f"[red]Cover letter generation failed: {e}[/red]")
        return _fallback_cover_letter(profile, job_title, company)


def _fallback_cover_letter(profile: dict, job_title: str, company: str) -> str:
    """Minimal fallback if API call fails — uses the candidate's own summary."""
    personal = profile["personal"]
    name = personal["name"]
    title = personal.get("title", "experienced professional")
    summary_snippet = (profile.get("summary") or "")[:200].strip()
    return (
        f"Dear Hiring Manager,\n\n"
        f"As a {title}, I am excited to apply for the {job_title} role at {company}. "
        f"{summary_snippet}\n\n"
        f"My background and skills align well with your requirements and I would welcome "
        f"the opportunity to discuss how I can contribute to your team's success.\n\n"
        f"Regards,\n{name}"
    )

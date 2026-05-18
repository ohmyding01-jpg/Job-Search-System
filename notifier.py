"""
Notification module — logs to console and optionally sends email.
"""

import os
import smtplib
import logging
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from pathlib import Path
from rich.console import Console
from rich.table import Table

console = Console()
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
APPLICATIONS_LOG = LOG_DIR / "applications.log"

logging.basicConfig(
    filename=LOG_DIR / "agent.log",
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("job_agent")


def log_successful_application(
    job_title: str,
    company: str,
    score: int,
    *,
    source_platform: str,
    apply_url: str = "",
    resume_path: str = "",
):
    """Persist a clear, timestamped record of every real application submission.

    Writes to logs/applications.log AND prints a high-visibility banner so the
    user can see when an application actually completed.
    """
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    platform = (source_platform or "unknown").upper()
    line = (
        f"[{ts}] APPLIED | {platform:<14} | "
        f"score={score:>3} | {job_title} @ {company} | url={apply_url or '-'}"
    )
    try:
        with open(APPLICATIONS_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as e:
        logger.error(f"Failed to append to applications log: {e}")

    console.print()
    console.print(f"[bold green]{'═' * 78}[/bold green]")
    console.print(f"[bold green]✓ APPLIED on {platform}[/bold green]   [white]{ts}[/white]")
    console.print(f"[bold]{job_title}[/bold] @ [cyan]{company}[/cyan]   [yellow](score {score}/100)[/yellow]")
    if apply_url:
        console.print(f"[dim]{apply_url}[/dim]")
    console.print(f"[bold green]{'═' * 78}[/bold green]")
    console.print()
    logger.info(line)


def _collect_notify_recipients() -> list[str]:
    recipients: list[str] = []

    csv_recipients = os.getenv("NOTIFY_EMAILS", "")
    if csv_recipients:
        recipients.extend([part.strip() for part in csv_recipients.split(",") if part.strip()])

    single = os.getenv("NOTIFY_EMAIL", "").strip()
    if single:
        recipients.append(single)

    cc = os.getenv("NOTIFY_CC_EMAIL", "").strip()
    if cc:
        recipients.append(cc)

    deduped: list[str] = []
    seen = set()
    for recipient in recipients:
        key = recipient.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(recipient)
    return deduped


def log_run_summary(stats: dict, new_jobs: int, applied: int, packs: int, errors: list[str]):
    """Print a rich summary table and log to file."""
    table = Table(title=f"Run Summary — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", style="white")

    table.add_row("New jobs found", str(new_jobs))
    table.add_row("Scored above threshold", str(stats.get("scored", 0)))
    table.add_row("Apply packs generated", str(packs))
    table.add_row("Auto-applied", str(applied))
    table.add_row("Total in DB", str(stats.get("total", 0)))
    table.add_row("Total applied (all-time)", str(stats.get("applied", 0)))
    table.add_row("Avg score (scored jobs)", str(stats.get("avg_score", 0)))

    console.print(table)

    if errors:
        console.print(f"[red]Errors: {len(errors)}[/red]")
        for err in errors[:5]:
            console.print(f"  [red]• {err}[/red]")

    logger.info(
        f"Run complete | new={new_jobs} packs={packs} applied={applied} errors={len(errors)}"
    )


def notify_application(
    job_title: str,
    company: str,
    score: int,
    resume_path: str,
    *,
    source_platform: str = "linkedin",
    apply_url: str = "",
):
    """Record + announce a real application submission; also email if configured."""
    log_successful_application(
        job_title=job_title,
        company=company,
        score=score,
        source_platform=source_platform,
        apply_url=apply_url,
        resume_path=resume_path,
    )

    notify_recipients = _collect_notify_recipients()
    if not notify_recipients:
        return

    source_label = (source_platform or "job board").title()
    subject = f"[Job Agent] Applied via {source_label}: {job_title} @ {company} (Score: {score})"
    body = (
        f"The job agent auto-applied to:\n\n"
        f"Role: {job_title}\n"
        f"Company: {company}\n"
        f"Source: {source_label}\n"
        f"Relevance Score: {score}/100\n"
        f"Resume used: {resume_path}\n\n"
        f"Application URL: {apply_url or 'Not captured'}\n"
        f"Applied at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
    )

    _send_email(notify_recipients, subject, body)
    logger.info(f"Notification sent for {job_title} @ {company}")


def notify_pack_ready(job_title: str, company: str, score: int,
                       resume_path: str, cl_path: str, *, source_platform: str = "linkedin"):
    """Send email notification when a high-score pack is ready but needs manual review."""
    notify_recipients = _collect_notify_recipients()
    if not notify_recipients:
        return

    source_label = (source_platform or "job board").title()
    subject = f"[Job Agent] Pack Ready via {source_label} (Score {score}): {job_title} @ {company}"
    body = (
        f"A new apply pack is ready for your review:\n\n"
        f"Role: {job_title}\n"
        f"Company: {company}\n"
        f"Source: {source_label}\n"
        f"Relevance Score: {score}/100\n\n"
        f"Resume: {resume_path}\n"
        f"Cover Letter: {cl_path}\n\n"
        f"This job scored below the auto-apply threshold. Review and apply manually.\n"
    )

    _send_email(notify_recipients, subject, body)


def _send_email(to: list[str], subject: str, body: str):
    smtp_host = os.getenv("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "").strip() or (to[0] if to else "")
    smtp_pass = os.getenv("SMTP_PASS", "")

    placeholder_values = {
        "",
        "your@gmail.com",
        "your_app_password",
        "your_email@gmail.com",
        "your_password",
        "changeme",
    }
    if (
        not to
        or smtp_user.strip().lower() in placeholder_values
        or smtp_pass.strip().lower() in placeholder_values
    ):
        logger.warning("SMTP not configured — email notification not sent")
        console.print("[yellow]Email notification skipped: SMTP_PASS or SMTP_USER is not configured[/yellow]")
        return

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = to[0]
        if len(to) > 1:
            msg["Cc"] = ", ".join(to[1:])
        msg.attach(MIMEText(body, "plain"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to, msg.as_string())
    except Exception as e:
        logger.error(f"Email send failed: {e}")
        console.print(f"[red]Email send failed: {e}[/red]")

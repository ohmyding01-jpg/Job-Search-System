"""
LinkedIn Easy Apply bot.
Handles multi-step Easy Apply forms: fills text fields, uploads resume,
handles radio buttons and checkboxes, and submits the application.
"""

import asyncio
import random
import re
from pathlib import Path
from datetime import datetime
from playwright.async_api import Page
from rich.console import Console

from tracker import Job

console = Console()

EASY_APPLY_BUTTON_SELECTOR = (
    '#jobs-apply-button-id, '
    '[data-live-test-job-apply-button], '
    'a[aria-label*="Easy Apply" i], '
    'button[aria-label*="Easy Apply" i], '
    '[role="button"][aria-label*="Easy Apply" i], '
    '.jobs-apply-button--top-card, '
    'button.jobs-apply-button, '
    'button:has-text("Easy Apply")'
)


def _normalise_text(value: str) -> str:
    return " ".join((value or "").lower().split())


def _profile_years_experience(profile: dict) -> str:
    summary = profile.get("summary", "")
    match = re.search(r"(\d+)\+?\s+years", summary, re.I)
    return match.group(1) if match else "7"


def _answer_for_question(question: str, profile: dict, *, input_type: str = "text") -> str:
    """Return a conservative answer for common Easy Apply text/number fields."""
    personal = profile.get("personal", {})
    prefs = profile.get("preferences", {})
    q = _normalise_text(question)

    if input_type == "number":
        if any(term in q for term in ("salary", "compensation", "pay", "rate")):
            return str(prefs.get("min_salary") or 130000)
        return _profile_years_experience(profile)

    if "email" in q:
        return personal.get("email", "")
    if "phone" in q or "mobile" in q:
        return personal.get("phone", "")
    if "linkedin" in q:
        return personal.get("linkedin") or personal.get("linkedin_url", "")
    if "first name" in q:
        return personal.get("name", "").split(" ")[0]
    if "last name" in q:
        return personal.get("name", "").split(" ")[-1]
    if q in {"name", "full name"} or "full name" in q:
        return personal.get("name", "")
    _loc = personal.get("location", "Hoover, AL")
    if "city" in q:
        return _loc.split(",")[0].strip()
    if "state" in q and "statement" not in q:
        parts = _loc.split(",")
        return parts[-1].strip()[:2] if len(parts) > 1 else "AL"
    if "location" in q or "address" in q:
        return _loc
    if any(term in q for term in ("salary", "compensation", "pay", "rate")):
        return str(prefs.get("min_salary") or 130000)
    if any(term in q for term in ("notice", "start date", "available")):
        return prefs.get("available_from") or prefs.get("notice_period", "2 weeks")
    if "clearance" in q or "public trust" in q:
        return personal.get("visa_status", "US Citizen with Public Trust Clearance")
    if any(term in q for term in ("years", "experience", "how many")):
        return _profile_years_experience(profile)

    return "N/A"


def _choice_for_question(question: str, options: list[str], profile: dict) -> str | None:
    """Choose a truthful option for common Easy Apply radio/select questions."""
    q = _normalise_text(question)
    option_map = {_normalise_text(option): option for option in options if option and option.strip()}

    def pick(*needles: str) -> str | None:
        for needle in needles:
            for normalized, original in option_map.items():
                if needle in normalized:
                    return original
        return None

    yes = pick("yes")
    no = pick("no")

    if any(term in q for term in ("sponsor", "sponsorship", "visa", "h-1b", "h1b")):
        return no
    if any(term in q for term in ("authorized", "authorised", "eligible to work", "right to work", "us citizen", "u.s. citizen")):
        return yes
    if "public trust" in q:
        return yes if "public trust" in _normalise_text(profile.get("personal", {}).get("visa_status", "")) else no
    if "clearance" in q and any(term in q for term in ("secret", "top secret", "ts/sci")):
        return no
    if any(term in q for term in ("certify", "acknowledge", "accurate", "background check", "verify", "consent")):
        return yes
    if any(term in q for term in ("willing to commute", "willing to relocate", "relocate", "onsite", "on-site")):
        return no
    if "country" in q:
        return pick("united states", "usa", "us")
    if "state" in q and "statement" not in q:
        return pick("virginia", "va")
    if any(term in q for term in ("notice", "start", "available")):
        return pick("immediately", "2 weeks", "two weeks") or options[0] if options else None

    return None


async def _type_humanly(page: Page, selector: str, text: str, delay_range=(0.05, 0.15)):
    """Type text with human-like per-character delays."""
    el = page.locator(selector).first
    await el.click(timeout=3000)
    await el.fill("")
    for char in text:
        await el.type(char, delay=random.randint(50, 150))
        await asyncio.sleep(random.uniform(*delay_range))


async def _safe_click(page: Page, selector: str, timeout: int = 5000) -> bool:
    """Click an element if found. Returns True on success."""
    try:
        el = page.locator(selector).first
        await el.wait_for(state="visible", timeout=timeout)
        await asyncio.sleep(random.uniform(0.3, 0.8))
        await el.click(timeout=timeout)
        return True
    except Exception:
        return False


async def _click_first_visible(
    page: Page,
    selector: str,
    timeout: int = 8000,
    *,
    easy_apply_js_fallback: bool = False,
) -> bool:
    """
    Click the first visible matching element, ignoring hidden duplicates.
    Falls back to a direct JS click when Playwright's visibility check fails
    (e.g. button is clipped by LinkedIn's sticky header).
    """
    try:
        locator = page.locator(selector)
        await locator.first.wait_for(state="attached", timeout=timeout)
        count = await locator.count()
        for i in range(count):
            el = locator.nth(i)
            try:
                if not await el.is_visible():
                    continue
                if await el.is_disabled():
                    continue
                aria_disabled = (await el.get_attribute("aria-disabled") or "").lower()
                if aria_disabled == "true":
                    continue
                await el.scroll_into_view_if_needed(timeout=3000)
                await asyncio.sleep(random.uniform(0.2, 0.5))
                await el.click(timeout=5000)
                return True
            except Exception:
                try:
                    await el.click(timeout=3000, force=True)
                    return True
                except Exception:
                    continue

        if not easy_apply_js_fallback:
            return False

        # Easy Apply JS fallback — handles the top-card button when Playwright
        # marks it not-visible due to sticky-header occlusion or CSS transforms.
        # Do not use this for modal Next/Review/Submit buttons.
        #
        # JS fallback — handles buttons that Playwright marks not-visible due to
        # sticky-header occlusion, overflow clipping, or CSS transforms.
        try:
            clicked = await page.evaluate("""
                () => {
                    const candidates = [
                        '#jobs-apply-button-id',
                        '[data-live-test-job-apply-button]',
                        'a[aria-label*="Easy Apply" i]',
                        'button[aria-label*="Easy Apply" i]',
                        '[role="button"][aria-label*="Easy Apply" i]',
                        '.jobs-apply-button--top-card',
                        'button.jobs-apply-button',
                    ];
                    for (const sel of candidates) {
                        const el = document.querySelector(sel);
                        if (el && !el.disabled && el.getAttribute('aria-disabled') !== 'true') {
                            el.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)
            if clicked:
                await asyncio.sleep(1.0)
                return True
        except Exception:
            pass

        return False
    except Exception:
        return False


def _linkedin_job_view_url(url: str) -> str | None:
    """Return the canonical LinkedIn job view URL for a saved LinkedIn job URL."""
    match = re.search(r"linkedin\.com/jobs/view/(\d+)", url or "")
    if not match:
        return None
    return f"https://www.linkedin.com/jobs/view/{match.group(1)}/"


def _linkedin_easy_apply_flow_url(url: str) -> str | None:
    """Return the direct LinkedIn Easy Apply flow URL for a job URL."""
    if "/apply/" in (url or "") and "openSDUIApplyFlow=true" in (url or ""):
        return url

    match = re.search(r"linkedin\.com/jobs/view/(\d+)", url or "")
    if not match:
        return None
    return f"https://www.linkedin.com/jobs/view/{match.group(1)}/apply/?openSDUIApplyFlow=true"


async def _easy_apply_dialog_is_open(page: Page) -> bool:
    """Detect that the Easy Apply form itself is open, not just the job page."""
    try:
        return await page.evaluate("""
            () => {
                const dialogs = Array.from(document.querySelectorAll(
                    '.jobs-easy-apply-modal, .artdeco-modal, [role="dialog"]'
                ));
                return dialogs.some(el => /Apply to|Contact info|Submit application/i.test(el.innerText || ''));
            }
        """)
    except Exception:
        return False


async def _dismiss_cookie_banner(page: Page) -> None:
    """Dismiss LinkedIn's cookie banner when it appears over the job page."""
    for selector in ['button:has-text("Reject")', 'button:has-text("Accept")']:
        if await _safe_click(page, selector, timeout=1200):
            await asyncio.sleep(0.5)
            return


async def _continue_past_safety_reminder(page: Page) -> bool:
    """Continue past LinkedIn's job safety reminder when it appears before apply."""
    try:
        body_text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        body_text = ""
    if "Job search safety reminder" not in body_text and "Continue applying" not in body_text:
        return False

    clicked = await _click_first_visible(
        page,
        'a:has-text("Continue applying"), button:has-text("Continue applying")',
        timeout=3000,
    )
    if clicked:
        await asyncio.sleep(2.0)
    return clicked


async def _open_easy_apply_modal(page: Page, apply_url: str, selector: str, min_d: float, max_d: float) -> bool:
    """
    Navigate to a LinkedIn job URL and open the visible Easy Apply modal.

    LinkedIn often stores/serves URLs like /jobs/view/<id>/apply/?openSDUIApplyFlow=true
    that do not reliably expose a clickable button on reload. Retrying the canonical
    /jobs/view/<id>/ page catches those cases before we give up.
    """
    urls_to_try = []
    for candidate in [
        _linkedin_easy_apply_flow_url(apply_url),
        apply_url,
        _linkedin_job_view_url(apply_url),
    ]:
        if candidate and candidate not in urls_to_try:
            urls_to_try.append(candidate)

    canonical_url = _linkedin_job_view_url(apply_url)
    if canonical_url and canonical_url not in urls_to_try:
        urls_to_try.append(canonical_url)

    for attempt, url in enumerate(urls_to_try, start=1):
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(random.uniform(min_d, max_d))
            await _dismiss_cookie_banner(page)
            if await _easy_apply_dialog_is_open(page):
                return True
            await page.evaluate("window.scrollTo(0, 0)")
            await asyncio.sleep(0.5)
        except Exception as e:
            console.print(f"  [yellow]LinkedIn job page load failed on attempt {attempt}: {e}[/yellow]")
            await asyncio.sleep(3)  # brief pause before next URL attempt
            continue

        for _ in range(2):
            # 6 s to attach — fast-fails jobs that genuinely have no Easy Apply button
            clicked = await _click_first_visible(
                page,
                selector,
                timeout=6000,
                easy_apply_js_fallback=True,
            )
            if not clicked:
                break
            await asyncio.sleep(2.0)
            if await _easy_apply_dialog_is_open(page):
                return True
            if await _continue_past_safety_reminder(page):
                if await _easy_apply_dialog_is_open(page):
                    return True

        if attempt < len(urls_to_try):
            console.print("  [yellow]Easy Apply not visible on saved URL; retrying canonical job page[/yellow]")

    return False


async def _fill_text_field(page: Page, label_text: str, value: str) -> bool:
    """Find an input by its label text and fill it."""
    try:
        # Try label-for association
        labels = await page.query_selector_all("label")
        for label in labels:
            text = (await label.inner_text()).strip().lower()
            if label_text.lower() in text:
                for_attr = await label.get_attribute("for")
                if for_attr:
                    input_el = page.locator(f"#{for_attr}")
                    if await input_el.count() > 0:
                        await input_el.fill(value)
                        return True
                # Try sibling input
                input_el = page.locator(f"label:has-text('{label_text}') + input, "
                                         f"label:has-text('{label_text}') ~ input")
                if await input_el.count() > 0:
                    await input_el.first.fill(value)
                    return True
        return False
    except Exception:
        return False


async def _control_context(page: Page, selector: str) -> str:
    """Return nearby visible text for a form control."""
    try:
        return await page.locator(selector).first.evaluate(
            """
            (el) => {
                const parts = [];
                const id = el.getAttribute('id');
                if (id) {
                    const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                    if (label) parts.push(label.innerText || '');
                }
                parts.push(el.getAttribute('aria-label') || '');
                parts.push(el.getAttribute('placeholder') || '');
                parts.push(el.getAttribute('name') || '');
                const container = el.closest('fieldset, .jobs-easy-apply-form-section__grouping, .fb-dash-form-element, .artdeco-text-input--container, div');
                if (container) parts.push((container.innerText || '').slice(0, 500));
                return parts.join(' ').replace(/\\s+/g, ' ').trim();
            }
            """
        )
    except Exception:
        return ""


async def _fill_remaining_text_controls(page: Page, profile: dict) -> None:
    """Fill empty visible inputs/textarea fields using nearby label text."""
    inputs = page.locator('input:not([type="hidden"]):not([type="file"]):not([type="radio"]):not([type="checkbox"]), textarea')
    for i in range(await inputs.count()):
        field = inputs.nth(i)
        try:
            if not await field.is_visible() or await field.input_value():
                continue
            input_type = (await field.get_attribute("type") or "text").lower()
            context = await field.evaluate(
                """
                (el) => {
                    const parts = [];
                    const id = el.getAttribute('id');
                    if (id) {
                        const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                        if (label) parts.push(label.innerText || '');
                    }
                    parts.push(el.getAttribute('aria-label') || '');
                    parts.push(el.getAttribute('placeholder') || '');
                    parts.push(el.getAttribute('name') || '');
                    const container = el.closest('fieldset, .jobs-easy-apply-form-section__grouping, .fb-dash-form-element, .artdeco-text-input--container, div');
                    if (container) parts.push((container.innerText || '').slice(0, 500));
                    return parts.join(' ').replace(/\\s+/g, ' ').trim();
                }
                """
            )
            value = _answer_for_question(context, profile, input_type=input_type)
            if value:
                await field.fill(value, timeout=3000)
        except Exception:
            pass


async def _choose_remaining_radios(page: Page, profile: dict) -> None:
    """Answer known radio groups. Unknown groups are left untouched for manual review."""
    groups = page.locator('fieldset, .jobs-easy-apply-form-section__grouping, .fb-dash-form-element')
    for i in range(await groups.count()):
        group = groups.nth(i)
        try:
            radios = group.locator('input[type="radio"]')
            radio_count = await radios.count()
            if radio_count == 0:
                continue
            already_checked = False
            options: list[str] = []
            for j in range(radio_count):
                radio = radios.nth(j)
                if await radio.is_checked():
                    already_checked = True
                    break
                option_text = await radio.evaluate(
                    """
                    (el) => {
                        const id = el.getAttribute('id');
                        const label = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                        return (label?.innerText || el.getAttribute('aria-label') || el.value || '').replace(/\\s+/g, ' ').trim();
                    }
                    """
                )
                options.append(option_text)
            if already_checked:
                continue

            question = await group.evaluate(
                "(el) => (el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 700)"
            )
            choice = _choice_for_question(question, options, profile)
            if not choice:
                continue

            for j, option in enumerate(options):
                if _normalise_text(option) == _normalise_text(choice):
                    radio = radios.nth(j)
                    await radio.evaluate(
                        """
                        (el) => {
                            const id = el.getAttribute('id');
                            const label = id ? document.querySelector(`label[for="${CSS.escape(id)}"]`) : null;
                            (label || el).click();
                        }
                        """
                    )
                    break
        except Exception:
            pass


async def _choose_remaining_selects(page: Page, profile: dict) -> None:
    """Select known dropdown answers and avoid arbitrary choices for unknown questions."""
    selects = page.locator("select")
    for i in range(await selects.count()):
        select = selects.nth(i)
        try:
            if not await select.is_visible() or await select.input_value():
                continue
            question = await select.evaluate(
                """
                (el) => {
                    const parts = [];
                    const id = el.getAttribute('id');
                    if (id) {
                        const label = document.querySelector(`label[for="${CSS.escape(id)}"]`);
                        if (label) parts.push(label.innerText || '');
                    }
                    const container = el.closest('fieldset, .jobs-easy-apply-form-section__grouping, .fb-dash-form-element, div');
                    if (container) parts.push((container.innerText || '').slice(0, 500));
                    return parts.join(' ').replace(/\\s+/g, ' ').trim();
                }
                """
            )
            options = select.locator("option")
            option_pairs: list[tuple[str, str]] = []
            for j in range(await options.count()):
                option = options.nth(j)
                value = await option.get_attribute("value")
                label = (await option.inner_text()).strip()
                if value and label and "select" not in label.lower() and "choose" not in label.lower():
                    option_pairs.append((value, label))
            choice = _choice_for_question(question, [label for _, label in option_pairs], profile)
            if not choice:
                continue
            for value, label in option_pairs:
                if _normalise_text(label) == _normalise_text(choice):
                    await select.select_option(value=value, timeout=3000)
                    break
        except Exception:
            pass


async def _handle_form_step(page: Page, profile: dict) -> bool:
    """
    Fill a single Easy Apply form step.
    Returns True if the step was handled successfully.
    """
    personal = profile["personal"]
    prefs = profile.get("preferences", {})

    await asyncio.sleep(random.uniform(1.0, 2.0))

    # Phone number fields
    phone_filled = await _fill_text_field(page, "phone", personal.get("phone", ""))

    # City / location fields
    await _fill_text_field(page, "city", personal.get("location", ""))
    await _fill_text_field(page, "location", personal.get("location", ""))

    # Years of experience — try common patterns
    for label in ["years of experience", "experience", "how many years"]:
        await _fill_text_field(page, label, "8")

    # Salary expectations
    if prefs.get("min_salary"):
        for label in ["expected salary", "salary", "desired salary", "remuneration"]:
            await _fill_text_field(page, label, str(prefs["min_salary"]))

    # Notice period
    for label in ["notice period", "start date", "available"]:
        await _fill_text_field(page, label, prefs.get("notice_period", "2 weeks"))

    # Work authorization / right to work
    for text in ["yes", "currently authorised", "right to work", "citizen", "permanent resident"]:
        rb = page.locator(f"[aria-label*='{text}' i], label:has-text('{text}') input[type='radio']")
        if await rb.count() > 0:
            try:
                await rb.first.click(timeout=3000)
            except Exception:
                pass

    # Require sponsorship — select "No"
    for text in ["no", "do not require", "not require"]:
        rb = page.locator(
            f"label:has-text('sponsor') ~ div label:has-text('{text}') input, "
            f"[aria-label*='sponsor' i] ~ div label:has-text('{text}')"
        )
        if await rb.count() > 0:
            try:
                await rb.first.click(timeout=3000)
            except Exception:
                pass

    # Handle checkboxes — click any "agree" / "consent" checkboxes
    checkboxes = page.locator("input[type='checkbox']")
    count = await checkboxes.count()
    for i in range(count):
        cb = checkboxes.nth(i)
        try:
            is_checked = await cb.is_checked()
            label_el = page.locator(f"label[for='{await cb.get_attribute('id')}']")
            label_text_val = ""
            if await label_el.count() > 0:
                label_text_val = (await label_el.inner_text()).lower()
            if not is_checked and any(
                kw in label_text_val for kw in ["agree", "consent", "certify", "acknowledge"]
            ):
                await cb.click(timeout=3000)
        except Exception:
            pass

    # Last pass: handle employer-specific questions from nearby label/context.
    await _fill_remaining_text_controls(page, profile)
    await _choose_remaining_radios(page, profile)
    await _choose_remaining_selects(page, profile)

    return True


async def _upload_resume(page: Page, resume_path: Path) -> bool:
    """Upload the tailored resume .docx to the Easy Apply form."""
    try:
        upload_input = page.locator("input[type='file']")
        if await upload_input.count() > 0:
            await upload_input.first.set_input_files(str(resume_path))
            await asyncio.sleep(2.0)
            console.print("  [green]Resume uploaded[/green]")
            return True
        return False
    except Exception as e:
        console.print(f"  [yellow]Resume upload skipped: {e}[/yellow]")
        return False


async def _advance_or_submit(page: Page) -> str:
    """
    Click Next/Review/Submit button.
    Returns: 'next' | 'submit' | 'review' | 'error'
    """
    for selector, action in [
        ('button:has-text("Submit application")', "submit"),
        ('button:has-text("Submit")', "submit"),
        ('button:has-text("Continue applying")', "next"),
        ('a:has-text("Continue applying")', "next"),
        ('button:has-text("Review")', "review"),
        ('button:has-text("Next")', "next"),
        ('button:has-text("Continue")', "next"),
    ]:
        if await _click_first_visible(page, selector, timeout=5000):
            await asyncio.sleep(random.uniform(1.5, 3.0))
            return action
    return "error"


async def _easy_apply_step_signature(page: Page) -> str:
    """Return visible modal text so we can detect an advance button that is not advancing."""
    try:
        return await page.evaluate(
            """
            () => {
              const dialog = document.querySelector('[role="dialog"]')
                || document.querySelector('.jobs-easy-apply-modal')
                || document.querySelector('form');
              return (dialog?.innerText || document.body.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 2000);
            }
            """
        )
    except Exception:
        return ""


def _next_step_is_repeating(action: str, signature: str, previous_signature: str, repeat_count: int) -> tuple[str, int]:
    if action != "next":
        return "", 0
    if signature and signature == previous_signature:
        return signature, repeat_count + 1
    return signature, 0


async def easy_apply(
    page: Page,
    job: Job,
    profile: dict,
    resume_path: Path,
    config: dict,
) -> bool:
    """
    Execute the full Easy Apply flow for a job.
    Returns True if application was successfully submitted.
    """
    min_d = config["browser"]["min_delay"]
    max_d = config["browser"]["max_delay"]

    console.print(f"  [cyan]Applying to:[/cyan] {job.title} @ {job.company}")

    try:
        # Click "Easy Apply" button
        clicked = await _open_easy_apply_modal(
            page,
            job.apply_url,
            EASY_APPLY_BUTTON_SELECTOR,
            min_d,
            max_d,
        )
        if not clicked:
            console.print(f"  [yellow]No Easy Apply button found for {job.job_id}[/yellow]")
            return False
        await asyncio.sleep(random.uniform(2.0, 3.5))

        # Multi-step form loop
        max_steps = 25
        steps_taken = 0
        resume_uploaded = False
        review_repeats = 0
        next_signature = ""
        next_repeats = 0

        while steps_taken < max_steps:
            steps_taken += 1

            # Check for file upload on this step
            if not resume_uploaded and resume_path.exists():
                uploaded = await _upload_resume(page, resume_path)
                if uploaded:
                    resume_uploaded = True
                await asyncio.sleep(1.0)

            # Fill form fields
            await _handle_form_step(page, profile)

            # Advance or submit
            action = await _advance_or_submit(page)
            console.print(f"  Step {steps_taken}: {action}")
            step_signature = await _easy_apply_step_signature(page)
            next_signature, next_repeats = _next_step_is_repeating(
                action,
                step_signature,
                next_signature,
                next_repeats,
            )

            if action == "submit":
                # Confirm submission
                await asyncio.sleep(2.0)
                # Check for success confirmation
                success_el = page.locator(
                    ':has-text("application was sent"), :has-text("Application submitted"), '
                    ':has-text("applied to"), .artdeco-toast--success'
                )
                if await success_el.count() > 0:
                    console.print(f"  [bold green]Applied![/bold green] {job.title} @ {job.company}")
                    # Dismiss confirmation dialog
                    await _safe_click(page, 'button[aria-label="Dismiss"]', timeout=3000)
                    return True
                # Sometimes submit leads to a review page — try once more
                final = await _advance_or_submit(page)
                if final == "submit":
                    await asyncio.sleep(2.0)
                return True

            elif action == "review":
                review_repeats += 1
                if review_repeats >= 3:
                    console.print("  [yellow]Review step repeated; required employer question likely needs manual input[/yellow]")
                    break
                continue

            elif action == "next" and next_repeats >= 3:
                console.print("  [yellow]Next step repeated without progress; required employer question likely needs manual input[/yellow]")
                break

            elif action == "error":
                console.print(f"  [red]No advance button found at step {steps_taken}[/red]")
                break

            else:
                review_repeats = 0

            await asyncio.sleep(random.uniform(min_d, max_d))

        console.print(f"  [red]Easy Apply did not complete for {job.job_id}[/red]")
        # Dismiss modal to avoid blocking the browser
        await _safe_click(page, 'button[aria-label="Dismiss"]', timeout=3000)
        return False

    except Exception as e:
        console.print(f"  [red]Apply error for {job.job_id}: {e}[/red]")
        try:
            await _safe_click(page, 'button[aria-label="Dismiss"]', timeout=2000)
        except Exception:
            pass
        return False

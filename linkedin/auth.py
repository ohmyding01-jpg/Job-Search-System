"""
LinkedIn session management.
First run: opens a visible browser, user logs in manually, cookies saved.
Later runs: restores cookies headlessly.

Dice and CareerBuilder sessions are also validated and refreshed here so the
apply cycle never blocks on an expired session.
"""

import asyncio
import json
import os
from pathlib import Path
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from rich.console import Console

console = Console()

_CONTEXT_OPTS = {
    "user_agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "locale": "en-US",
    "timezone_id": "America/New_York",
}

_STEALTH_SCRIPT = """
    Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
    Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3] });
    Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
"""


async def _make_browser(playwright, headless: bool, config: dict):
    profile_dir = Path(config["browser"].get("profile_dir") or Path(config["browser"]["cookies_file"]).parent / "playwright-profile")
    profile_dir.mkdir(parents=True, exist_ok=True)

    # Remove stale Chrome lock files left by crashed browser instances.
    for lock_name in ("SingletonLock", "SingletonSocket", "SingletonCookie"):
        lock = profile_dir / lock_name
        if lock.exists():
            try:
                lock.unlink()
            except Exception:
                pass

    chromium_args = [
        "--no-sandbox",
        "--disable-blink-features=AutomationControlled",
        "--disable-infobars",
        "--disable-dev-shm-usage",
    ]
    if headless:
        # Force the modern headless mode — truly invisible, no dock icon, no flicker.
        chromium_args.append("--headless=new")

    context = await playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_dir),
        headless=headless,
        viewport={"width": config["browser"]["width"], "height": config["browser"]["height"]},
        **_CONTEXT_OPTS,
        args=chromium_args,
    )
    await context.add_init_script(_STEALTH_SCRIPT)
    page = context.pages[0] if context.pages else await context.new_page()
    return context.browser, context, page


def _on_feed(url: str) -> bool:
    return any(x in url for x in ["/feed", "/mynetwork", "/jobs", "/messaging"])


async def load_or_create_session(config: dict) -> tuple[Browser, BrowserContext, Page]:
    cookies_path = Path(config["browser"]["cookies_file"])
    cookies_path.parent.mkdir(parents=True, exist_ok=True)

    playwright = await async_playwright().start()

    # Restore session — use config headless setting so Dice/CB login prompts are visible.
    _headless = bool(config.get("browser", {}).get("headless", False))
    if cookies_path.exists():
        browser, context, page = await _make_browser(playwright, headless=_headless, config=config)
        cookies = json.loads(cookies_path.read_text())
        await context.add_cookies(cookies)
        console.print("[green]Session restored from cookies[/green]")

        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(2)

        if _on_feed(page.url):
            console.print("[green]LinkedIn session valid[/green]")
            return browser, context, page

        console.print("[yellow]Session expired — need to log in again[/yellow]")
        await browser.close()

    # Manual login — open a visible browser
    browser, context, page = await _make_browser(playwright, headless=False, config=config)
    await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)

    console.print("")
    console.print("[bold yellow]ACTION REQUIRED[/bold yellow]")
    console.print("[yellow]A browser window has opened. Log in to LinkedIn normally.[/yellow]")
    console.print("[yellow]The script will continue automatically once you reach the feed.[/yellow]")
    console.print("")

    # Poll until the user reaches the feed.
    # Allow a longer, configurable window for 2FA or manual verification steps.
    login_timeout_minutes = int(config.get("browser", {}).get("login_timeout_minutes", 20))
    if login_timeout_minutes <= 0:
        while True:
            await asyncio.sleep(1)
            if _on_feed(page.url):
                break
    else:
        timeout_seconds = max(60, login_timeout_minutes * 60)
        for _ in range(timeout_seconds):
            await asyncio.sleep(1)
            if _on_feed(page.url):
                break
        else:
            raise RuntimeError(
                f"Timed out waiting for LinkedIn login ({login_timeout_minutes} min). Please try again."
            )

    await asyncio.sleep(2)
    cookies = await context.cookies()
    cookies_path.write_text(json.dumps(cookies, indent=2))
    console.print(f"[green]Logged in — session saved to {cookies_path}[/green]")

    return browser, context, page


async def save_cookies(context: BrowserContext, config: dict):
    cookies_path = Path(config["browser"]["cookies_file"])
    cookies = await context.cookies()
    cookies_path.write_text(json.dumps(cookies, indent=2))


# ─── Multi-site session validator ────────────────────────────────────────────

async def _is_dice_logged_in(page: Page) -> bool:
    """Navigate to Dice and check whether the session is still active."""
    try:
        await page.goto("https://www.dice.com/dashboard", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1)
        url = page.url.lower()
        # Logged in → stays on dashboard; logged out → redirected to login
        return "dashboard/login" not in url and "sign-in" not in url and "login" not in url
    except Exception:
        return False


async def _is_careerbuilder_logged_in(page: Page) -> bool:
    """Navigate to CareerBuilder profile and check session status."""
    try:
        await page.goto("https://www.careerbuilder.com/profile/detail", wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1)
        url = page.url.lower()
        return "sign-in" not in url and "identity.monster.com" not in url and "login" not in url
    except Exception:
        return False


async def _auto_login(page: Page, *, site: str, login_url: str,
                      email_env: str, password_env: str,
                      email_selector: str, password_selector: str,
                      submit_selector: str, success_check) -> bool:
    """Silently log in using credentials from .env. Returns True if successful."""
    email = os.getenv(email_env, "").strip()
    password = os.getenv(password_env, "").strip()
    if not email or not password:
        return False
    try:
        await page.goto(login_url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(1)
        await page.fill(email_selector, email)
        await asyncio.sleep(0.5)
        await page.fill(password_selector, password)
        await asyncio.sleep(0.5)
        await page.click(submit_selector)
        await asyncio.sleep(3)
        if await success_check(page):
            console.print(f"  [green]{site} auto-logged in via credentials ✓[/green]")
            return True
        console.print(f"  [yellow]{site} auto-login may have failed — check credentials in .env[/yellow]")
        return False
    except Exception as e:
        console.print(f"  [yellow]{site} auto-login error: {e}[/yellow]")
        return False


async def _dice_success(page: Page) -> bool:
    url = page.url.lower()
    return "dashboard/login" not in url and "login" not in url


async def _cb_success(page: Page) -> bool:
    url = page.url.lower()
    return "sign-in" not in url and "identity.monster.com" not in url


async def validate_and_refresh_sessions(context: BrowserContext) -> dict[str, bool]:
    """
    Check Dice and CareerBuilder session health and silently re-login if needed.
    Returns {'dice': bool, 'careerbuilder': bool} indicating whether each is logged in.

    Uses DICE_EMAIL / DICE_PASSWORD and CAREERBUILDER_EMAIL / CAREERBUILDER_PASSWORD
    from .env for silent re-login. If credentials are absent the site is skipped
    gracefully — no blocking prompt, no crash.
    """
    status: dict[str, bool] = {}

    # Use a scratch page so we don't disrupt the main LinkedIn page.
    page = await context.new_page()
    try:
        # ── Dice ─────────────────────────────────────────────────────────────
        dice_ok = await _is_dice_logged_in(page)
        if not dice_ok:
            console.print("  [yellow]Dice session expired — attempting auto-login…[/yellow]")
            dice_ok = await _auto_login(
                page, site="Dice",
                login_url="https://www.dice.com/dashboard/login",
                email_env="DICE_EMAIL", password_env="DICE_PASSWORD",
                email_selector='input[type="email"], input[name="email"], #email',
                password_selector='input[type="password"], input[name="password"]',
                submit_selector='button[type="submit"], button:has-text("Sign in")',
                success_check=_dice_success,
            )
        status["dice"] = dice_ok
        if dice_ok:
            console.print("  [dim]Dice session ✓[/dim]")

        # ── CareerBuilder ─────────────────────────────────────────────────────
        cb_ok = await _is_careerbuilder_logged_in(page)
        if not cb_ok:
            console.print("  [yellow]CareerBuilder session expired — attempting auto-login…[/yellow]")
            cb_ok = await _auto_login(
                page, site="CareerBuilder",
                login_url="https://www.careerbuilder.com/profile/sign-in",
                email_env="CAREERBUILDER_EMAIL", password_env="CAREERBUILDER_PASSWORD",
                email_selector='input[type="email"], input[name="email"]',
                password_selector='input[type="password"]',
                submit_selector='button[type="submit"], button:has-text("Sign in")',
                success_check=_cb_success,
            )
        status["careerbuilder"] = cb_ok
        if cb_ok:
            console.print("  [dim]CareerBuilder session ✓[/dim]")

    finally:
        try:
            await page.close()
        except Exception:
            pass

    return status

"""
Stephen's Job Agent — one-click launcher.

Run this once:
    python3 start.py

It will:
1. Start the web dashboard on http://localhost:8080
2. Open your browser automatically
3. You click START in the dashboard to begin job hunting

First time? Enter your free Google AI key in the dashboard.
Get one at: https://aistudio.google.com/apikey (free, no credit card)
"""

import os
import sys
import time
import threading
import webbrowser
import subprocess
from pathlib import Path

BASE = Path(__file__).parent
PORT = 8080
URL = f"http://localhost:{PORT}"


def print_banner():
    print()
    print("=" * 60)
    print("  Job Agent — Stephen Muliokela")
    print("=" * 60)
    print(f"  Dashboard: {URL}")
    print("  Opening in browser... (Ctrl+C to stop)")
    print("=" * 60)
    print()


def load_env():
    env_file = BASE / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, _, v = line.partition("=")
                key = k.strip()
                val = v.strip().strip('"').strip("'")
                if key and val and key not in os.environ:
                    os.environ[key] = val


def open_browser_after_delay():
    time.sleep(2)
    webbrowser.open(URL)


def check_requirements():
    missing = []
    try:
        import fastapi
        import uvicorn
        import playwright
    except ImportError as e:
        missing.append(str(e))
    if missing:
        print("Missing packages. Installing...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q",
                               "fastapi", "uvicorn[standard]", "playwright"])
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])
        print("Dependencies installed.")


def configure_ai_provider():
    """Auto-detect and configure the AI provider from .env or environment."""
    gemini_key = os.environ.get("GEMINI_API_KEY", "").strip()
    openai_key = os.environ.get("OPENAI_API_KEY", "").strip()
    openai_base = os.environ.get("OPENAI_BASE_URL", "").strip()

    if gemini_key:
        # Update config.yaml to use Gemini directly (Stephen's free key)
        cfg = BASE / "candidates" / "stephen" / "config.yaml"
        if cfg.exists():
            text = cfg.read_text()
            if 'provider: "openai"' in text and 'ag/gemini-3-flash' in text:
                text = text.replace('provider: "openai"\n  model: "ag/gemini-3-flash"',
                                    'provider: "gemini"\n  model: "gemini-2.0-flash-lite"')
                cfg.write_text(text)
        print("  AI: Google Gemini (free tier) ✓")
    elif openai_base and "localhost" in openai_base:
        print(f"  AI: Local proxy at {openai_base} ✓")
    elif openai_key:
        print("  AI: OpenAI ✓")
    else:
        print("  AI: ⚠  No API key found. Add one in the dashboard to enable AI scoring.")


if __name__ == "__main__":
    os.chdir(BASE)
    load_env()
    check_requirements()
    configure_ai_provider()
    print_banner()

    threading.Thread(target=open_browser_after_delay, daemon=True).start()

    try:
        import uvicorn
        from dashboard import app
        uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="warning")
    except KeyboardInterrupt:
        print("\nDashboard stopped.")

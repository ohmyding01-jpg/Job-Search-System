"""
Open a visible LinkedIn login window and save the session cookie.

Use this during the setup meeting so Samiha can log in herself without
starting a full job scan.
"""

import asyncio
import yaml
from pathlib import Path

from linkedin.auth import load_or_create_session, save_cookies


def load_config() -> dict:
    with open(Path(__file__).parent / "config.yaml") as f:
        return yaml.safe_load(f)


async def main():
    config = load_config()
    browser, context, _page = await load_or_create_session(config)
    await save_cookies(context, config)
    await context.close()
    await browser.close()
    print("LinkedIn session saved. You can now start the 24/7 agent.")


if __name__ == "__main__":
    asyncio.run(main())

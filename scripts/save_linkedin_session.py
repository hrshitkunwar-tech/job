#!/usr/bin/env python3
from __future__ import annotations

import asyncio
from pathlib import Path

from playwright.async_api import async_playwright


STATE_PATH = Path("data/browser_state/linkedin_state.json")


async def main():
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)

    print("Opening LinkedIn login in a visible browser...")
    print("1) Sign in to LinkedIn")
    print("2) Open any LinkedIn jobs page to confirm session")
    print("3) Return here and press Enter")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
        input("\nPress Enter after you are fully logged in... ")
        await context.storage_state(path=str(STATE_PATH))
        await browser.close()

    print(f"Saved LinkedIn session state to: {STATE_PATH}")
    print("Automation will now reuse this session for LinkedIn apply flows.")


if __name__ == "__main__":
    asyncio.run(main())

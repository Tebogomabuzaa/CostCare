"""Capture screenshots of the deployed CostCare site for the README.

Uses Playwright with the Microsoft Edge (or Chrome) already installed on this machine,
so no browser download is needed:

  pip install playwright
  python deploy/screenshots.py https://<api-id>.execute-api.af-south-1.amazonaws.com/
"""

import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

OUT = Path(__file__).resolve().parent.parent / "docs" / "screenshots"

DESKTOP_PAGES = [
    ("01-home.png", "", None),
    ("02-ai-search.png", "find?q=cheap%20root%20canal%20in%20Cape%20Town%20under%20R8000", "#results .result-card:not(.skeleton-card)"),
    ("03-search-mri.png", "find?q=MRI%20scan", "#results .result-card:not(.skeleton-card)"),
    ("05-about-partners.png", "about", None),
    ("06-api-docs.png", "docs", ".swagger-ui .opblock"),
]


def capture(base_url: str, channel: str = "msedge") -> list[Path]:
    base_url = base_url.rstrip("/") + "/"
    OUT.mkdir(parents=True, exist_ok=True)
    saved = []
    with sync_playwright() as p:
        browser = p.chromium.launch(channel=channel, headless=True)

        desktop = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        page = desktop.new_page()
        for name, path, wait_for in DESKTOP_PAGES:
            page.goto(base_url + path, wait_until="networkidle")
            if wait_for:
                page.wait_for_selector(wait_for, timeout=20000)
                page.wait_for_timeout(800)  # let card animations and map tiles settle
            page.screenshot(path=OUT / name)
            saved.append(OUT / name)

        # Provider page: open the first result from a search
        page.goto(base_url + "find?q=MRI%20scan", wait_until="networkidle")
        page.wait_for_selector("#results .rc-provider", timeout=20000)
        page.click("#results .rc-provider")
        page.wait_for_load_state("networkidle")
        page.wait_for_timeout(800)
        page.screenshot(path=OUT / "04-provider-page.png")
        saved.append(OUT / "04-provider-page.png")

        mobile = browser.new_context(viewport={"width": 390, "height": 844}, device_scale_factor=2,
                                     is_mobile=True, has_touch=True)
        mpage = mobile.new_page()
        mpage.goto(base_url + "find?q=GP%20consultation%20in%20Johannesburg", wait_until="networkidle")
        mpage.wait_for_selector("#results .result-card:not(.skeleton-card)", timeout=20000)
        mpage.wait_for_timeout(800)
        mpage.screenshot(path=OUT / "07-mobile-search.png")
        saved.append(OUT / "07-mobile-search.png")

        browser.close()
    return saved


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("Usage: python deploy/screenshots.py <website-url> [msedge|chrome]")
    for path in capture(sys.argv[1], *(sys.argv[2:3] or [])):
        print(f"saved {path}")

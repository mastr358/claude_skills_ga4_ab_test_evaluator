#!/usr/bin/env python3
"""
Screenshot HTML AB Test Report using Playwright.

Takes an ab_test_overview.html file and produces a PNG screenshot.
Saves the PNG alongside the HTML file (or to a custom output path).

USAGE:
    python screenshot_html_report.py /path/to/ab_test_overview.html
    python screenshot_html_report.py /path/to/ab_test_overview.html --output /path/to/screenshot.png

REQUIREMENTS:
    pip install playwright
    playwright install chromium
"""

import argparse
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright


# Viewport width matching the HTML's max-width (1200px) + padding
VIEWPORT_WIDTH = 1280
# Initial height — actual screenshot uses full_page=True
VIEWPORT_HEIGHT = 900


def screenshot_html(html_path: Path, output_path: Path) -> Path:
    """Take a full-page screenshot of an HTML file and save as PNG."""
    if not html_path.exists():
        print(f"ERROR: HTML file not found: {html_path}", file=sys.stderr)
        sys.exit(1)

    file_url = html_path.resolve().as_uri()

    with sync_playwright() as p:
        # Use --headless=new for Chrome's native headless mode (no visible window on macOS)
        browser = p.chromium.launch(
            headless=True,
            args=['--headless=new', '--disable-gpu', '--no-sandbox'],
        )
        page = browser.new_page(viewport={"width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT})
        page.goto(file_url, wait_until="networkidle")
        # Wait for any fonts/images to load
        page.wait_for_timeout(500)
        page.screenshot(path=str(output_path), full_page=True)
        browser.close()

    return output_path


def main():
    parser = argparse.ArgumentParser(description="Screenshot HTML AB test report to PNG")
    parser.add_argument("html_file", type=str, help="Path to ab_test_overview.html")
    parser.add_argument("--output", "-o", type=str, default=None,
                        help="Output PNG path (default: same dir as HTML, named ab_test_overview.png)")
    args = parser.parse_args()

    html_path = Path(args.html_file)

    if args.output:
        output_path = Path(args.output)
    else:
        output_path = html_path.with_suffix(".png")

    result = screenshot_html(html_path, output_path)
    print(f"Screenshot saved: {result}")


if __name__ == "__main__":
    main()

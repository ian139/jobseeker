from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class LiveSmokeResult:
    status: Literal["ready", "unavailable", "failed"]
    message: str


def check_playwright_available() -> LiveSmokeResult:
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ModuleNotFoundError:
        return LiveSmokeResult("unavailable", "Playwright is not installed; install with `pip install -e .[live]` and run `python -m playwright install chromium`.")

    try:
        with sync_playwright() as playwright:
            browser_type = playwright.chromium
            name = browser_type.name
    except Exception as exc:
        return LiveSmokeResult("failed", f"Playwright import succeeded but runtime smoke failed: {exc}")
    return LiveSmokeResult("ready", f"Playwright {name} adapter is importable; run live dry-runs only against approved targets.")

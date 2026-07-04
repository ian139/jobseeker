from __future__ import annotations

from pathlib import Path

import yaml

from job_scraper.outreach.models import OutreachConfig


def load_outreach_config(path: Path) -> OutreachConfig:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return OutreachConfig.model_validate(data)

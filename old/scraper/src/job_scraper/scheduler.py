from __future__ import annotations

from collections.abc import Callable

from apscheduler.schedulers.blocking import BlockingScheduler

from job_scraper.config import ScraperConfig
from job_scraper.storage import JobStorage
from job_scraper.sync import SearchClient, SyncSummary, sync_once


def run_daemon(
    client: SearchClient,
    storage: JobStorage,
    config: ScraperConfig,
    on_summary: Callable[[SyncSummary], None] | None = None,
) -> None:
    first_summary = sync_once(client, storage, config)
    if on_summary is not None:
        on_summary(first_summary)

    scheduler = BlockingScheduler()

    def scheduled_sync() -> None:
        summary = sync_once(client, storage, config)
        if on_summary is not None:
            on_summary(summary)

    scheduler.add_job(scheduled_sync, "interval", hours=24, id="theirstack-sync", replace_existing=True)
    scheduler.start()

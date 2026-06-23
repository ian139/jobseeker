from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class AppSettings(BaseSettings):
    theirstack_api_key: str = Field(default="", alias="THEIRSTACK_API_KEY")
    job_scraper_db_path: Path = Field(default=Path("data/jobs.sqlite3"), alias="JOB_SCRAPER_DB_PATH")
    openai_api_key: str = Field(default="", alias="OPENAI_API_KEY")
    openai_model: str = Field(default="gpt-4o-mini", alias="OPENAI_MODEL")
    application_pack_dir: Path = Field(default=Path("data/application_packs"), alias="APPLICATION_PACK_DIR")

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


class OrderBy(BaseModel):
    field: str
    desc: bool = True


class SearchConfig(BaseModel):
    posted_at_max_age_days: int = 1
    discovered_overlap_minutes: int = 10
    limit: int = 100
    max_pages: int = 5
    include_total_results: bool = False
    order_by: list[OrderBy] = Field(default_factory=list)


class FilterConfig(BaseModel):
    model_config = ConfigDict(extra="allow")

    is_closed: bool | None = None
    company_type: str | None = None
    company_description_pattern_or: list[str] = Field(default_factory=list)
    job_title_pattern_or: list[str] = Field(default_factory=list)
    job_title_not: list[str] = Field(default_factory=list)
    job_description_pattern_or: list[str] = Field(default_factory=list)
    job_description_pattern_not: list[str] = Field(default_factory=list)
    employment_statuses_or: list[str] = Field(default_factory=list)
    job_country_code_or: list[str] = Field(default_factory=list)
    remote: bool | None = None
    job_seniority_or: list[str] = Field(default_factory=list)
    min_salary_usd: float | None = None
    url_domain_or: list[str] = Field(default_factory=list)


class ScraperConfig(BaseModel):
    search: SearchConfig
    filters: FilterConfig


def load_config(path: Path) -> ScraperConfig:
    with path.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return ScraperConfig.model_validate(data)


def _compact(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _compact(value.model_dump())
    if isinstance(value, list):
        return [_compact(item) for item in value]
    if isinstance(value, dict):
        return {key: _compact(item) for key, item in value.items()}
    return value


def _drop_empty(payload: dict[str, Any]) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key, value in payload.items():
        if value is None:
            continue
        if isinstance(value, list) and not value:
            continue
        cleaned[key] = value
    return cleaned


def build_search_payload(
    config: ScraperConfig,
    *,
    page: int,
    discovered_at_gte: str | None = None,
    preview_count: bool = False,
) -> dict[str, object]:
    payload: dict[str, Any] = config.search.model_dump(exclude={"discovered_overlap_minutes", "max_pages"})
    payload.update(config.filters.model_dump())
    payload["page"] = page
    if discovered_at_gte:
        payload["discovered_at_gte"] = discovered_at_gte
    if preview_count:
        payload["blur_company_data"] = True
        payload["include_total_results"] = True
        payload["limit"] = 1
    return _drop_empty(_compact(payload))


COMPANY_IDENTIFIER_FILTER_PREFIXES = (
    "company_domain",
    "company_id",
    "company_linkedin_url",
    "company_name",
)


def has_company_identifier_filters(config: ScraperConfig) -> bool:
    filter_data = config.filters.model_dump()
    for key in filter_data:
        if not key.startswith(COMPANY_IDENTIFIER_FILTER_PREFIXES):
            continue
        value = filter_data.get(key)
        if value not in (None, [], ""):
            return True
    return False

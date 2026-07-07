from pathlib import Path


def test_env_defaults_cover_active_ingestion_config():
    content = Path(".env.example").read_text()
    assert "DATABASE_URL=data/jobs.sqlite3" in content
    assert "JOB_SOURCE_BASE_URL=" in content
    assert "JOB_SOURCE_API_KEY=" in content

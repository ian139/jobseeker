from jobs_assistant.live_smoke import check_playwright_available


def test_live_smoke_is_guarded_when_playwright_missing_or_ready():
    result = check_playwright_available()
    assert result.status in {"ready", "unavailable", "failed"}
    if result.status == "unavailable":
        assert "pip install -e .[live]" in result.message

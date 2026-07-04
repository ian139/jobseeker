from pathlib import Path


def test_env_default_resume_points_to_archive():
    content = Path(".env.example").read_text()
    assert "RESUME_PATH=archive/old-applier/data/Main_Resume.pdf" in content

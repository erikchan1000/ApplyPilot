"""Shared fixtures for scoring tests."""

from pathlib import Path

import pytest

SAMPLE_DIR = Path.home() / ".applypilot" / "tailored_resumes"


@pytest.fixture
def sample_resume_text() -> str:
    """Load the BMO Software Developer resume as a representative sample."""
    path = SAMPLE_DIR / "BMO_Software_Developer.txt"
    if not path.exists():
        pytest.skip(f"Sample resume not found at {path}")
    return path.read_text(encoding="utf-8")


@pytest.fixture
def alt_resume_text() -> str:
    """Load the Full Stack Engineer New Grad resume as an alternate sample."""
    path = SAMPLE_DIR / "linkedin_Full_Stack_Engineer_New_Grad.txt"
    if not path.exists():
        pytest.skip(f"Sample resume not found at {path}")
    return path.read_text(encoding="utf-8")


@pytest.fixture
def minimal_resume_text() -> str:
    """A minimal hand-crafted resume for controlled testing."""
    return (
        "Jane Doe\n"
        "Backend Engineer\n"
        "jane@example.com | 5551234567 | https://github.com/janedoe\n"
        "\n"
        "TECHNICAL SKILLS\n"
        "Languages: Python, Go, SQL\n"
        "Frameworks: Flask, FastAPI\n"
        "\n"
        "EXPERIENCE\n"
        "Software Engineer at Acme Corp\n"
        "Python, Flask, PostgreSQL | Jan 2023 - Present\n"
        "- Built REST APIs serving 10k req/s.\n"
        "- Migrated legacy monolith to microservices.\n"
        "\n"
        "Junior Developer at StartupCo\n"
        "Node.js, React | Jun 2021 - Dec 2022\n"
        "- Developed user dashboard with React.\n"
        "\n"
        "PROJECTS\n"
        "Open Source CLI Tool - Developer productivity utility\n"
        "Rust | N/A\n"
        "- Built a CLI that speeds up local dev workflows.\n"
        "\n"
        "EDUCATION\n"
        "MIT | Bachelor's in Computer Science"
    )

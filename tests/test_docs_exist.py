from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_architecture_doc_exists() -> None:
    assert (REPO_ROOT / "docs" / "architecture.md").is_file()


def test_env_example_exists() -> None:
    assert (REPO_ROOT / ".env.example").is_file()


def test_makefile_exists() -> None:
    assert (REPO_ROOT / "Makefile").is_file()

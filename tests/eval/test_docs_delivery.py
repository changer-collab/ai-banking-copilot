from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS = REPO_ROOT / "docs"


def _read(name: str) -> str:
    return (DOCS / name).read_text(encoding="utf-8")


def test_architecture_doc_covers_pipeline_and_layers() -> None:
    text = _read("architecture.md")
    assert "规划内核" in text and "可信执行内核" in text and "事实网关" in text
    assert "评测" in text


def test_algorithms_doc_covers_core_algorithms() -> None:
    text = _read("algorithms.md")
    for keyword in ("三色", "净效应", "锚点", "执行门", "哈希链", "白名单"):
        assert keyword in text


def test_security_design_doc_covers_official_mechanisms() -> None:
    text = _read("security-design.md")
    for keyword in ("权限分级", "注入防御", "幻觉防护", "操作审计", "异常熔断", "沙箱"):
        assert keyword in text


def test_security_self_assessment_has_two_required_sections() -> None:
    text = _read("security-self-assessment.md")
    assert "权限分级实现说明" in text
    assert "已知风险清单" in text


def test_intro_is_within_two_hundred_characters() -> None:
    body = _read("作品简介.md")
    stripped = "".join(body.split())
    assert len(stripped) <= 200
    assert "AI银行副驾" in stripped


def test_deploy_readme_documents_tests_and_eval() -> None:
    text = (REPO_ROOT / "deploy" / "README.md").read_text(encoding="utf-8")
    assert "pytest" in text
    assert "eval" in text


def test_third_party_notices_are_registered() -> None:
    text = (REPO_ROOT / "THIRD_PARTY_NOTICES.md").read_text(encoding="utf-8")
    assert "待登记" not in text
    for package in ("pytest", "ruff", "mypy", "fastapi", "uvicorn", "httpx"):
        assert package in text

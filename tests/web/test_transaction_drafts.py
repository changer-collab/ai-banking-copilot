from fastapi.testclient import TestClient

from ai_banking_copilot.web.app import AppDeps, create_app


def _client(deps: AppDeps) -> TestClient:
    return TestClient(create_app(deps))


def test_transfer_draft_patch_regenerates_the_frozen_plan(deps: AppDeps) -> None:
    client = _client(deps)
    submitted = client.post(
        "/api/submit", json={"utterance": "\u8f6c\u7ed9\u674e\u5a1c 500 \u5143"}
    ).json()

    draft = submitted["draft"]
    assert draft["kind"] == "transfer"
    assert {"amount", "remark"}.issubset(draft["editable_fields"])
    fields = {field["key"]: field for field in draft["fields"]}
    assert fields["payee"]["value"] == "p_lina"
    assert fields["payee"]["display"].startswith("李娜")
    assert "p_lina" not in fields["payee"]["display"]
    assert fields["payee"]["editable"] is False
    original_hash = submitted["card"]["frozen_hash"]

    patched = client.patch(
        f"/api/drafts/{draft['draft_id']}",
        json={"changes": {"amount": "600\u5143", "remark": "\u623f\u79df"}},
    )

    assert patched.status_code == 200
    body = patched.json()
    assert body["state"] == "awaiting_confirm"
    assert body["card"]["frozen_hash"] != original_hash
    assert body["draft"]["origin_plan_hash"] == original_hash
    assert body["review"]["values"]["amount"] == "600.00"


def test_draft_patch_rejects_internal_identifier_injection(deps: AppDeps) -> None:
    client = _client(deps)
    submitted = client.post(
        "/api/submit", json={"utterance": "\u8f6c\u7ed9\u674e\u5a1c 500 \u5143"}
    ).json()

    response = client.patch(
        f"/api/drafts/{submitted['draft']['draft_id']}",
        json={"changes": {"payee_id": "p_someone_else"}},
    )

    assert response.status_code == 422


def test_subscription_draft_discloses_the_bank_side_boundary(deps: AppDeps) -> None:
    client = _client(deps)
    submitted = client.post(
        "/api/submit",
        json={
            "utterance": "\u5173\u95ed\u6263\u6b3e\u534f\u8bae",
            "selected": {"subscription": "sub_video"},
        },
    ).json()

    draft = submitted["draft"]
    assert draft["kind"] == "subscription"
    assert any("\u672c\u884c\u4fa7" in line for line in draft["disclosures"])


def test_birthday_draft_keeps_budget_separate_from_order_amount(deps: AppDeps) -> None:
    client = _client(deps)
    submitted = client.post(
        "/api/submit",
        json={
            "utterance": (
                "\u7231\u4eba\u751f\u65e5\u9884\u7b97800\u5143\uff0c\u8ba2299\u5143\u9c9c\u82b1"
                "\uff0c\u5546\u6237 \u82b1\u95f4\u96c6\uff08\u897f\u6e56\u5e97\uff09"
                "\uff0c\u9001\u8fbe\u65f6\u95f4 2026-11-04"
            )
        },
    ).json()

    draft = submitted["draft"]
    assert draft["kind"] == "life_event"
    fields = {field["key"]: field for field in draft["fields"]}
    assert fields["budget_limit"]["value"] == "800.00"
    assert fields["order_amount"]["value"] == "299.00"

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import app as modal_app
from app import VendorDecision, process_expenses


def history(expense_id: int, transaction_id: int) -> dict[str, Any]:
    return {
        "expenseid": expense_id,
        "vendor": "Acme Co",
        "categoryid": 10,
        "transactionid": transaction_id,
        "bank_name": "Checking",
        "notes": "ACME CO",
    }


def imported(expense_id: int) -> dict[str, Any]:
    return {
        "expenseid": expense_id,
        "categoryid": 999,
        "transactionid": expense_id + 100,
        "bank_name": "Checking",
        "notes": "SQ * ACME CO 1234",
    }


def accept_top(descriptor: str, candidates: list[str]) -> VendorDecision:
    return VendorDecision(vendor=candidates[0])


def test_unmatched_bank_expense_is_routed_from_matched_history() -> None:
    expenses = [history(1, 101), history(2, 102), history(3, 103), imported(4)]
    updates: list[tuple[int, dict[str, Any]]] = []

    def update(expense_id: int, fields: dict[str, Any]) -> dict[str, Any]:
        updates.append((expense_id, fields))
        return {"vendor": fields["vendor"], "categoryid": fields["categoryid"]}

    summary = process_expenses(
        expenses,
        {4},
        {1, 2, 3},
        accept_top,
        update,
        dry_run=False,
    )

    assert updates == [(4, {"vendor": "Acme Co", "categoryid": 10})]
    assert summary["applied"] == 1
    assert summary["candidates"] == 1


def test_matched_expenses_are_not_processed() -> None:
    expenses = [history(1, 101), history(2, 102), history(3, 103), imported(4)]
    updates: list[dict[str, Any]] = []

    summary = process_expenses(
        expenses,
        set(),
        {1, 2, 3, 4},
        accept_top,
        lambda _, fields: updates.append(fields) or fields,
        dry_run=False,
    )

    assert updates == []
    assert summary == {"pulled": 4, "candidates": 0}


def test_dry_run_does_not_update_the_import() -> None:
    expenses = [history(1, 101), history(2, 102), history(3, 103), imported(4)]
    updates: list[dict[str, Any]] = []

    summary = process_expenses(
        expenses,
        {4},
        {1, 2, 3},
        accept_top,
        lambda _, fields: updates.append(fields) or fields,
        dry_run=True,
    )

    assert updates == []
    assert summary["dry_run"] == 1


def test_transient_errors_are_retried() -> None:
    expenses = [history(1, 101), history(2, 102), history(3, 103), imported(4)]

    def fail(*_: Any) -> VendorDecision:
        raise RuntimeError("temporary")

    with pytest.raises(RuntimeError, match="1 expense operations need retrying"):
        process_expenses(
            expenses,
            {4},
            {1, 2, 3},
            fail,
            lambda *_: {},
            dry_run=False,
        )


@pytest.mark.parametrize("vendor", [None, "Unknown Vendor"])
def test_unknown_or_null_vendor_abstains(vendor: str | None) -> None:
    expenses = [history(1, 101), history(2, 102), history(3, 103), imported(4)]
    updates: list[dict[str, Any]] = []

    summary = process_expenses(
        expenses,
        {4},
        {1, 2, 3},
        lambda *_: VendorDecision(vendor=vendor),
        lambda _, fields: updates.append(fields) or fields,
        dry_run=False,
    )

    assert updates == []
    assert summary["abstained"] == 1


def test_only_reconciled_expenses_train_the_category() -> None:
    expenses = [
        history(1, 101),
        history(2, 102),
        history(3, 103),
        {**history(5, 105), "transactionid": None, "categoryid": 20},
        imported(4),
    ]
    updates: list[dict[str, Any]] = []

    process_expenses(
        expenses,
        {4},
        {1, 2, 3},
        accept_top,
        lambda _, fields: updates.append(fields) or fields,
        dry_run=False,
    )

    assert updates == [{"vendor": "Acme Co", "categoryid": 10}]


def test_most_recent_reconciled_expense_sets_the_category() -> None:
    expenses = [
        {**history(1, 101), "categoryid": 20, "updated": "2026-07-01"},
        {**history(3, 103), "categoryid": 30, "updated": "2026-06-01"},
        {**history(2, 102), "categoryid": 10, "updated": "2026-07-02"},
        imported(4),
    ]
    updates: list[dict[str, Any]] = []

    process_expenses(
        expenses,
        {4},
        {1, 2, 3},
        accept_top,
        lambda _, fields: updates.append(fields) or fields,
        dry_run=False,
    )

    assert updates == [{"vendor": "Acme Co", "categoryid": 10}]


def test_routing_log_includes_category_names(capsys: pytest.CaptureFixture[str]) -> None:
    expenses = [history(1, 101), imported(4)]

    process_expenses(
        expenses,
        {4},
        {1},
        accept_top,
        lambda _, fields: {"vendor": fields["vendor"], "categoryid": fields["categoryid"]},
        categories={10: "Meals", 999: "Uncategorized"},
        dry_run=False,
    )

    applied = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if json.loads(line)["event"] == "routing_applied"
    )
    assert applied["before"] == {"vendor": None, "categoryid": 999, "category": "Uncategorized"}
    assert applied["category"] == "Meals"
    assert applied["after"] == {"vendor": "Acme Co", "categoryid": 10, "category": "Meals"}


def test_unknown_category_id_logs_a_null_name(capsys: pytest.CaptureFixture[str]) -> None:
    expenses = [history(1, 101), imported(4)]

    process_expenses(
        expenses,
        {4},
        {1},
        accept_top,
        lambda _, fields: {"vendor": fields["vendor"], "categoryid": fields["categoryid"]},
        categories={},
        dry_run=False,
    )

    applied = next(
        json.loads(line)
        for line in capsys.readouterr().out.splitlines()
        if json.loads(line)["event"] == "routing_applied"
    )
    assert applied["category"] is None
    assert applied["after"]["category"] is None


def test_list_categories_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRESHBOOKS_ACCOUNT_ID", "account")
    paths: list[str] = []

    def fake_request(method: str, path: str, body: object = None) -> dict[str, Any]:
        paths.append(path)
        page = 2 if "page=2" in path else 1
        return {
            "response": {
                "result": {
                    "pages": 2,
                    "categories": [{"categoryid": page, "category": f"Category {page}"}],
                }
            }
        }

    monkeypatch.setattr(modal_app, "request", fake_request)

    assert modal_app.list_categories() == {1: "Category 1", 2: "Category 2"}
    assert paths[0] == "/accounting/account/account/expenses/categories?page=1&per_page=100"


def test_list_expenses_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRESHBOOKS_ACCOUNT_ID", "account")

    def fake_request(method: str, path: str, body: object = None) -> dict[str, Any]:
        page = 2 if "page=2" in path else 1
        return {
            "response": {
                "result": {
                    "pages": 2,
                    "expenses": [{"expenseid": page}],
                }
            }
        }

    monkeypatch.setattr(modal_app, "request", fake_request)

    assert [row["expenseid"] for row in modal_app.list_expenses()] == [1, 2]


def test_petty_cash_expense_ids_come_from_the_filtered_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("FRESHBOOKS_PETTY_CASH_ACCOUNT_UUID", "petty-cash")
    paths: list[str] = []

    def fake_request(method: str, path: str, body: object = None) -> dict[str, Any]:
        paths.append(path)
        return {
            "response": {
                "result": {
                    "general_ledger": {
                        "data": [
                            {
                                "sub_accounts": [
                                    {
                                        "transactions": [
                                            {"expenseid": 4},
                                            {"expenseid": None},
                                        ]
                                    }
                                ]
                            }
                        ]
                    }
                }
            }
        }

    monkeypatch.setattr(modal_app, "request", fake_request)

    assert modal_app.petty_cash_expense_ids("business", "USD") == {4}
    assert "accountid=petty-cash" in paths[0]


def test_request_refreshes_and_persists_rotated_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name, value in {
        "FRESHBOOKS_ACCESS_TOKEN": "old-access",
        "FRESHBOOKS_REFRESH_TOKEN": "old-refresh",
        "FRESHBOOKS_CLIENT_ID": "client",
        "FRESHBOOKS_CLIENT_SECRET": "secret",
        "FRESHBOOKS_REDIRECT_URI": "https://example.com",
    }.items():
        monkeypatch.setenv(name, value)

    calls: list[str] = []
    saved: list[dict[str, str]] = []

    def response(status: int, payload: dict[str, Any]) -> httpx.Response:
        return httpx.Response(status, json=payload, request=httpx.Request("GET", "https://x"))

    def fake_request(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(kwargs["headers"]["Authorization"])
        return response(401, {}) if len(calls) == 1 else response(200, {"ok": True})

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr(
        httpx,
        "post",
        lambda *args, **kwargs: response(
            200, {"access_token": "new-access", "refresh_token": "new-refresh"}
        ),
    )
    monkeypatch.setattr(modal_app.freshbooks_secret, "update", saved.append)

    assert modal_app.request("GET", "/test") == {"ok": True}
    assert calls == ["Bearer old-access", "Bearer new-access"]
    assert saved == [
        {
            "FRESHBOOKS_ACCESS_TOKEN": "new-access",
            "FRESHBOOKS_REFRESH_TOKEN": "new-refresh",
        }
    ]


def _api_response(status: int, payload: dict[str, Any] | None = None) -> httpx.Response:
    return httpx.Response(status, json=payload or {}, request=httpx.Request("GET", "https://x"))


def test_request_retries_server_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRESHBOOKS_ACCESS_TOKEN", "access")
    outcomes: list[Any] = [
        _api_response(500),
        httpx.ConnectError("reset"),
        _api_response(200, {"ok": True}),
    ]
    sleeps: list[float] = []

    def fake_request(*args: Any, **kwargs: Any) -> httpx.Response:
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr(modal_app.time, "sleep", sleeps.append)

    assert modal_app.request("GET", "/test") == {"ok": True}
    assert sleeps == [2, 4]


def test_request_gives_up_after_repeated_server_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRESHBOOKS_ACCESS_TOKEN", "access")
    calls: list[int] = []

    def fake_request(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(1)
        return _api_response(503)

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr(modal_app.time, "sleep", lambda seconds: None)

    with pytest.raises(httpx.HTTPStatusError):
        modal_app.request("GET", "/test")
    assert len(calls) == modal_app.API_ATTEMPTS


def test_request_does_not_retry_client_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FRESHBOOKS_ACCESS_TOKEN", "access")
    calls: list[int] = []

    def fake_request(*args: Any, **kwargs: Any) -> httpx.Response:
        calls.append(1)
        return _api_response(404)

    monkeypatch.setattr(httpx, "request", fake_request)

    with pytest.raises(httpx.HTTPStatusError):
        modal_app.request("GET", "/test")
    assert len(calls) == 1


def test_scheduled_run_reports_start_and_success(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALTHCHECKS_PING_URL", "https://hc-ping.com/check-id")
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(modal_app, "run_once", lambda: {"applied": 1})
    monkeypatch.setattr(
        modal_app,
        "ping_healthcheck",
        lambda url, signal="": events.append((url, signal)),
    )

    assert modal_app.scheduled_run.local() == {"applied": 1}
    assert events == [
        ("https://hc-ping.com/check-id", "start"),
        ("https://hc-ping.com/check-id", ""),
    ]


def test_scheduled_run_reports_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HEALTHCHECKS_PING_URL", "https://hc-ping.com/check-id")
    events: list[tuple[str, str]] = []

    def fail() -> dict[str, int]:
        raise RuntimeError("routing failed")

    monkeypatch.setattr(modal_app, "run_once", fail)
    monkeypatch.setattr(
        modal_app,
        "ping_healthcheck",
        lambda url, signal="": events.append((url, signal)),
    )

    with pytest.raises(RuntimeError, match="routing failed"):
        modal_app.scheduled_run.local()

    assert events == [
        ("https://hc-ping.com/check-id", "start"),
        ("https://hc-ping.com/check-id", "fail"),
    ]


def test_healthcheck_outage_does_not_fail_the_job(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def fail(*args: Any, **kwargs: Any) -> None:
        nonlocal attempts
        attempts += 1
        raise OSError

    monkeypatch.setattr(httpx, "get", fail)

    modal_app.ping_healthcheck("https://hc-ping.com/check-id")

    assert attempts == modal_app.HEALTHCHECK_ATTEMPTS


def install_typesafe_transport(monkeypatch: pytest.MonkeyPatch, respond: Any) -> None:
    import httpx2
    from pydantic_ai import Agent
    from pydantic_ai.models.typesafe import TypeSafeModel
    from pydantic_ai.providers.typesafe import TypeSafeProvider

    client = httpx2.AsyncClient(transport=httpx2.MockTransport(respond))
    provider = TypeSafeProvider(api_key="test-only", http_client=client)
    monkeypatch.setattr(
        modal_app,
        "Agent",
        lambda model, **kwargs: Agent(
            TypeSafeModel("jev-1.13.0", provider=provider),
            model_settings={"max_retries": 0},
            **kwargs,
        ),
    )


@pytest.mark.parametrize("confidence, abstain", [(0.96, False), (0.5, False), (0.99, True)])
def test_run_once_uses_native_jev_with_known_vendors_and_abstention(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    confidence: float,
    abstain: bool,
) -> None:
    import httpx2

    monkeypatch.setattr(modal_app, "list_expenses", lambda: [history(1, 101), imported(4)])
    monkeypatch.setattr(modal_app, "business_uuid", lambda: "business")
    monkeypatch.setattr(modal_app, "petty_cash_expense_ids", lambda *_: {4})
    monkeypatch.setattr(modal_app, "list_categories", lambda: {10: "Software"})
    updates = []
    monkeypatch.setattr(
        modal_app,
        "update_expense",
        lambda expense_id, fields: updates.append((expense_id, fields)) or fields,
    )

    def respond(request: httpx2.Request) -> httpx2.Response:
        assert str(request.url) == "https://api.typesafe.ai/v1/systemone"
        payload = json.loads(request.content)
        assert payload["model"] == "jev-1.13.0"
        assert len(payload["questions"]) == 1
        question_id, question = next(iter(payload["questions"].items()))
        options = question["criteria"]
        assert "Acme Co" in options, payload
        assert len(options) == 2, payload
        none = next(key for key in options if key != "Acme Co")
        chosen = none if abstain else "Acme Co"
        return httpx2.Response(
            200,
            json={
                "model": "jev-1.13.0",
                "answers": {
                    question_id: {
                        "type": "choice",
                        "choice": chosen,
                        "confidence": confidence,
                        "probabilities": {key: 0.98 if key == chosen else 0.02 for key in options},
                    }
                },
                "usage": {"input_tokens": 100, "output_tokens": 20},
            },
        )

    install_typesafe_transport(monkeypatch, respond)
    summary = modal_app.run_once()
    accepted = confidence >= 0.9 and not abstain
    outcome = "applied" if accepted else "abstained"
    assert summary[outcome] == 1
    assert updates == ([(4, {"vendor": "Acme Co", "categoryid": 10})] if accepted else [])
    logs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    routing = next(line for line in logs if line["event"] == f"routing_{outcome}")
    assert routing["model"] == "jev-1.13.0"
    assert routing["confidence"] == confidence
    assert routing["usage"] == {"input_tokens": 100, "output_tokens": 20}
    assert "SQ * ACME" not in json.dumps(logs)


@pytest.mark.parametrize("response_kind", ["outage", "malformed"])
def test_run_once_jev_failures_do_not_update_expenses(
    monkeypatch: pytest.MonkeyPatch,
    response_kind: str,
) -> None:
    import httpx2

    monkeypatch.setattr(modal_app, "list_expenses", lambda: [history(1, 101), imported(4)])
    monkeypatch.setattr(modal_app, "business_uuid", lambda: "business")
    monkeypatch.setattr(modal_app, "petty_cash_expense_ids", lambda *_: {4})
    monkeypatch.setattr(modal_app, "list_categories", lambda: {})

    def no_write(*args: Any) -> None:
        pytest.fail("A failed model call must not update an expense")

    monkeypatch.setattr(modal_app, "update_expense", no_write)
    install_typesafe_transport(
        monkeypatch, lambda _: httpx2.Response(503 if response_kind == "outage" else 200, json={})
    )
    with pytest.raises(RuntimeError, match="1 expense operations need retrying"):
        modal_app.run_once()

from __future__ import annotations

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

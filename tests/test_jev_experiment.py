from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

import experiment
from app import VendorDecision
from jev import JevSelector


def selector_response(**changes: Any) -> dict[str, Any]:
    return {
        "model": "jev-test",
        "answers": {
            "vendor": {
                "choice": "vendor_0",
                "confidence": 0.96,
                "probabilities": {"vendor_0": 0.98, "none": 0.02},
                **changes,
            }
        },
        "usage": {"input_tokens": 100, "output_tokens": 20},
    }


def test_jev_request_and_vendor_mapping() -> None:
    def respond(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["Authorization"] == "Bearer test-key"
        body = json.loads(request.content)
        assert body["state"] == {"descriptor": "SQ * ACME"}
        assert body["questions"]["vendor"]["criteria"] == {
            "vendor_0": "Acme",
            "none": "No clear match to a known vendor",
        }
        return httpx.Response(200, json=selector_response())

    with httpx.Client(transport=httpx.MockTransport(respond)) as client:
        decision = JevSelector(client, api_key="test-key")("SQ * ACME", ["Acme"])
    assert decision.vendor == "Acme"
    assert decision.confidence == 0.96
    assert decision.usage == {"input_tokens": 100, "output_tokens": 20}


@pytest.mark.parametrize(
    "changes",
    [
        {"confidence": 0.2},
        {"choice": "none", "probabilities": {"vendor_0": 0.02, "none": 0.98}},
    ],
)
def test_jev_abstains(changes: dict[str, Any]) -> None:
    with httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json=selector_response(**changes))
        )
    ) as client:
        assert JevSelector(client, api_key="test")("unknown", ["Acme"]).vendor is None


@pytest.mark.parametrize(
    "changes",
    [
        {"choice": "invented"},
        {"confidence": float("nan")},
        {"confidence": 1.1},
        {"probabilities": {"vendor_0": 0.2, "none": 0.1}},
        {"probabilities": {"vendor_0": 0.2, "none": 0.8}},
        {"probabilities": {"vendor_0": 0.98, "unexpected": 0.02}},
    ],
)
def test_invalid_answers_fail_closed(changes: dict[str, Any]) -> None:
    # Build bytes to allow a deliberately nonstandard NaN value in the test response.
    payload = json.dumps(selector_response(**changes)).encode()
    with (
        httpx.Client(
            transport=httpx.MockTransport(lambda _: httpx.Response(200, content=payload))
        ) as client,
        pytest.raises(ValueError),
    ):
        JevSelector(client, api_key="test")("descriptor", ["Acme"])


def test_vendor_limit_and_http_errors_are_not_silently_accepted() -> None:
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429))) as client:
        select = JevSelector(client, api_key="test")
        with pytest.raises(ValueError):
            select("descriptor", [str(i) for i in range(255)])
        with pytest.raises(httpx.HTTPStatusError):
            select("descriptor", ["Acme"])


def row(expense_id: int, when: str, vendor: str = "Acme", notes: str = "ACME") -> dict:
    return {
        "expenseid": expense_id,
        "date": when,
        "vendor": vendor,
        "notes": notes,
        "transactionid": expense_id,
        "bank_name": "Checking",
        "categoryid": 10,
    }


def test_holdout_excludes_future_vendors_unmatched_rows_and_label_only_descriptors() -> None:
    expenses = [
        row(1, "2026-09-01"),
        row(2, "2026-10-01", "New Vendor", "NEW VENDOR"),
        row(3, "2026-10-02", notes=""),
        row(4, "2026-10-03", "Unreconciled"),
        row(5, "2026-10-04"),
        row(6, "missing"),
    ]
    vendors, rows, familiar = experiment.holdout(expenses, {1, 2, 3, 5, 6}, "2026-10-01", 50)
    assert vendors == ["Acme"]
    assert [r["expenseid"] for r in rows] == [2, 5]
    assert familiar == {"acme"}
    assert experiment.holdout(expenses, {1, 2, 5}, "2026-10-01", 1)[1] == [expenses[1]]


def test_comparison_reports_wrong_assignments_errors_and_segments_without_raw_data(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = [
        row(1, "2026-10-01", notes="private familiar descriptor"),
        row(2, "2026-10-02", "Other Vendor", "private unseen descriptor"),
        row(3, "2026-10-03", notes="private third descriptor"),
    ]
    answers = iter(
        [VendorDecision(vendor="Acme"), VendorDecision(vendor="Acme"), VendorDecision(vendor=None)]
    )

    def fail(*_: Any) -> VendorDecision:
        raise RuntimeError("private exception contents")

    summary = experiment.compare(
        rows,
        ["Acme"],
        {"private familiar descriptor"},
        {"test": lambda *_: next(answers), "broken": fail},
    )
    assert summary["test"]["correct"] == 1
    assert summary["test"]["wrong"] == 1
    assert summary["test"]["abstained"] == 1
    assert summary["test"]["assignment_accuracy"] == 0.5
    assert summary["test"]["coverage"] == 2 / 3
    assert summary["test"]["familiar_correct"] == 1
    assert summary["test"]["unseen_wrong"] == 1
    assert summary["broken"]["error"] == 3
    assert summary["broken"]["assignment_accuracy"] is None
    output = capsys.readouterr().out
    assert "private" not in output
    assert "Acme" not in output
    assert "Other Vendor" not in output


def test_remote_experiment_does_not_write_expenses(monkeypatch: pytest.MonkeyPatch) -> None:
    expenses = [row(1, "2026-09-01"), row(2, "2026-10-01"), row(3, "2026-10-02")]
    for expense in expenses:
        expense["amount"] = {"code": "USD"}
    monkeypatch.setattr(experiment.custodian, "list_expenses", lambda: expenses)
    monkeypatch.setattr(experiment.custodian, "business_uuid", lambda: "business")
    monkeypatch.setattr(experiment.custodian, "petty_cash_expense_ids", lambda *_: {3})

    def no_write(*_: Any, **__: Any) -> None:
        pytest.fail("Experiment attempted to write an expense")

    monkeypatch.setattr(experiment.custodian, "update_expense", no_write)
    monkeypatch.setattr(experiment.custodian, "process_expenses", no_write)
    monkeypatch.setattr(experiment, "Agent", lambda *a, **kw: object())
    monkeypatch.setattr(experiment, "JevSelector", lambda *a, **kw: object())

    def check_comparison(rows: list[dict], vendors: list[str], *args: Any) -> dict:
        assert [r["expenseid"] for r in rows] == [2]
        assert vendors == ["Acme"]
        return {"checked": True}

    monkeypatch.setattr(experiment, "compare", check_comparison)
    assert experiment.evaluate.local("2026-10-01")["results"] == {"checked": True}


def test_cost_estimates_use_recorded_usage_and_cached_input_rates() -> None:
    assert experiment.estimate_cost(
        "jev-1.13.0", {"input_tokens": 15442, "output_tokens": 6065}
    ) == pytest.approx(0.000648564)
    assert experiment.estimate_cost(
        "gpt-5-mini-2025-08-07",
        {"input_tokens": 3951, "output_tokens": 5916, "cache_read_tokens": 0},
    ) == pytest.approx(0.01281975)
    assert experiment.estimate_cost(
        "gpt-5-mini-2025-08-07",
        {"input_tokens": 1000000, "output_tokens": 1000000, "cache_read_tokens": 500000},
    ) == pytest.approx(2.1375)
    assert experiment.estimate_cost("unpriced", {"input_tokens": 10}) is None
    assert experiment.estimate_cost("jev-1.13.0", {}) is None
    with pytest.raises(ValueError):
        experiment.estimate_cost(
            "jev-1.13.0", {"input_tokens": 10, "output_tokens": 1, "cache_read_tokens": 11}
        )


def logged(expense_id: int, descriptor: str, vendor: str | None = "Acme") -> dict:
    import hashlib

    from app import normalize

    return {
        "expense_id": expense_id,
        "timestamp": "2026-10-20T12:00:00Z",
        "descriptor_hash": hashlib.sha256(normalize(descriptor).encode()).hexdigest()[:16],
        "vendor": vendor,
        "categoryid": 10,
        "before": {"vendor": descriptor},
    }


def test_replay_uses_seven_day_boundary_excludes_self_and_recovers_original_input() -> None:
    expenses = [
        row(1, "2026-10-01", "Old", "OLD"),
        row(2, "2026-10-03", "Boundary", "BOUNDARY"),
        row(3, "2026-10-04", "Too Recent", "RECENT"),
        row(4, "2026-10-10", "New Vendor", "edited notes"),
        row(5, "2026-09-01", "Unmatched", "UNMATCHED"),
    ]
    decision = logged(4, "original descriptor", "Old")
    rows, summary = experiment.replay_rows(
        expenses, {1, 2, 3, 4}, [decision, decision], "2026-01-01", 100
    )
    assert len(rows) == 1
    assert rows[0]["_vendors"] == ["Boundary", "Old"]
    assert rows[0]["_cutoff"] == "2026-10-03"
    assert rows[0]["vendor"] == "New Vendor"
    assert rows[0]["notes"] == "original descriptor"
    assert summary["logged_expenses"] == 1
    assert summary["distinct_descriptors"] == 1


def test_replay_reports_unrecoverable_descriptors_and_unreconciled_cases() -> None:
    expenses = [row(1, "2026-09-01"), row(2, "2026-10-01"), row(3, "2026-10-02")]
    bad = logged(2, "different descriptor")
    bad["before"] = {"vendor": "changed"}
    rows, summary = experiment.replay_rows(
        expenses, {1, 2}, [bad, logged(3, "ACME")], "2026-01-01", 100
    )
    assert rows == []
    assert summary["skipped"] == {
        "descriptor_hash_mismatch_or_missing": 1,
        "not_reconciled_or_ineligible": 1,
    }


def test_replay_comparison_uses_per_expense_candidates_and_scores_category() -> None:
    first = {
        **row(1, "2026-10-01"),
        "_vendors": ["Acme"],
        "_familiar": set(),
        "_history": {"Acme": 99},
    }
    second = {
        **row(2, "2026-10-02", "New Vendor"),
        "_vendors": ["Other"],
        "_familiar": {"acme"},
        "_history": {"Other": 10},
    }
    observed = []

    def select(descriptor: str, vendors: list[str]) -> VendorDecision:
        observed.append(vendors)
        return VendorDecision(vendor="Acme" if vendors == ["Acme"] else None)

    result = experiment.compare([first, second], [], set(), {"test": select})["test"]
    assert observed == [["Acme"], ["Other"]]
    assert result["correct"] == 1
    assert result["appropriate_abstention"] == 1
    assert result["vendor_or_category_wrong"] == 1
    assert result["in_list"] == 1
    assert result["out_of_list"] == 1


def test_prepare_only_returns_before_constructing_models(monkeypatch: pytest.MonkeyPatch) -> None:
    expenses = [row(1, "2026-09-01"), row(2, "2026-10-01")]
    for expense in expenses:
        expense["amount"] = {"code": "USD"}
    monkeypatch.setattr(experiment.custodian, "list_expenses", lambda: expenses)
    monkeypatch.setattr(experiment.custodian, "business_uuid", lambda: "business")
    monkeypatch.setattr(experiment.custodian, "petty_cash_expense_ids", lambda *_: set())

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Preparation must not construct models or write expenses")

    monkeypatch.setattr(experiment, "Agent", forbidden)
    monkeypatch.setattr(experiment, "JevSelector", forbidden)
    monkeypatch.setattr(experiment.custodian, "update_expense", forbidden)
    result = experiment.evaluate.local(
        "2026-10-01", decisions=[logged(2, "ACME")], prepare_only=True
    )
    assert result["rows"] == 1
    assert result["results"] == {}
    assert result["replay"]["logged_decisions_vs_final_labels"]["correct"] == 1

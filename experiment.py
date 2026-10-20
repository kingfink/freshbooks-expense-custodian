"""Unscheduled, read-only comparison on reconciled FreshBooks expenses."""

from __future__ import annotations

import hashlib
import json
import os
from collections import Counter
from collections.abc import Callable
from datetime import date, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

import httpx
import modal
from pydantic_ai import Agent

import app as custodian
from app import VendorDecision, normalize
from jev import JevSelector

experiment = modal.App("freshbooks-jev-experiment")

# USD per million tokens; standard direct API rates checked 2026-10-20.
# https://docs.typesafe.ai/models
# https://developers.openai.com/api/docs/models/gpt-5-mini
PRICING_CHECKED = "2026-10-20"
TOKEN_RATES = {
    "jev-1.13.0": (0.042, 0.042, 0.0),
    "gpt-5-mini-2025-08-07": (0.25, 0.025, 2.0),
}


def estimate_cost(model: str | None, usage: dict[str, int]) -> float | None:
    rates = TOKEN_RATES.get(model or "")
    if rates is None or not {"input_tokens", "output_tokens"} <= usage.keys():
        return None
    inputs, outputs = usage["input_tokens"], usage["output_tokens"]
    cached = usage.get("cache_read_tokens", 0)
    if min(inputs, outputs, cached) < 0 or cached > inputs:
        raise ValueError("Invalid token usage")
    input_rate, cache_rate, output_rate = rates
    return ((inputs - cached) * input_rate + cached * cache_rate + outputs * output_rate) / 1e6


class BaselineDecision(VendorDecision):
    model: str | None
    usage: dict[str, int]


def holdout(
    expenses: list[dict[str, Any]], trusted_ids: set[int], since: str, limit: int
) -> tuple[list[str], list[dict[str, Any]], set[str]]:
    date.fromisoformat(since)
    if limit < 1:
        raise ValueError("limit must be positive")
    training, testing = [], []
    for expense in expenses:
        if expense["expenseid"] not in trusted_ids or not custodian._is_bank_import(expense):
            continue
        if not expense.get("vendor") or expense.get("categoryid") is None:
            continue
        expense_date = str(expense.get("date") or "")
        try:
            date.fromisoformat(expense_date)
        except ValueError:
            continue
        (training if expense_date < since else testing).append(expense)
    vendors = sorted(custodian.vendor_history(training, {e["expenseid"] for e in training}))
    # Do not fall back to vendor: reconciliation has already replaced it with the label.
    testing = [e for e in testing if str(e.get("notes") or "").strip()]
    testing.sort(key=lambda e: (e["date"], e["expenseid"]))
    familiar = {normalize(str(e["notes"])) for e in training if e.get("notes")}
    if not vendors or not testing:
        raise ValueError("Need pre-cutoff vendor history and post-cutoff labeled notes")
    return vendors, testing[:limit], familiar


def replay_rows(
    expenses: list[dict[str, Any]],
    trusted_ids: set[int],
    decisions: list[dict[str, Any]],
    since: str,
    limit: int,
    lag_days: int = 7,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Reconstruct inputs from logs and dated, currently reconciled bank history."""
    if lag_days < 1:
        raise ValueError("lag_days must be positive")
    eligible = {}
    for expense in expenses:
        try:
            date.fromisoformat(str(expense.get("date", "")))
        except ValueError:
            continue
        if (
            expense["expenseid"] in trusted_ids
            and custodian._is_bank_import(expense)
            and expense.get("vendor")
            and expense.get("categoryid") is not None
        ):
            eligible[expense["expenseid"]] = expense
    first = {}
    for decision in sorted(decisions, key=lambda d: d["timestamp"]):
        first.setdefault(decision["expense_id"], decision)
    skipped = Counter()
    rows = []
    for expense_id, decision in first.items():
        expense = eligible.get(expense_id)
        if expense is None:
            skipped["not_reconciled_or_ineligible"] += 1
            continue
        if expense["date"] < since:
            skipped["before_since"] += 1
            continue
        descriptor = None
        # Notes survive most updates; a pre-update vendor can recover the fallback input.
        for value in (expense.get("notes"), decision.get("before", {}).get("vendor")):
            if value and hashlib.sha256(normalize(str(value)).encode()).hexdigest()[
                :16
            ] == decision.get("descriptor_hash"):
                descriptor = str(value).strip()
                break
        if not descriptor:
            skipped["descriptor_hash_mismatch_or_missing"] += 1
            continue
        cutoff = date.fromisoformat(expense["date"]) - timedelta(days=lag_days)
        history = [
            e
            for e in eligible.values()
            if e["expenseid"] != expense_id and date.fromisoformat(e["date"]) <= cutoff
        ]
        mapping = custodian.vendor_history(history, {e["expenseid"] for e in history})
        if not mapping:
            skipped["no_prior_vendors"] += 1
            continue
        if len(mapping) > 254:
            skipped["too_many_prior_vendors"] += 1
            continue
        familiar = {normalize(str(e["notes"])) for e in history if e.get("notes")}
        rows.append(
            {
                **expense,
                "notes": descriptor,
                "_vendors": sorted(mapping),
                "_history": mapping,
                "_familiar": familiar,
                "_logged_vendor": decision.get("vendor"),
                "_logged_category": decision.get("categoryid"),
                "_descriptor_hash": decision["descriptor_hash"],
                "_cutoff": cutoff.isoformat(),
            }
        )
    rows.sort(key=lambda e: (e["date"], e["expenseid"]))
    selected = rows[:limit]
    return selected, {
        "log_records": len(decisions),
        "logged_expenses": len(first),
        "eligible_before_limit": len(rows),
        "skipped": dict(skipped),
        "distinct_descriptors": len({e["_descriptor_hash"] for e in selected}),
        "lag_days": lag_days,
    }


def compare(
    rows: list[dict[str, Any]],
    vendors: list[str],
    familiar: set[str],
    selectors: dict[str, Callable[[str, list[str]], VendorDecision]],
) -> dict[str, Any]:
    if not rows:
        raise ValueError("Need at least one evaluation row")
    totals: dict[str, Counter] = {name: Counter() for name in selectors}
    for row in rows:
        descriptor = str(row["notes"]).strip()
        expected = normalize(str(row["vendor"]))
        candidates = row.get("_vendors", vendors)
        known_descriptors = row.get("_familiar", familiar)
        segment = "familiar" if normalize(descriptor) in known_descriptors else "unseen"
        in_list = expected in {normalize(v) for v in candidates}
        for name, select in selectors.items():
            started = perf_counter()
            result: dict[str, Any] = {
                "selector": name,
                "expense_id": row["expenseid"],
                "segment": segment,
                "vendor_count": len(candidates),
                "label_in_candidates": in_list,
                "history_cutoff": row.get("_cutoff"),
            }
            try:
                decision = select(descriptor, candidates)
                if decision.vendor is not None and decision.vendor not in candidates:
                    raise ValueError("Selector returned an unknown vendor")
                outcome = (
                    "abstained"
                    if decision.vendor is None
                    else "correct"
                    if normalize(decision.vendor) == expected
                    else "wrong"
                )
                totals[name]["in_list" if in_list else "out_of_list"] += 1
                if decision.vendor is None and not in_list:
                    totals[name]["appropriate_abstention"] += 1
                if "_history" in row and decision.vendor is not None:
                    category_correct = row["_history"][decision.vendor] == row["categoryid"]
                    result["category_correct"] = category_correct
                    totals[name][
                        "vendor_and_category_correct"
                        if outcome == "correct" and category_correct
                        else "vendor_or_category_wrong"
                    ] += 1
                # No descriptors or vendor names in evaluation output.
                for field in ("confidence", "probability", "model", "usage"):
                    if hasattr(decision, field):
                        result[field] = getattr(decision, field)
                cost = estimate_cost(result.get("model"), result.get("usage", {}))
                result["estimated_cost_usd"] = cost
                if cost is None:
                    totals[name]["unpriced_rows"] += 1
                else:
                    totals[name]["estimated_cost_usd"] += cost
                for token, count in result.get("usage", {}).items():
                    totals[name][token] += count
            except Exception as error:
                outcome = "error"
                result["error"] = type(error).__name__
            elapsed = perf_counter() - started
            totals[name][outcome] += 1
            totals[name][f"{segment}_{outcome}"] += 1
            totals[name]["seconds"] += elapsed
            custodian.log("evaluation_row", **result, outcome=outcome, seconds=elapsed)
    summaries = {}
    for name, counts in totals.items():
        assigned = counts["correct"] + counts["wrong"]
        summaries[name] = {
            **counts,
            "rows": len(rows),
            "coverage": assigned / len(rows),
            "assignment_accuracy": counts["correct"] / assigned if assigned else None,
            "mean_seconds": counts["seconds"] / len(rows),
            "pricing_checked": PRICING_CHECKED,
            "estimated_cost_usd": (
                None if counts["error"] or counts["unpriced_rows"] else counts["estimated_cost_usd"]
            ),
        }
    return summaries


@experiment.function(
    image=custodian.image.pip_install("pydantic-ai-slim[openai]>=1,<2").add_local_python_source(
        "app"
    ),
    secrets=[
        custodian.freshbooks_secret,
        modal.Secret.from_name("openai-secret"),
        modal.Secret.from_name("typesafe-secret", required_keys=["TYPESAFE_API_KEY"]),
    ],
    timeout=3600,
)
def evaluate(
    since: str,
    limit: int = 50,
    min_confidence: float = 0.9,
    decisions: list[dict[str, Any]] | None = None,
    lag_days: int = 7,
    prepare_only: bool = False,
) -> dict[str, Any]:
    date.fromisoformat(since)
    if limit < 1 or not 0 <= min_confidence <= 1:
        raise ValueError("Require a positive limit and confidence between 0 and 1")
    # No scheduled_run, process_expenses, or update_expense calls in this experiment.
    expenses = custodian.list_expenses()
    currencies = {e["amount"]["code"] for e in expenses if e.get("amount", {}).get("code")}
    if len(currencies) != 1:
        raise ValueError("Evaluation requires exactly one expense currency")
    unmatched = custodian.petty_cash_expense_ids(custodian.business_uuid(), currencies.pop())
    trusted = {e["expenseid"] for e in expenses} - unmatched
    replay = None
    if decisions is None:
        vendors, rows, familiar = holdout(expenses, trusted, since, limit)
    else:
        rows, replay = replay_rows(expenses, trusted, decisions, since, limit, lag_days)
        if not rows:
            return {"replay": replay, "results": {}, "rows": 0}
        logged_counts = Counter()
        for row in rows:
            original = row["_logged_vendor"]
            outcome = (
                "abstained"
                if original is None
                else "correct"
                if normalize(original) == normalize(row["vendor"])
                else "wrong"
            )
            logged_counts[outcome] += 1
            if original is not None:
                pair_correct = outcome == "correct" and row["_logged_category"] == row["categoryid"]
                logged_counts[
                    "vendor_and_category_correct" if pair_correct else "vendor_or_category_wrong"
                ] += 1
        replay["logged_decisions_vs_final_labels"] = dict(logged_counts)
        vendors, familiar = [], set()
    if len(vendors) > 254:
        raise ValueError("Too many historical vendors for one Jev Choice")
    if prepare_only:
        return {
            "replay": replay,
            "rows": len(rows),
            "results": {},
            "min_vendor_count": min(len(r.get("_vendors", vendors)) for r in rows),
            "max_vendor_count": max(len(r.get("_vendors", vendors)) for r in rows),
        }
    agent = Agent(
        os.getenv("OPENAI_MODEL", "openai-responses:gpt-5-mini"),
        output_type=VendorDecision,
        instructions="Choose an exact supplied vendor or null. Never invent a vendor or category.",
    )

    def baseline(descriptor: str, vendors: list[str]) -> VendorDecision:
        result = agent.run_sync(json.dumps({"descriptor": descriptor, "vendors": vendors}))
        usage = result.usage
        if callable(usage):
            usage = usage()
        return BaselineDecision(
            vendor=result.output.vendor,
            model=result.response.model_name,
            usage={
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
                "cache_read_tokens": usage.cache_read_tokens,
            },
        )

    with httpx.Client() as client:
        summaries = compare(
            rows,
            vendors,
            familiar,
            {
                "jev": JevSelector(client, min_confidence=min_confidence),
                "openai": baseline,
            },
        )

    return {
        "replay": replay,
        "since": since,
        "limit": limit,
        "min_confidence": min_confidence,
        "vendor_count": len(vendors) if replay is None else None,
        "min_vendor_count": min(len(r.get("_vendors", vendors)) for r in rows),
        "max_vendor_count": max(len(r.get("_vendors", vendors)) for r in rows),
        "results": summaries,
    }


@experiment.local_entrypoint()
def main(
    since: str,
    limit: int = 50,
    min_confidence: float = 0.9,
    decisions: str = "",
    lag_days: int = 7,
    prepare_only: bool = False,
) -> None:
    date.fromisoformat(since)
    if limit < 1 or not 0 <= min_confidence <= 1:
        raise ValueError("Require a positive limit and confidence between 0 and 1")
    manifest = json.loads(Path(decisions).read_text()) if decisions else None
    print(
        json.dumps(
            evaluate.remote(since, limit, min_confidence, manifest, lag_days, prepare_only),
            indent=2,
        )
    )

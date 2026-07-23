from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections import Counter
from collections.abc import Callable
from datetime import date
from typing import Any
from urllib.parse import urlencode

import httpx
import modal
from pydantic import BaseModel
from pydantic_ai import Agent

APP_NAME = "freshbooks-expense-custodian"
API_ROOT = "https://api.freshbooks.com"
HEALTHCHECK_ATTEMPTS = 3
DRY_RUN = False

app = modal.App(APP_NAME)
freshbooks_secret = modal.Secret.from_name("freshbooks-secret")
openai_secret = modal.Secret.from_name("openai-secret")
healthchecks_secret = modal.Secret.from_name(
    "freshbooks-expense-custodian-healthchecks",
    required_keys=["HEALTHCHECKS_PING_URL"],
)
image = modal.Image.debian_slim(python_version="3.12").pip_install("pydantic-ai-slim[openai]>=1,<2")


class VendorDecision(BaseModel):
    vendor: str | None


def log(event: str, **values: Any) -> None:
    print(json.dumps({"event": event, **values}, default=str, separators=(",", ":")))


def ping_healthcheck(ping_url: str, signal: str = "") -> None:
    url = ping_url.rstrip("/")
    if signal:
        url = f"{url}/{signal}"
    error: Exception | None = None
    for _ in range(HEALTHCHECK_ATTEMPTS):
        try:
            response = httpx.get(url, timeout=5)
            response.raise_for_status()
            return
        except Exception as caught:
            error = caught
    log(
        "healthcheck_ping_failed",
        signal=signal or "success",
        error=type(error).__name__,
    )


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def vendor_history(expenses: list[dict[str, Any]], trusted_ids: set[int]) -> dict[str, int]:
    latest: dict[str, tuple[tuple[str, str, int], str, int]] = {}
    for expense in expenses:
        vendor, category = expense.get("vendor"), expense.get("categoryid")
        if _expense_id(expense) not in trusted_ids or not vendor or category is None:
            continue
        key = normalize(vendor)
        rank = (
            str(expense.get("updated") or ""),
            str(expense.get("date") or ""),
            _expense_id(expense),
        )
        if key not in latest or rank > latest[key][0]:
            latest[key] = (rank, str(vendor), int(category))

    return {entry[1]: entry[2] for entry in latest.values()}


def process_expenses(
    expenses: list[dict[str, Any]],
    candidate_ids: set[int],
    trusted_ids: set[int],
    decide: Callable[[str, list[str]], VendorDecision],
    update: Callable[[int, dict[str, Any]], dict[str, Any]],
    *,
    categories: dict[int, str] | None = None,
    dry_run: bool = True,
) -> dict[str, int]:
    categories = categories or {}
    summary = Counter(pulled=len(expenses), candidates=len(candidate_ids))
    history = vendor_history(expenses, trusted_ids)
    vendors = sorted(history)

    for expense in expenses:
        expense_id = _expense_id(expense)
        if expense_id not in candidate_ids:
            continue
        if not _is_bank_import(expense):
            summary["ignored"] += 1
            continue

        descriptor = next(
            (
                str(expense[field]).strip()
                for field in ("notes", "vendor")
                if _present(expense.get(field))
            ),
            None,
        )
        context = {
            "expense_id": expense_id,
            "before": _label_category(
                {"vendor": expense.get("vendor"), "categoryid": expense.get("categoryid")},
                categories,
            ),
        }
        if not descriptor or not vendors:
            outcome = _abstain("no_descriptor_or_history", context, dry_run)
        else:
            context["descriptor_hash"] = hashlib.sha256(normalize(descriptor).encode()).hexdigest()[
                :16
            ]
            try:
                decision = decide(descriptor, vendors)
            except Exception as error:
                log("routing_retry", **context, error=type(error).__name__)
                outcome = "retry"
            else:
                outcome = _apply_decision(
                    expense, decision, vendors, history, update, context, categories, dry_run
                )

        summary[outcome] += 1

    result = dict(summary)
    if result.get("retry"):
        raise RuntimeError(f"{result['retry']} expense operations need retrying")
    return result


def _apply_decision(
    expense: dict[str, Any],
    decision: VendorDecision,
    vendors: list[str],
    history: dict[str, int],
    update: Callable[[int, dict[str, Any]], dict[str, Any]],
    context: dict[str, Any],
    categories: dict[int, str],
    dry_run: bool,
) -> str:
    context["vendor"] = decision.vendor
    if decision.vendor not in vendors:
        return _abstain("vendor_not_selected", context, dry_run)

    category_id = history[decision.vendor]
    fields: dict[str, Any] = {}
    if expense.get("vendor") != decision.vendor:
        fields["vendor"] = decision.vendor
    if category_id != expense.get("categoryid"):
        fields["categoryid"] = category_id
    context.update(
        categoryid=category_id,
        category=_category_name(categories, category_id),
        after=_label_category(
            {
                "vendor": fields.get("vendor", expense.get("vendor")),
                "categoryid": fields.get("categoryid", expense.get("categoryid")),
            },
            categories,
        ),
    )

    if dry_run:
        log("routing_dry_run", **context)
        return "dry_run"
    if not fields:
        log("routing_noop", **context)
        return "applied"
    try:
        updated = update(_expense_id(expense), fields)
    except Exception as error:
        log("routing_retry", **context, error=type(error).__name__)
        return "retry"
    context["after"] = _label_category(
        {"vendor": updated.get("vendor"), "categoryid": updated.get("categoryid")},
        categories,
    )
    log("routing_applied", **context)
    return "applied"


def _abstain(reason: str, context: dict[str, Any], dry_run: bool) -> str:
    log("routing_abstained", **context, reason=reason)
    return "dry_run" if dry_run else "abstained"


def _category_name(categories: dict[int, str], category_id: Any) -> str | None:
    if category_id is None:
        return None
    return categories.get(int(category_id))


def _label_category(fields: dict[str, Any], categories: dict[int, str]) -> dict[str, Any]:
    labeled: dict[str, Any] = {}
    for key, value in fields.items():
        labeled[key] = value
        if key == "categoryid":
            labeled["category"] = _category_name(categories, value)
    return labeled


def _is_bank_import(expense: dict[str, Any]) -> bool:
    return bool(
        _present(expense.get("bank_name"))
        and _present(expense.get("transactionid"))
        and not expense.get("from_bulk_import")
        and expense.get("vis_state", 0) == 0
    )


def _expense_id(expense: dict[str, Any]) -> int:
    return int(expense.get("expenseid") or expense["id"])


def _present(value: Any) -> bool:
    return value is not None and value != "" and value is not False


def _refresh_tokens() -> None:
    response = httpx.post(
        f"{API_ROOT}/auth/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": os.environ["FRESHBOOKS_CLIENT_ID"],
            "client_secret": os.environ["FRESHBOOKS_CLIENT_SECRET"],
            "refresh_token": os.environ["FRESHBOOKS_REFRESH_TOKEN"],
            "redirect_uri": os.environ["FRESHBOOKS_REDIRECT_URI"],
        },
        timeout=30,
    )
    response.raise_for_status()
    tokens = {
        "FRESHBOOKS_ACCESS_TOKEN": response.json()["access_token"],
        "FRESHBOOKS_REFRESH_TOKEN": response.json()["refresh_token"],
    }
    freshbooks_secret.update(tokens)
    os.environ.update(tokens)


def request(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    def send() -> httpx.Response:
        return httpx.request(
            method,
            f"{API_ROOT}{path}",
            headers={"Authorization": f"Bearer {os.environ['FRESHBOOKS_ACCESS_TOKEN']}"},
            json=body,
            timeout=30,
        )

    response = send()
    if response.status_code == 401:
        _refresh_tokens()
        response = send()
    response.raise_for_status()
    return response.json()


def list_expenses() -> list[dict[str, Any]]:
    account = os.environ["FRESHBOOKS_ACCOUNT_ID"]
    expenses: list[dict[str, Any]] = []
    page = 1
    while True:
        result = request(
            "GET",
            f"/accounting/account/{account}/expenses/expenses?page={page}&per_page=100",
        )["response"]["result"]
        expenses.extend(result.get("expenses", []))
        if page >= int(result.get("pages", 1)):
            return expenses
        page += 1


def list_categories() -> dict[int, str]:
    account = os.environ["FRESHBOOKS_ACCOUNT_ID"]
    categories: dict[int, str] = {}
    page = 1
    while True:
        result = request(
            "GET",
            f"/accounting/account/{account}/expenses/categories?page={page}&per_page=100",
        )["response"]["result"]
        for category in result.get("categories", []):
            if category.get("categoryid") is not None:
                categories[int(category["categoryid"])] = str(category.get("category") or "")
        if page >= int(result.get("pages", 1)):
            return categories
        page += 1


def business_uuid() -> str:
    account_id = os.environ["FRESHBOOKS_ACCOUNT_ID"]
    memberships = request("GET", "/auth/api/v1/users/me")["response"]["business_memberships"]
    for membership in memberships:
        business = membership["business"]
        if business["account_id"] == account_id:
            return str(business["business_uuid"])
    raise RuntimeError(f"No FreshBooks business found for account {account_id}")


def petty_cash_expense_ids(business_id: str, currency_code: str) -> set[int]:
    ledger_query = urlencode(
        {
            "accountid": os.environ["FRESHBOOKS_PETTY_CASH_ACCOUNT_UUID"],
            "currency_code": currency_code,
            "start_date": "2018-01-01",
            "end_date": date.today().isoformat(),
            "locale": "en",
        }
    )
    report = request(
        "GET",
        f"/accounting/businesses/{business_id}/reports/general_ledger?{ledger_query}",
    )["response"]["result"]["general_ledger"]
    transactions = (
        transaction
        for account in report.get("data", [])
        for sub_account in (account, *account.get("sub_accounts", []))
        for transaction in sub_account.get("transactions", [])
    )
    return {
        int(transaction["expenseid"])
        for transaction in transactions
        if transaction.get("expenseid") is not None
    }


def update_expense(expense_id: int, fields: dict[str, Any]) -> dict[str, Any]:
    if set(fields) - {"vendor", "categoryid"}:
        raise ValueError("Unsupported expense update")
    account = os.environ["FRESHBOOKS_ACCOUNT_ID"]
    result = request(
        "PUT",
        f"/accounting/account/{account}/expenses/expenses/{expense_id}",
        {"expense": fields},
    )["response"]["result"]
    return result["expense"]


def run_once() -> dict[str, int]:
    expenses = list_expenses()
    bank_ids = {_expense_id(expense) for expense in expenses if _is_bank_import(expense)}
    currency_code = next(
        (
            str(expense["amount"]["code"])
            for expense in expenses
            if expense.get("amount", {}).get("code")
        ),
        "USD",
    )
    unmatched_ids = petty_cash_expense_ids(business_uuid(), currency_code)
    candidate_ids = bank_ids & unmatched_ids
    trusted_ids = bank_ids - unmatched_ids
    categories = list_categories()
    agent = Agent(
        os.getenv("OPENAI_MODEL", "openai-responses:gpt-5-mini"),
        output_type=VendorDecision,
        instructions="Choose an exact supplied vendor or null. Never invent a vendor or category.",
    )

    def decide(descriptor: str, vendors: list[str]) -> VendorDecision:
        prompt = json.dumps({"descriptor": descriptor, "vendors": vendors})
        return agent.run_sync(prompt).output

    summary = process_expenses(
        expenses,
        candidate_ids,
        trusted_ids,
        decide,
        update_expense,
        categories=categories,
        dry_run=DRY_RUN,
    )
    summary.update(bank_expenses=len(bank_ids), training_expenses=len(trusted_ids))
    log("run_completed", **summary)
    return summary


@app.function(
    image=image,
    secrets=[freshbooks_secret, openai_secret, healthchecks_secret],
    schedule=modal.Cron("0 3 * * *", timezone="America/New_York"),
    max_containers=1,
)
def scheduled_run() -> dict[str, int]:
    ping_url = os.environ["HEALTHCHECKS_PING_URL"]
    ping_healthcheck(ping_url, "start")
    try:
        summary = run_once()
    except Exception:
        ping_healthcheck(ping_url, "fail")
        raise
    ping_healthcheck(ping_url)
    return summary


@app.local_entrypoint()
def main() -> None:
    print(scheduled_run.remote())

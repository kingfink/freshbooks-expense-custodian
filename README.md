# FreshBooks Expense Custodian

A small, stateless Modal job that fixes unmatched bank-created expenses using reconciled FreshBooks history.

## Why this exists

FreshBooks' `Apply to future expenses` option appears to depend on automatic merchant matching, which often fails in this account. This job reuses human-reviewed vendors and categories from reconciled bank expenses to fill that gap.

Daily at 3 AM `America/New_York`, it:

1. Finds unmatched bank expenses in the Petty Cash ledger.
2. Builds vendor history and each vendor's latest category from reconciled bank expenses.
3. Uses Jev 1.13.0 through PydanticAI to select a known vendor or abstain.
4. Applies matches with confidence ≥0.9 and logs the result.

Ambiguous expenses stay untouched. API failures fail the run and trigger monitoring. Once reconciled, expenses leave Petty Cash and the candidate set; no database or watermark is needed.

Descriptors and known vendor names are sent to TypeSafe. JSON logs record before/after values, model confidence, and token usage, with a descriptor hash instead of raw notes. Modal forwards logs through its configured integration.

## Setup

In the deployment workspace, configure three Modal secrets:

- `typesafe-secret`, containing `TYPESAFE_API_KEY`.
- `freshbooks-secret`, containing:

```text
FRESHBOOKS_ACCOUNT_ID
FRESHBOOKS_ACCESS_TOKEN
FRESHBOOKS_REFRESH_TOKEN
FRESHBOOKS_CLIENT_ID
FRESHBOOKS_CLIENT_SECRET
FRESHBOOKS_REDIRECT_URI
FRESHBOOKS_PETTY_CASH_ACCOUNT_UUID
```

- `freshbooks-expense-custodian-healthchecks`, containing `HEALTHCHECKS_PING_URL`.

The FreshBooks OAuth app requires these scopes:

```text
user:profile:read
user:expenses:read
user:expenses:write
user:reports:read
user:account:read
```

Bank imports are identified by `transactionid` and `bank_name`; descriptors come from `notes`, falling back to `vendor`. Set `FRESHBOOKS_PETTY_CASH_ACCOUNT_UUID` to the account-specific Petty Cash system-account UUID.

Install, deploy, and run manually:

```bash
uv sync --frozen
uv run modal deploy app.py
uv run modal run app.py
```

Set `DRY_RUN = True` in `app.py` to log proposed changes without updating expenses. OAuth token refreshes are still persisted to `freshbooks-secret`.

## Monitoring

Create a Healthchecks.io cron check: `0 3 * * *`, timezone `America/New_York`, grace time 60 minutes.

Store its ping URL in Modal:

```bash
uv run modal secret create freshbooks-expense-custodian-healthchecks \
  HEALTHCHECKS_PING_URL=https://hc-ping.com/<check-uuid>
```

Runs ping `/start`, then the base URL on success or `/fail` on exception. Monitoring outages are logged without interrupting expense processing.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Continuous deployment

GitHub Actions runs lint, formatting, and tests on PRs and pushes to `master`. Require `Check formatting and linting` and `Run pytest` in the default-branch ruleset.

Passing pushes to `master` deploy to Modal when Actions secrets `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET` are configured and `MODAL_DEPLOY_ENABLED=true` is set as an Actions variable. Otherwise, deployment is skipped.

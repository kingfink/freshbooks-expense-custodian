# FreshBooks Expense Custodian

A small, stateless Modal job that fixes unmatched bank-created expenses using reconciled FreshBooks history.

## Why this exists

FreshBooks has an `Apply to future expenses` option, but in observed use it only seems to work after FreshBooks has automatically matched the bank transaction's descriptor to a merchant. FreshBooks does not document how that matching works, and in the account this was built for, it fails for roughly 50–75% of imported expenses. When it fails, FreshBooks neither fills in the vendor nor reuses the category mapping, so the same expense must be corrected manually again.

This project is a workaround for that missing general-purpose rule. It learns only from bank-created expenses that have already been matched during reconciliation, uses their corrected vendors and categories as history, and applies clear matches to new unmatched expenses. Ambiguous expenses are left untouched for normal review in FreshBooks.

At 3 AM America/New_York it:

1. Fetches expenses and the Petty Cash General Ledger.
2. Treats bank-created expenses still in Petty Cash as unmatched.
3. Finds each vendor's most recently used category in matched bank-created expenses.
4. Asks PydanticAI to select an exact known vendor or abstain.
5. Applies the vendor and its most recently used category, then emits JSON logs to stdout.

There is no database or watermark. Once an expense is matched, FreshBooks moves it out of Petty Cash and it naturally leaves the candidate set. Repeated runs are idempotent, and Modal forwards stdout through its configured logging integration.

The expense descriptor is sent to OpenAI for vendor selection. Logs contain only a truncated descriptor hash, never the raw text.

## Setup

Select the Modal workspace that should own the deployment. The function injects three named secrets:

- `openai-secret`, containing `OPENAI_API_KEY`.
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

The code uses `transactionid` plus `bank_name` as the bank-created marker and `notes`, then `vendor`, as the descriptor. The Petty Cash ledger supplies the unmatched state. `FRESHBOOKS_PETTY_CASH_ACCOUNT_UUID` is the account-specific system-account identifier resolved during setup.

Install the locked project environment:

```bash
uv sync
```

Deploy and run manually:

```bash
uv run modal deploy app.py
uv run modal run app.py
```

Set `DRY_RUN = True` in `app.py` when validating a routing change. FreshBooks token rotation is persisted back to `freshbooks-secret` automatically.

## Monitoring

Create a Healthchecks.io check with this schedule:

- Schedule type: Cron
- Cron expression: `0 3 * * *`
- Time zone: `America/New_York`
- Grace time: 60 minutes

Store its ping URL in Modal:

```bash
uv run modal secret create freshbooks-expense-custodian-healthchecks \
  HEALTHCHECKS_PING_URL=https://hc-ping.com/<check-uuid>
```

Each scheduled run sends `/start` when it begins, the base URL after it succeeds, and `/fail` if it raises an exception. Healthchecks pings are best-effort: monitoring outages are logged but never fail expense processing.

## Development

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
```

## Continuous deployment

GitHub Actions runs two checks for pull requests and pushes to `master`: `Check formatting and linting` and `Run pytest`. The default-branch ruleset should require both checks after they have appeared in the repository once.

A successful push to `master` also deploys to Modal only after all three repository settings exist:

- Actions secrets `MODAL_TOKEN_ID` and `MODAL_TOKEN_SECRET`.
- Actions variable `MODAL_DEPLOY_ENABLED` set to `true`.

Until then, the deploy job is skipped, so the workflow can be merged before deployment credentials are configured.

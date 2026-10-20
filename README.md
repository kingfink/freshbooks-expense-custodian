# FreshBooks Expense Custodian

A small, stateless Modal job that fixes unmatched bank-created expenses using reconciled FreshBooks history.

## Why this exists

FreshBooks has an `Apply to future expenses` option, but in observed use it only seems to work after FreshBooks has automatically matched the bank transaction's descriptor to a merchant. FreshBooks does not document how that matching works, and in the account this was built for, it fails for roughly 50–75% of imported expenses. When it fails, FreshBooks neither fills in the vendor nor reuses the category mapping, so the same expense must be corrected manually again.

This project is a workaround for that missing general-purpose rule. It learns only from bank-created expenses that have already been matched during reconciliation, uses their corrected vendors and categories as history, and applies clear matches to new unmatched expenses. Ambiguous expenses are left untouched for normal review in FreshBooks.

At 3 AM America/New_York it:

1. Fetches expenses and the Petty Cash General Ledger.
2. Treats bank-created expenses still in Petty Cash as unmatched.
3. Finds each vendor's most recently used category in matched bank-created expenses.
4. Asks pinned Jev 1.13.0 to select an exact known vendor or abstain, requiring confidence of at least 0.9.
5. Applies the vendor and its most recently used category, then emits JSON logs to stdout.

There is no database or watermark. Once an expense is matched, FreshBooks moves it out of Petty Cash and it naturally leaves the candidate set. Repeated runs are idempotent, and Modal forwards stdout through its configured logging integration.

The expense descriptor and known vendor names are sent to TypeSafe for vendor selection. Logs contain only a truncated descriptor hash, never the raw text.

## Setup

Select the Modal workspace that should own the deployment. The function injects three named secrets:

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

Set `DRY_RUN = True` in `app.py` when validating a routing change. Jev returns only supplied vendors or abstains; responses below 0.9 confidence leave expenses untouched. API failures and malformed responses trigger the existing failed-run healthcheck path without falling back to OpenAI. Routing logs include the returned model, confidence, selected probability, and token usage. The seven-day history lag is used only for historical evaluation; production continues to use all reconciled bank history. FreshBooks token rotation is persisted back to `freshbooks-secret` automatically.

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

## Jev experiment

`experiment.py` compares the Jev selector with the former OpenAI selector on reconciled bank expenses. It runs as a separate, unscheduled Modal app; it does not update expenses. FreshBooks OAuth token refresh can still update the existing Modal secret, as in the normal job.

Create a Modal secret named `typesafe-secret` containing `TYPESAFE_API_KEY` in the `finkel-data-and-analytics` workspace, using the Modal dashboard. The experiment also uses `freshbooks-secret` and `openai-secret` (containing `OPENAI_API_KEY`), which is needed only for the comparison baseline. Descriptors and the historical vendor list are sent to TypeSafe and OpenAI; output contains expense IDs, outcomes, model metadata, token usage, and timings, without raw descriptors or vendor names.

Run a bounded comparison with an explicit holdout date:

```bash
MODAL_PROFILE=finkel-data-and-analytics uv run modal run experiment.py --since 2026-10-01 --limit 50 --min-confidence 0.9
```

This freezes the known vendor list to reconciled expenses dated before `--since` and evaluates the first `--limit` eligible expenses on or after that date, ordered by date and expense ID. Only currently reconciled bank expenses with a vendor, category, and valid date qualify. Evaluation rows also require nonempty `notes`: falling back to the corrected `vendor` would reveal the answer. Vendors appearing only after the cutoff remain out-of-list cases, so the model should abstain. A Jev Choice supports 255 options; the runner rejects more than 254 historical vendors to reserve one abstain option.

The selector uses `jev-1.13.0` and abstains below the chosen confidence threshold or when Jev chooses no match. The default 0.9 threshold is an experimental setting, not a measured accuracy guarantee. Invalid responses and API failures are counted separately as errors; neither is treated as a successful abstention. There is no fallback between providers.

Compare `wrong`, `abstained`, `coverage`, and `assignment_accuracy` (correct / non-abstained assignments), plus familiar/unseen descriptor counts and mean latency. Familiar means the normalized descriptor also appears before the cutoff. Token totals and estimated USD API costs are reported for both providers, using version-specific standard rates checked on 2026-10-20 in `experiment.py`. Unknown models or missing usage return a null cost, and a run with errors has no complete cost estimate. Costs exclude Modal compute, credits, discounts, taxes, and future price changes. Errors may still incur provider charges not represented in successful-response token totals. Model names are recorded per row. The scheduled job uses the pinned Jev selector and the same confidence threshold.

Tim confirmed that linking/reconciling an expense in this account represents human review and acceptance of its vendor/category, so those final values are the evaluation labels even when an initial assignment was automated. Unmatched expenses do not qualify as labeled examples. These are current reconciled labels, not historical snapshots: expense dates define the split, while later corrections may affect both sides. Descriptors may also have been edited. Repeated descriptors remain in the sample and can dominate aggregate results. A single cutoff and threshold are exploratory; use a separate later holdout to validate any threshold selected from this run.

### Replay logged decisions with a seven-day vendor lag

The replay accepts a private JSON manifest of log records containing `timestamp`, `expense_id`, `descriptor_hash`, the original selected `vendor`/`categoryid`, and `before.vendor`. Keep this manifest outside the repository. Rows are deduplicated by expense ID using the earliest logged decision. Only currently reconciled bank imports with final labels qualify.

```bash
MODAL_PROFILE=finkel-data-and-analytics uv run modal run experiment.py --since 1970-01-01 --limit 100 --decisions /private/tmp/freshbooks-replay-decisions.json --lag-days 7 --prepare-only
```

Preparation reads FreshBooks and returns eligibility and original-decision audit counts without calling TypeSafe or OpenAI. Remove `--prepare-only` to compare both models after authorization for the dataset. Each expense gets its own vendor/category history, drawn from eligible expenses dated on or before its expense date minus seven days. The test expense is excluded. Final labels are never added to the candidate list merely because they are expected answers. Currently reconciled historical bank expenses provide the same vendor/category eligibility convention as the production router.

Descriptors must match the logged hash, recovered either from current notes or the original pre-update vendor field. Unrecoverable inputs, unmatched expenses, empty histories, and oversized candidate lists are skipped with counts. Replay output includes familiar/unseen and in-list/out-of-list counts, appropriate abstentions for out-of-list vendors, vendor/category pair correctness, API cost estimates, and original logged decisions compared with final labels. The lag reduces potential leakage but cannot prove exactly when a historical vendor correction became available; reconstructed category history still uses current final values.

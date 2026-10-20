# Jev vendor-routing evaluation: 2026-10-20

The scheduled selector is switched from GPT-5-mini to pinned `jev-1.13.0`, using a 0.9 minimum confidence and an explicit no-match option. Category selection continues to use the vendor's latest category from reconciled bank history. The seven-day history lag below is an evaluation rule; it does not restrict production history.

## Historical validation

Tim confirmed that linking/reconciling an expense in this FreshBooks account represents human review and acceptance of its vendor/category. Final reconciled values therefore supply validation labels, including when the original assignment was automated. The repository identifies these as bank imports no longer in the Petty Cash candidate set. Unmatched expenses do not supply labels.

PostHog's `modal.function-logs` stream contained 62 FreshBooks routing records for 60 expense IDs in the searched 30-day window, all logged on 2026-10-20. The replay deduplicated repeated attempts by retaining the earliest decision per expense. Of those 60 expenses, 58 qualified as currently reconciled labeled examples; two were unmatched or otherwise ineligible.

Each original descriptor was recovered from current notes or the logged pre-update vendor and verified against the logged descriptor hash. All 58 eligible inputs were verified. For each test expense, known vendors and category history were reconstructed from eligible, currently reconciled bank expenses dated on or before the test expense date minus seven days, excluding the test expense itself. Each reconstructed candidate list contained 34 vendors. Both models received the same input and choices; final labels were used only for scoring.

[Completed Modal replay](https://modal.com/apps/finkel-data-and-analytics/main/ap-RajFYdoXZbQ138cvz11cSQ). [Aggregate results](jev-replay-2026-10-20.json) contain no descriptors or vendor names. No expenses were changed during evaluation.

| Result | Jev 1.13.0 | GPT-5-mini-2025-08-07 |
| --- | ---: | ---: |
| Expenses evaluated | 58 | 58 |
| Correct vendor/category pairs | 57 | 57 |
| Incorrect assignments | 0 | 0 |
| Abstentions | 1 | 1 |
| API errors | 0 | 0 |
| Assignment coverage | 98.3% | 98.3% |
| Accuracy among assigned expenses | 100% | 100% |
| Mean elapsed seconds per call | 0.0858 | 4.4287 |
| Estimated API cost, USD | $0.00221 | $0.04445 |
| Input tokens | 52,611 | 13,413 |
| Output tokens | 20,698 | 20,548 |

Both models accepted all 57 familiar cases correctly and abstained on the one unfamiliar, out-of-list merchant. Jev's confidence on the abstained case was 0.9: it selected the explicit no-match option rather than failing the below-0.9 gate. The original logged production decisions also matched 57 final vendor/category labels and abstained once.

An earlier [17-expense comparison](https://modal.com/apps/finkel-data-and-analytics/main/ap-Hl4Uiu9LXRRv6HQJfGe2xH) had 16 correct Jev assignments and one abstention, versus 16 correct OpenAI assignments and one error against the final labels. That OpenAI error did not recur in this replay, so the earlier result is not evidence of a stable accuracy disadvantage.

## Cost estimates

[TypeSafe pricing](https://docs.typesafe.ai/models) lists Jev 1.13 at $0.042 per million input tokens with free output. [OpenAI pricing](https://developers.openai.com/api/docs/models/gpt-5-mini) lists GPT-5-mini at $0.25 per million input tokens, $0.025 for cached input, and $2.00 for output. Rates were checked on 2026-10-20. This replay recorded no OpenAI cached input tokens.

Jev was about 51.6 times faster and 20.1 times cheaper in this sequential replay. At the same mean token usage, 1,000 calls would cost approximately $0.0381 for Jev or $0.7664 for GPT-5-mini. This is a linear scenario, not a monthly-volume forecast or production latency guarantee.

Estimates use provider-reported token usage and standard direct API list prices, not verified invoices. They exclude Modal compute, credits, discounts, taxes, and future price changes. Vendor list size, descriptor length, repeated attempts, and repeated evaluation of abstentions affect cost. Failed requests can incur charges without usable token metadata. The runner leaves full estimated cost null for unknown model versions, missing usage, or run errors rather than implying zero cost.

## Limits and operations

The 58 expenses represent only 10 distinct descriptor hashes: nine familiar inputs and one unfamiliar input. This replay exercises neither an unfamiliar in-list merchant nor a category correction. Current labels and category history are not historical snapshots of when corrections first became available. The seven-day lag reduces leakage risk but cannot eliminate it.

The result supports Jev handling the routine workload with matching observed accuracy and lower latency/cost. Novel and ambiguous inputs remain less tested. Production keeps exact-vendor validation, abstention, the existing idempotent update path, failed-run healthchecks, and before/after logging. Jev model, confidence, selected probability, and token usage are included in routing logs. Reverting the production switch restores the former selector; `DRY_RUN = True` disables expense writes while retaining routing output. Switching selectors does not undo past writes.

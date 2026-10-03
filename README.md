# usage-data

Public, auto-refreshed data file powering [maxghenis.com/usage](https://maxghenis.com/usage).

`usage.json` is regenerated every ~30 minutes by `build_usage.py` + `extract.py`,
which read the raw Claude Code and Codex session JSONL on my machine and
aggregate to daily totals. Only daily aggregates are published: token counts,
computed cost at public API list prices, and model breakdowns. No message
content, no file paths, no project names.

## Methodology

- **Dated to when usage happened.** Every API call is bucketed by its own
  timestamp (UTC), not by session start, so sessions that span weeks don't
  pile onto their start date.
- **Bill-faithful codex accounting.** Codex fork/resume writes a snapshot
  file containing a full replay of prior history under a fresh session id.
  Replays are detected **structurally** — the leaf `session_meta` (carrying
  `forked_from_id`) adjacent to the copied ancestor's `session_meta`, with
  the native boundary at the first `task_started` whose preserved
  `started_at` agrees with its own outer timestamp — never by wall-clock
  timing, which misses multi-second copied prefixes and falsely discards
  fresh same-second work. Genuine cumulative-counter resets segment a file
  into billing epochs that are summed; rate-limit-only heartbeats are
  ignored. Only live continuation deltas of `total_token_usage` count.
- **Claude requests deduplicated globally** by `(message.id, requestId)`,
  keeping the **most complete** record (largest token total): subagent
  transcripts stream several usage snapshots per request (the first is a
  placeholder with `output_tokens≈1`; the last carries the billed totals),
  resumed sessions copy history into new transcript files (usually
  verbatim, occasionally truncated), and the pick must not depend on
  which file sorts last.
- **Cache-creation captured.** Claude prompt-cache writes are priced at the
  5m/1h write rates; cache reads at the cache-hit rate.
- **Pricing** is pinned per model generation at public list prices
  (standard tier, no Batch/Flex modifiers; Claude 4.6+ have no
  long-context premium), from Anthropic's pricing page and, for OpenAI,
  cross-checked against LiteLLM's pricing table. A price row applies only
  to its own model id (plus date stamps and Codex variants such as
  `-codex`), so a new generation such as `claude-opus-5-5` can never
  inherit an older row's rates; a model with no row is published at $0
  and named under `pricing.unpriced` in `usage.json`. Claude fast-mode
  requests are priced at their own fast-mode rows.
- **Durability.** A local scan cache (keyed by size, mtime, and accounting-
  algorithm version) retains per-file results after Claude Code rotates
  transcripts (~30 days) or codex archives sessions. For days whose
  transcripts rotated before the cache existed, `claude_history_seed.json`
  carries the Logpile ledger's deduplicated daily totals; per day, whichever
  source knows more tokens wins wholesale.
- **Coverage:** `~/.codex{,-2,-3,-4}/{sessions,archived_sessions}`,
  OpenClaw codex homes, and `~/.claude/projects` (including
  `subagents/` transcripts).

Claude figures match [`ccusage`](https://github.com/ryoppippi/ccusage)
20.0.24 run with `--timezone UTC` to the cent for July 2026, and day by
day for September 1–27 (see RECONCILIATION.md, 2026-09-28).

This methodology reconciles month-by-month with
[Logpile](https://logpile.ai)'s adversarially reviewed strict accounting
(`session_daily_effective`, native columns) — exact on most months, within
~2% elsewhere, with the residual fully attributed to private sessions the
ledger intentionally excludes. It **intentionally diverges from
[`ccusage`](https://github.com/ryoppippi/ccusage)-class counters on Codex**,
which both count inherited fork history whose replay spans multiple
wall-clock seconds and discard usage after genuine counter resets; see
[RECONCILIATION.md](RECONCILIATION.md).

Human/automated split comes from [Logpile](https://logpile.ai) session-origin
classification.

## Tests

```bash
uv run --no-project --with pytest --with hypothesis python -m pytest -q
```

`tests/test_accounting_properties.py` holds the property tests: every
billed Claude request is counted exactly once at its billed vector, per-day
totals equal the sum over unique requests, the result is independent of
file order, and no model id is priced at another generation's rates.

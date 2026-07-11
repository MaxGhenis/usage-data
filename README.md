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
- **Bill-faithful codex accounting.** Codex "resume" writes a snapshot file
  containing a full replay of prior history (re-stamped into a single
  wall-clock second). Replay bursts are detected and excluded — only live
  continuation deltas of the cumulative `total_token_usage` counter count.
- **Claude messages deduplicated globally** by `(message.id, requestId)`,
  since resumed sessions copy history into new transcript files.
- **Cache-creation captured.** Claude prompt-cache writes are priced at the
  5m/1h write rates; cache reads at the cache-hit rate.
- **Pricing** is pinned per model at public list prices (standard tier, no
  Batch/Flex/long-context modifiers), cross-checked against LiteLLM's
  pricing table.
- **Durability.** A local scan cache retains per-file results after Claude
  Code rotates transcripts (~30 days) or codex archives sessions. Claude
  history before 2026-05-09 predates retention and is seeded from the
  Logpile session ledger (`claude_history_seed.json`, session-start-day
  attribution).
- **Coverage:** `~/.codex{,-2,-3}/sessions`, `~/.codex/archived_sessions`,
  OpenClaw codex homes, and `~/.claude/projects`.

This methodology reconciles with [`ccusage`](https://github.com/ryoppippi/ccusage)
monthly totals to within ~10%; see [RECONCILIATION.md](RECONCILIATION.md) for
the 2026-07 audit that led to it.

Human/automated split comes from [Logpile](https://logpile.ai) session-origin
classification.

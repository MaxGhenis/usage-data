# 2026-07-11 reconciliation: dashboard vs ccusage

The dashboard and `bunx ccusage@latest monthly` disagreed badly on monthly
API-equivalent cost. Root-caused 2026-07-11 by recomputing every month from
the raw session JSONL with an independent extractor and auditing both
pipelines' methodologies. **ccusage was right (within ~10%) every month; the
old dashboard was wrong every month** — including July, where its total was
accidentally close because two large errors cancelled.

## Monthly API-equivalent cost ($k, calendar months 2026, as of Jul 11)

| Month | Old dashboard | ccusage | Independent recompute | Fixed dashboard |
|-------|--------------:|--------:|----------------------:|----------------:|
| Mar   | 11.5          | 6.8     | 5.9                   | **7.0**         |
| Apr   | 99.7          | 41.0    | 41.7                  | **42.5**        |
| May   | 179.5         | 94.2    | 93.7                  | **95.9**        |
| Jun   | 20.7          | 73.3    | 64.3                  | **66.2**        |
| Jul 1–11 | 51.6       | 54.9    | 49.0                  | **51.8**        |

Remaining gaps vs ccusage are explained and expected: the dashboard keeps
Claude history from before on-disk transcript rotation (ccusage can no longer
see it; +$1–2k in Mar–May), covers extra `CODEX_HOME` dirs, buckets days in
UTC where ccusage uses local time, and prices 1h cache writes at the 1h rate.

## What was wrong in the old generator

1. **Codex resume snapshots double-counted (the big one).** Resuming a codex
   session writes a new rollout file containing a full replay of prior
   history (re-stamped into one wall-clock second, under a fresh session id)
   plus the live continuation. The generator counted every file's final
   cumulative `total_token_usage`, so a session resumed N times was counted
   ~N+1 times. On Jul 1–11 alone, 573 of 834 rollout files were replay
   snapshots holding 105.5B cached-input tokens of replayed history vs 22.2B
   live. Fix: detect the leading same-second burst and count only live
   deltas (same approach as ccusage).
2. **Whole sessions attributed to their start date.** Long-running sessions
   (some spanning Apr→Jul) put all usage on their start month. This
   inflated Apr/May and starved Jun — the direction flip in the table.
   Fix: per-event timestamps.
3. **claude-fable-5 was unpriced → $0.** 18B tokens of Fable usage (Jun–Jul)
   were published at zero cost. Fix: $10/$1/$50 (+cache-write rates).
4. **Opus 4.5–4.8 priced at legacy Opus rates** ($15/$1.5/$75 instead of
   $5/$0.5/$25) — 3x overpricing on all 2026 Opus usage. Sonnet 5 similarly
   $3/$15 instead of $2/$10.
5. **gpt-5.6-sol priced at a $1.75/$0.175/$14 fallback** instead of its
   $5/$0.5/$30 list price (~2.9x underpricing of July's dominant codex
   model). Errors 1+2 (inflating) vs 3+5 (deflating) happened to cancel in
   July's total.
6. **Claude cache-creation tokens ignored.** Framed as "a small underprice";
   actually ~$9k in Jul 1–11 alone. Now captured and priced (5m/1h).
7. **Coverage gaps.** `~/.codex/archived_sessions` (991 files / 26GB,
   mostly Mar–May, never ingested), `~/.codex-2`, `~/.codex-3`, and OpenClaw
   codex homes were not scanned. Claude messages duplicated across resumed
   session files (66.8B tokens) were not deduplicated.

## What ccusage gets slightly wrong (for this setup)

- No access to Claude Code transcripts older than the ~30-day on-disk
  rotation (its Mar/Apr Claude ≈ $0; the ledger-seeded dashboard keeps them).
- Doesn't scan extra `CODEX_HOME` dirs (`~/.codex-2`, `~/.codex-3`) or
  OpenClaw codex homes (small here).

## The fix

`extract.py` now does bill-faithful extraction (burst-skip, per-event
dating, global Claude dedup, cache-write capture, corrected pinned pricing,
full coverage) with an mtime-keyed scan cache so rotated/archived files keep
contributing; `claude_history_seed.json` preserves pre-rotation Claude
history from the Logpile ledger. Accounting rules are unit-tested in
`tests/test_extract.py`.

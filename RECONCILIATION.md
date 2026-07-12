# 2026-07-12 reconciliation: dashboard vs logpile strict accounting

The 2026-07-11 rebuild below reconciled the dashboard to ccusage. Logpile's
adversarial pre-launch review (findings B3+B4, `logpile
docs/reviews/2026-07-11-sol-full-review.md`) then established that
**ccusage-class Codex counting itself overcounts** through two paths this
tracker shared:

1. **Multi-second fork replays.** Same-second burst detection assumes a
   copied prefix cannot cross a second boundary. Real snapshots do (one
   replayed 36,480 records across seconds), so inherited history was
   counted as native. Conversely, 231 fresh files that legitimately wrote
   several counters in one second had live work discarded. Detection is
   now structural: leaf `session_meta` with `forked_from_id` adjacent to
   the copied ancestor's `session_meta`, native boundary at the first
   `task_started` whose preserved `started_at` matches its own outer
   clock.
2. **Discarded counter-reset epochs.** The componentwise-max baseline
   treated every cumulative-counter reset as duplication. One real root
   session reached 8.7B input tokens, reset to zero, and continued for
   weeks — everything after the reset was silently dropped. Explicit
   all-zero vectors now end a billing epoch and epochs are summed; partial
   telemetry wobbles still clamp.

The same rebuild fixed a latent Claude bug the logpile comparison exposed:
subagent transcripts emit several usage records per `(message.id,
requestId)` — the first is the message_start placeholder with
`output_tokens≈1`, the last carries the billed totals — and first-wins
dedup kept the placeholder (July 2026 alone: 71M output tokens
undercounted, ~$3.5k). Dedup is now last-wins. The Claude history seed was
also regenerated from the ledger's post-review native columns with
day-level arbitration (per day, scan or seed wholesale — per-model
arbitration double-counts because the ledger buckets whole sessions under
one model). Coverage added `~/.codex-4` and archived_sessions for all
codex homes.

## Monthly API-equivalent cost ($k, as of Jul 12)

| Month | ccusage-method (7/11 table) | Strict (7/12) | Codex-only strict |
|-------|----------------------------:|--------------:|------------------:|
| Mar   | 7.0   | **4.9**  | 3.3  |
| Apr   | 42.5  | **11.5** | 10.9 |
| May   | 95.9  | **39.0** | 29.8 |
| Jun   | 66.2  | **31.9** | 16.4 |
| Jul (→12) | 51.8 (→11th) | **74.1** | 31.9 |
| Lifetime | ~276.7 (as of 7/12 build) | **163.4** | — |

Mar–Jun drop because inherited fork history is no longer counted (the
fork-heavy campaign months). July rises: it gains a day and a half of new
usage, the recovered post-reset epoch, and the corrected Claude output.

## Verification against logpile `session_daily_effective`

Month-by-month, native (deduplicated) columns, same corpus scope
(`.codex-4` excluded from the comparison since logpile does not scan it):

- **Codex:** Mar/May/Jun exact to the token (+0.00%); Apr +1.6% input /
  +2.0% output; Jul +0.3% / +0.4%. The Apr/Jul residual is fully
  attributed: 28 rollouts logpile excludes as private sessions or
  unparseable (0.41B input — the dashboard counts their tokens since the
  usage was billed; only content-free aggregates are published) plus 5
  private-session stubs retaining partial ledger rows.
- **Claude:** Jan–Jun exact (≤0.01%); Jul +0.9% input / +1.3% output —
  the live scan runs ahead of the ledger's last sync and includes private
  sessions.

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

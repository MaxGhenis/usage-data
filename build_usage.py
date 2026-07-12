#!/usr/bin/env python3
"""Build usage.json for maxghenis.com/usage.

Data source: raw Claude Code and Codex session JSONL on this machine,
processed by extract.py (see its docstring for the accounting rules).
Headlines: usage is dated to when each API call happened (per-event
timestamps, UTC), codex fork/resume replays are excluded via structural
detection (not timing heuristics), genuine codex counter resets are
summed as billing epochs, Claude messages are deduplicated across
resumed session files, and Claude cache-creation tokens are captured and
priced. A scan cache plus a one-time seed from the Logpile ledger keep
months alive after the CLIs rotate their on-disk transcripts.

Dollar figures are raw per-model token counts at public API list prices
(standard tier; no Batch/Flex/long-context modifiers), cross-checked
against LiteLLM's pricing table. This methodology reconciles with
logpile's adversarially reviewed strict accounting (session_daily_
effective). It intentionally diverges from `ccusage monthly` on Codex:
ccusage-class same-second replay detection both counts inherited fork
history that spans multiple wall-clock seconds and discards usage after
genuine cumulative-counter resets (see RECONCILIATION.md).

Output schema (unchanged):
{
  "generatedAt": ISO8601,
  "dateRange": {start, end},
  "daily": [ {date, human:{claude,codex,other}, automated:{claude,codex,other}} ],
            each client bucket = {tokens, cost, msgs, prompts}
  "summary": {week, month, lifetime},
  "byModel": [ {client, model, priceSource, human:{tokens,cost},
                automated:{tokens,cost}, all:{tokens,cost}} ],
  "pricing": {note, models:[...]},
  "leaderboards": {tokscale, straude}
}
"""

from __future__ import annotations

import json
import glob
import sqlite3
import urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from collections import defaultdict

from extract import (
    HUMAN_ORIGINS,
    LOGPILE_DB,
    N_FIELDS,
    PRICING,
    connect_logpile_ro,
    cost_usd,
    extract_daily,
    resolve_price,
)

USERNAME = "maxghenis"


def _bucket():
    return {"tokens": 0, "cost": 0.0, "msgs": 0, "prompts": 0}


# ─────────────────────────────────────────────────────────
# Real user-typed prompts (independent of token accounting; covers the
# full Claude Code history-file retention, which reaches back further
# than the per-project session files).
# ─────────────────────────────────────────────────────────
_CLAUDE_SKIP_DISPLAY = {"/clear", "/compact", "exit", "exit0", "exit1"}


def parse_real_user_prompts():
    """Walk Claude Code + Codex sources for real user-typed prompts.

    Returns {date: {"claude": int, "codex": int}}.

    Claude: union (max per day) of ~/.claude/history.jsonl and the
    per-project session files. Codex: user_message events in session JSONL,
    skipping automated exec / subagent sessions.
    """
    daily = defaultdict(lambda: {"claude": 0, "codex": 0})

    cc_per_day_history: dict[str, int] = defaultdict(int)
    cc_per_day_projects: dict[str, int] = defaultdict(int)

    history_path = Path.home() / ".claude" / "history.jsonl"
    try:
        with open(history_path, encoding="utf-8", errors="replace") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                display = (obj.get("display") or "").strip()
                if not display or display in _CLAUDE_SKIP_DISPLAY:
                    continue
                ts_ms = obj.get("timestamp", 0)
                try:
                    d = datetime.fromtimestamp(ts_ms / 1000).date()
                except Exception:
                    continue
                cc_per_day_history[d.isoformat()] += 1
    except OSError:
        pass

    cc_files = glob.glob(
        str(Path.home() / ".claude" / "projects" / "**" / "*.jsonl"),
        recursive=True,
    )
    for fp in cc_files:
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "user":
                        continue
                    if obj.get("isSidechain") or obj.get("userType") != "external":
                        continue
                    msg = obj.get("message", {})
                    if msg.get("role") != "user":
                        continue
                    content = msg.get("content", "")
                    if isinstance(content, list) and all(
                        isinstance(c, dict) and c.get("type") == "tool_result"
                        for c in content
                    ):
                        continue
                    if isinstance(content, str) and not content.strip():
                        continue
                    ts = obj.get("timestamp", "")
                    try:
                        d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                    except Exception:
                        continue
                    cc_per_day_projects[d.isoformat()] += 1
        except OSError:
            continue

    for day in set(cc_per_day_history) | set(cc_per_day_projects):
        daily[day]["claude"] = max(
            cc_per_day_history.get(day, 0), cc_per_day_projects.get(day, 0)
        )

    cx_files = sorted(
        glob.glob(
            str(Path.home() / ".codex" / "sessions" / "**" / "*.jsonl"),
            recursive=True,
        )
    ) + sorted(
        glob.glob(str(Path.home() / ".codex" / "archived_sessions" / "*.jsonl"))
    )
    for fp in cx_files:
        try:
            with open(fp, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            if not lines:
                continue
            try:
                first = json.loads(lines[0])
            except json.JSONDecodeError:
                continue
            is_new_format = first.get("type") == "session_meta"
            if is_new_format:
                source = first.get("payload", {}).get("source")
                if isinstance(source, dict) or source == "exec":
                    continue
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if (
                        obj.get("type") == "event_msg"
                        and obj.get("payload", {}).get("type") == "user_message"
                    ):
                        ts = obj.get("timestamp", "")
                        try:
                            d = datetime.fromisoformat(ts.replace("Z", "+00:00")).date()
                        except Exception:
                            continue
                        daily[d.isoformat()]["codex"] += 1
            else:
                try:
                    session_d = datetime.fromisoformat(
                        first.get("timestamp", "").replace("Z", "+00:00")
                    ).date()
                except Exception:
                    continue
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "message" or obj.get("role") != "user":
                        continue
                    content = obj.get("content", [])
                    is_wrapper = False
                    has_text = False
                    if isinstance(content, list):
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            txt = c.get("text", "")
                            if txt.startswith("<environment_context>") or txt.startswith(
                                "<user_instructions>"
                            ):
                                is_wrapper = True
                                break
                            if txt.strip():
                                has_text = True
                    if is_wrapper or not has_text:
                        continue
                    daily[session_d.isoformat()]["codex"] += 1
        except OSError:
            continue

    return daily


# ─────────────────────────────────────────────────────────
# Message counts, from the Logpile ledger (session-start-day attribution;
# counts only, not used for cost).
# ─────────────────────────────────────────────────────────
def read_msgs_by_day():
    if not LOGPILE_DB.exists():
        return {}
    rows = None
    for attempt in range(3):
        try:
            con = connect_logpile_ro()
            rows = con.execute(
                """
                SELECT substr(first_timestamp, 1, 10) AS day,
                       source,
                       session_origin AS origin,
                       SUM(user_message_count + assistant_message_count) AS msgs
                FROM sessions
                WHERE username = ? AND first_timestamp IS NOT NULL AND first_timestamp != ''
                GROUP BY day, source, origin
                """,
                (USERNAME,),
            ).fetchall()
            con.close()
            break
        except sqlite3.Error:
            import time

            time.sleep(2 * (attempt + 1))
    if rows is None:
        # Ledger locked by a concurrent sync; message counts are cosmetic,
        # so skip them this cycle rather than failing the build.
        print("  (logpile.db locked; skipping message counts this run)")
        return {}
    out = defaultdict(lambda: {"human": defaultdict(int), "automated": defaultdict(int)})
    for day, source, origin, msgs in rows:
        client = "claude" if source == "claudecode" else "codex" if source == "codex" else "other"
        group = "human" if origin in HUMAN_ORIGINS else "automated"
        out[day][group][client] += msgs or 0
    return out


def fetch_leaderboards():
    """Fetch live tokscale ranks. Falls back to last-known values (with an
    asOf date) if the network call fails, so the build never breaks."""
    tokscale = {
        "url": "https://tokscale.ai/u/MaxGhenis",
        "rank": {},
        "users": None,
        "asOf": None,
    }

    def lookup(period):
        url = f"https://tokscale.ai/api/leaderboard?period={period}&limit=1000"
        req = urllib.request.Request(url, headers={"User-Agent": "usage-tracker"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read().decode())
        users = data.get("users", [])
        pg = data.get("pagination") or {}
        total = pg.get("totalUsers") or (data.get("stats") or {}).get("uniqueUsers")
        me = next(
            (u for u in users if str(u.get("username", "")).lower() == "maxghenis"),
            None,
        )
        return (me.get("rank") if me else None), total

    try:
        for period, key in (("week", "week"), ("month", "month"), ("all", "allTime")):
            rank, total = lookup(period)
            if rank is not None:
                tokscale["rank"][key] = rank
            if period == "all" and total:
                tokscale["users"] = total
        tokscale["asOf"] = datetime.now(timezone.utc).date().isoformat()
    except Exception as e:  # noqa: BLE001
        print(f"  (leaderboard fetch failed: {e}; using last-known)")
        tokscale["rank"] = {"month": 4, "allTime": 8}
        tokscale["users"] = 252
        tokscale["asOf"] = "2026-05-31"

    straude = {
        "url": "https://straude.com/u/maxghenis",
        "embedSvg": "https://straude.com/api/embed/maxghenis/svg",
    }
    return {"tokscale": tokscale, "straude": straude}


def build(daily_usage, msgs_by_day, prompts_by_date, leaderboards):
    if not daily_usage:
        return None

    by_date: dict[str, dict] = {}
    by_model: dict[tuple, dict] = {}

    for day, groups in daily_usage.items():
        if not day or len(day) != 10:
            continue
        row = by_date.setdefault(
            day,
            {
                "human": {"claude": _bucket(), "codex": _bucket(), "other": _bucket()},
                "automated": {"claude": _bucket(), "codex": _bucket(), "other": _bucket()},
            },
        )
        for group, clients in groups.items():
            for client, models in clients.items():
                for model, v in models.items():
                    tokens = sum(v)
                    cost = cost_usd(model, v)
                    b = row[group][client]
                    b["tokens"] += tokens
                    b["cost"] += cost

                    mk = (client, model)
                    mrow = by_model.setdefault(
                        mk,
                        {
                            "human": {"tokens": 0, "cost": 0.0},
                            "automated": {"tokens": 0, "cost": 0.0},
                            "source": resolve_price(model)["source"],
                        },
                    )
                    mrow[group]["tokens"] += tokens
                    mrow[group]["cost"] += cost

    for day, groups in msgs_by_day.items():
        row = by_date.get(day)
        if row is None:
            continue
        for group in ("human", "automated"):
            for client, n in groups[group].items():
                if client in row[group]:
                    row[group][client]["msgs"] += n

    for day, p in prompts_by_date.items():
        row = by_date.get(day)
        if row is None:
            continue
        row["human"]["claude"]["prompts"] = p.get("claude", 0)
        row["human"]["codex"]["prompts"] = p.get("codex", 0)

    for row in by_date.values():
        for group in ("human", "automated"):
            for client in ("claude", "codex", "other"):
                row[group][client]["cost"] = round(row[group][client]["cost"], 2)

    # Gap-fill one row per calendar day so the chart x-axis is linear.
    all_iso = sorted(by_date.keys())
    start_d = date.fromisoformat(all_iso[0])
    end_d = date.fromisoformat(all_iso[-1])
    daily_rows = []
    cur = start_d
    while cur <= end_d:
        iso = cur.isoformat()
        daily_rows.append(
            {
                "date": iso,
                **(
                    by_date.get(iso)
                    or {
                        "human": {"claude": _bucket(), "codex": _bucket(), "other": _bucket()},
                        "automated": {"claude": _bucket(), "codex": _bucket(), "other": _bucket()},
                    }
                ),
            }
        )
        cur += timedelta(days=1)

    def window(rows, start: date, end: date):
        out = {
            g: {
                "claude": _bucket(),
                "codex": _bucket(),
                "other": _bucket(),
                "total": _bucket(),
            }
            for g in ("human", "automated", "all")
        }
        for r in rows:
            d = date.fromisoformat(r["date"])
            if not (start <= d <= end):
                continue
            for group in ("human", "automated"):
                for client in ("claude", "codex", "other"):
                    src = r[group][client]
                    for dest in (out[group][client], out["all"][client]):
                        dest["tokens"] += src["tokens"]
                        dest["cost"] += src["cost"]
                        dest["msgs"] += src["msgs"]
                        dest["prompts"] += src["prompts"]
        for g in ("human", "automated", "all"):
            for client in ("claude", "codex", "other"):
                c = out[g][client]
                c["cost"] = round(c["cost"], 2)
                for f in ("tokens", "cost", "msgs", "prompts"):
                    out[g]["total"][f] += c[f]
            out[g]["total"]["cost"] = round(out[g]["total"]["cost"], 2)
        return out

    today = date.today()
    summary = {
        "week": window(daily_rows, today - timedelta(days=6), today),
        "month": window(daily_rows, today - timedelta(days=29), today),
        "lifetime": window(daily_rows, start_d, max(end_d, today)),
    }

    by_model_out = []
    for (client, model), v in by_model.items():
        h, a = v["human"], v["automated"]
        by_model_out.append(
            {
                "client": client,
                "model": model,
                "priceSource": v["source"],
                "human": {"tokens": h["tokens"], "cost": round(h["cost"], 2)},
                "automated": {"tokens": a["tokens"], "cost": round(a["cost"], 2)},
                "all": {
                    "tokens": h["tokens"] + a["tokens"],
                    "cost": round(h["cost"] + a["cost"], 2),
                },
            }
        )
    by_model_out.sort(key=lambda r: -r["all"]["cost"])

    pricing_models = [
        {
            "tier": k,
            "input": v["input"],
            "cached": v["cached"],
            "output": v["output"],
            **({"cacheWrite5m": v["cw5m"], "cacheWrite1h": v["cw1h"]} if "cw5m" in v else {}),
            "source": v["source"],
        }
        for k, v in PRICING.items()
    ]

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dateRange": {"start": start_d.isoformat(), "end": max(end_d, today).isoformat()},
        "daily": daily_rows,
        "summary": summary,
        "byModel": by_model_out,
        "pricing": {
            "note": (
                "Costs computed from raw per-model token counts at public API "
                "list prices (standard short-context; no Batch/Flex/long-context "
                "modifiers), cross-checked against LiteLLM. Usage is dated to "
                "when each API call happened (per-message timestamps, UTC). "
                "Codex fork/resume replays are excluded via structural "
                "detection (forked_from_id lineage + task_started clock "
                "agreement) and genuine counter resets are summed as billing "
                "epochs; Claude messages are deduplicated across resumed "
                "sessions; Claude cache-creation (write) tokens are captured "
                "and priced at 5m/1h rates. Claude history before 2026-05-09 "
                "predates on-disk transcript retention and is seeded from the "
                "Logpile session ledger (session-start-day attribution, no "
                "cache-write data)."
            ),
            "models": pricing_models,
        },
        "leaderboards": leaderboards,
    }


def main():
    print("Extracting daily usage from session JSONL (cached scan)...")
    daily_usage = extract_daily()
    print("Reading message counts from logpile.db...")
    msgs_by_day = read_msgs_by_day()
    print("Parsing JSONL for real user prompts...")
    prompts_by_date = parse_real_user_prompts()
    print("Fetching leaderboards...")
    leaderboards = fetch_leaderboards()
    output = build(daily_usage, msgs_by_day, prompts_by_date, leaderboards)

    out_path = Path(__file__).resolve().parent / "usage.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    s = output["summary"]
    print(f"\nWrote {out_path}")
    print(f"  Date range: {output['dateRange']['start']} → {output['dateRange']['end']}")
    for w in ("week", "month", "lifetime"):
        for g in ("human", "automated", "all"):
            t = s[w][g]["total"]
            print(
                f"  {w:10} {g:10}: ${t['cost']:>10,.0f} / {t['tokens']/1e9:>6.1f}B"
                f"  ({t['prompts']:,} prompts)"
            )


if __name__ == "__main__":
    main()

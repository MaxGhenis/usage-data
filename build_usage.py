#!/usr/bin/env python3
"""Build usage.json for maxghenis.com/usage.

Data source: the local Logpile SQLite ledger (~/logpile/logpile.db), which
continuously indexes every Claude Code and Codex session into a durable
table. Logpile is the system of record, so it retains history long after
Claude Code / Codex rotate their on-disk JSONL away, and it tags each
session with a workflow origin (human-driven vs automated). Dollar figures
are computed here from raw per-model token counts at public API list prices.

Two things make the token math source-specific (see read_logpile):
  * Claude:  usage.input_tokens excludes cache, so fresh + cached are
             disjoint; logpile's total_input is correct (it omits
             cache-CREATION tokens, a small underprice).
  * Codex:   the cumulative total_token_usage.input_tokens already INCLUDES
             cached tokens, so logpile's stored total_input double-counts
             cache. We use input(=incl-cache) + output as the true total and
             derive uncached = input - cached.

Output schema:
{
  "generatedAt": ISO8601,
  "dateRange": {start, end},
  "daily": [ {date, human:{claude,codex,other}, automated:{claude,codex,other}} ],
            each client bucket = {tokens, cost, msgs, prompts}
  "summary": {week, month, lifetime},
            each = {human:{...,total}, automated:{...,total}, all:{...,total}}
  "byModel": [ {client, model, priceSource, human:{tokens,cost},
                automated:{tokens,cost}, all:{tokens,cost}} ],
  "pricing": {note, models:[{tier, input, cached, output, source}]},
  "leaderboards": {tokscale, straude}
}
'all' = human + automated (summed on the client). Prompts are real
user-typed prompts (parsed from session JSONL); they are human by
definition, so automated prompts are always 0.
"""

from __future__ import annotations

import json
import glob
import sqlite3
import urllib.request
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from typing import Optional

LOGPILE_DB = Path.home() / "logpile" / "logpile.db"
USERNAME = "maxghenis"

# ─────────────────────────────────────────────────────────
# Pricing — public API list prices, $ per 1M tokens.
# ─────────────────────────────────────────────────────────
# cached = cache-read rate. OpenAI's published cache discount is 90%
# (cached = 0.1 x input); Anthropic publishes cache-read explicitly.
# Long-context / Batch / Flex / Priority modifiers are NOT applied — these
# are standard short-context list rates. Codex's uncached input is a small
# fraction of volume (most input is cached), so this assumption is minor.
PRICING = {
    "opus":          {"input": 15.0, "cached": 1.50,  "output": 75.0, "source": "Anthropic list"},
    "sonnet":        {"input": 3.0,  "cached": 0.30,  "output": 15.0, "source": "Anthropic list"},
    "haiku":         {"input": 1.0,  "cached": 0.10,  "output": 5.0,  "source": "Anthropic list"},
    "gpt-5.5":       {"input": 5.0,  "cached": 0.50,  "output": 30.0, "source": "OpenAI list"},
    "gpt-5.4":       {"input": 2.50, "cached": 0.25,  "output": 15.0, "source": "OpenAI list"},
    "gpt-5.4-mini":  {"input": 0.75, "cached": 0.075, "output": 4.50, "source": "OpenAI list"},
    "gpt-5.2-codex": {"input": 1.75, "cached": 0.175, "output": 14.0, "source": "OpenAI list"},
    "gpt-4.1":       {"input": 2.0,  "cached": 0.20,  "output": 8.0,  "source": "OpenAI list"},
}


def resolve_price(model: str) -> tuple[dict, str]:
    """Map a raw model id to a price tier. Returns (rates, source_label)."""
    m = (model or "").lower()
    if m.startswith("claude-opus"):
        return PRICING["opus"], PRICING["opus"]["source"]
    if m.startswith("claude-sonnet"):
        return PRICING["sonnet"], PRICING["sonnet"]["source"]
    if m.startswith("claude-haiku"):
        return PRICING["haiku"], PRICING["haiku"]["source"]
    if m.startswith("gpt-5") and "mini" in m:
        return PRICING["gpt-5.4-mini"], PRICING["gpt-5.4-mini"]["source"]
    if m.startswith("gpt-5.5"):
        return PRICING["gpt-5.5"], PRICING["gpt-5.5"]["source"]
    if m.startswith("gpt-5.4"):
        return PRICING["gpt-5.4"], PRICING["gpt-5.4"]["source"]
    if m.startswith("gpt-4.1"):
        return PRICING["gpt-4.1"], PRICING["gpt-4.1"]["source"]
    if m.startswith(("gpt-5.3", "gpt-5.2", "gpt-5.1", "gpt-5")):
        # Low-volume tail; price at the gpt-5.2-codex rate.
        return PRICING["gpt-5.2-codex"], "estimated (gpt-5.2-codex rate)"
    return {"input": 0.0, "cached": 0.0, "output": 0.0}, "unpriced"


HUMAN_ORIGINS = {"human_direct", "human_delegated"}


def origin_group(origin: str) -> str:
    return "human" if origin in HUMAN_ORIGINS else "automated"


def client_of(source: str) -> str:
    if source == "claudecode":
        return "claude"
    if source == "codex":
        return "codex"
    return "other"


def _bucket():
    return {"tokens": 0, "cost": 0.0, "msgs": 0, "prompts": 0}


# ─────────────────────────────────────────────────────────
# Real user-typed prompts (independent of Logpile; covers the full
# Claude Code history-file retention, which reaches back further than the
# per-project session files).
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
# Logpile read + pricing
# ─────────────────────────────────────────────────────────
def read_logpile():
    """Aggregate logpile.db into per-day, per-origin, per-client buckets and
    a per-model rollup. Returns (by_date, by_model).

    by_date[date][group][client] = {tokens, cost, msgs}
    by_model[(client, model)]    = {human:{tokens,cost}, automated:{...}, source}
    """
    if not LOGPILE_DB.exists():
        raise RuntimeError(f"logpile DB not found at {LOGPILE_DB}")
    con = sqlite3.connect(str(LOGPILE_DB))
    rows = con.execute(
        """
        SELECT substr(first_timestamp, 1, 10) AS day,
               source,
               COALESCE(NULLIF(model, ''), '(unknown)') AS model,
               session_origin AS origin,
               SUM(fresh_input_tokens)   AS fresh,
               SUM(cached_input_tokens)  AS cached,
               SUM(total_output_tokens)  AS output,
               SUM(user_message_count + assistant_message_count) AS msgs
        FROM sessions
        WHERE username = ? AND first_timestamp IS NOT NULL AND first_timestamp != ''
        GROUP BY day, source, model, origin
        """,
        (USERNAME,),
    ).fetchall()
    con.close()

    by_date: dict[str, dict] = {}
    by_model: dict[tuple, dict] = {}

    for day, source, model, origin, fresh, cached, output, msgs in rows:
        fresh = fresh or 0
        cached = cached or 0
        output = output or 0
        msgs = msgs or 0
        client = client_of(source)
        group = origin_group(origin)
        rates, src_label = resolve_price(model)

        if source == "codex":
            # input_tokens already includes cached; total = input + output.
            tokens = fresh + output
            uncached = max(0, fresh - cached)
        else:
            # Claude: fresh and cached are disjoint.
            tokens = fresh + cached + output
            uncached = fresh
        cost = (
            uncached * rates["input"]
            + cached * rates["cached"]
            + output * rates["output"]
        ) / 1_000_000

        row = by_date.setdefault(
            day,
            {
                "human": {"claude": _bucket(), "codex": _bucket(), "other": _bucket()},
                "automated": {"claude": _bucket(), "codex": _bucket(), "other": _bucket()},
            },
        )
        b = row[group][client]
        b["tokens"] += tokens
        b["cost"] += cost
        b["msgs"] += msgs

        mk = (client, model)
        mrow = by_model.setdefault(
            mk,
            {
                "human": {"tokens": 0, "cost": 0.0},
                "automated": {"tokens": 0, "cost": 0.0},
                "source": src_label,
            },
        )
        mrow[group]["tokens"] += tokens
        mrow[group]["cost"] += cost

    return by_date, by_model


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


def build(by_date, by_model, prompts_by_date, leaderboards):
    if not by_date:
        return None

    # Merge real prompts into the human bucket (prompts are human by def.).
    for day, p in prompts_by_date.items():
        row = by_date.get(day)
        if row is None:
            continue
        row["human"]["claude"]["prompts"] = p.get("claude", 0)
        row["human"]["codex"]["prompts"] = p.get("codex", 0)

    # Round costs.
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
        {"tier": k, **{f: v[f] for f in ("input", "cached", "output", "source")}}
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
                "modifiers). Cached input billed at each provider's cache-read "
                "rate. Claude cache-creation tokens are not captured by Logpile, "
                "a small underestimate."
            ),
            "models": pricing_models,
        },
        "leaderboards": leaderboards,
    }


def main():
    print("Reading logpile.db...")
    by_date, by_model = read_logpile()
    print("Parsing JSONL for real user prompts...")
    prompts_by_date = parse_real_user_prompts()
    print("Fetching leaderboards...")
    leaderboards = fetch_leaderboards()
    output = build(by_date, by_model, prompts_by_date, leaderboards)

    out_path = Path.home() / "usage-tracker" / "usage.json"
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

#!/usr/bin/env python3
"""Build usage.json for maxghenis.com/usage page.

Data sources:
1. Tokscale's `graph` JSON output — accurate LiteLLM pricing for tokens
   and cost across Claude Code + Codex + Gemini + OpenClaw.
2. Direct JSONL parsing — real user-typed prompt counts per day. Tokscale's
   "messages" field counts ALL message records (assistant + tool results +
   system), so it's not a clean "how many things did Max type" metric.

The two are merged into the daily rows so the chart can show either.

Output schema:
{
  "generatedAt": ISO8601,
  "dateRange": {start, end},
  "daily": [
    {"date", "claude": {tokens, cost, msgs, prompts}, "codex": {...}, "other": {...}}
  ],
  "summary": {"week", "month", "lifetime": {claude, codex, other, total}},
  "byModel": [{client, model, tokens, cost, share}],
  "leaderboards": {...}
}
"""

from __future__ import annotations

import json
import subprocess
import glob
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from collections import defaultdict
from typing import Optional


_CLAUDE_SKIP_DISPLAY = {"/clear", "/compact", "exit", "exit0", "exit1"}


def parse_real_user_prompts():
    """Walk Claude Code + Codex sources for real user-typed prompts.

    Returns: {date: {"claude": int, "codex": int}}

    Sources:
      Claude Code: ~/.claude/history.jsonl (the shell history of typed
        prompts; covers the full retention window of Claude Code, much
        further back than the per-project session files). UI commands
        like /clear, /compact, exit are skipped.
      Codex: per-session JSONL files in ~/.codex/sessions/** and
        ~/.codex/archived_sessions/. New format uses
        event_msg/user_message events; old format uses raw message
        records (with environment_context wrappers filtered out).
    """
    print("Parsing JSONL files for real user prompts...")
    daily = defaultdict(lambda: {"claude": 0, "codex": 0})

    # ─ Claude Code: union of two sources, take max per day
    #   1. ~/.claude/history.jsonl (covers full retention but only flushes
    #      when sessions exit, so today's running sessions are missing)
    #   2. ~/.claude/projects/**/*.jsonl (per-session JSONL written
    #      incrementally, but only goes back ~3 months; Claude Code rotates
    #      these out on disk)
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
                        d = datetime.fromisoformat(
                            ts.replace("Z", "+00:00")
                        ).date()
                    except Exception:
                        continue
                    cc_per_day_projects[d.isoformat()] += 1
        except OSError:
            continue

    all_cc_days = set(cc_per_day_history.keys()) | set(cc_per_day_projects.keys())
    for day in all_cc_days:
        daily[day]["claude"] = max(
            cc_per_day_history.get(day, 0),
            cc_per_day_projects.get(day, 0),
        )

    # ─ Codex
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
                # Skip subagent dict-source and automated exec sessions
                # for the user-prompts count specifically
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
                            d = datetime.fromisoformat(
                                ts.replace("Z", "+00:00")
                            ).date()
                        except Exception:
                            continue
                        daily[d.isoformat()]["codex"] += 1
            else:
                # Old format — no per-message timestamps; use session start
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
                    if obj.get("type") != "message":
                        continue
                    if obj.get("role") != "user":
                        continue
                    content = obj.get("content", [])
                    is_wrapper = False
                    has_text = False
                    if isinstance(content, list):
                        for c in content:
                            if not isinstance(c, dict):
                                continue
                            txt = c.get("text", "")
                            if txt.startswith("<environment_context>") or \
                               txt.startswith("<user_instructions>"):
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

CLAUDE_CLIENTS = {"claude"}
CODEX_CLIENTS = {"codex"}


def run_tokscale_graph():
    """Call `bunx tokscale@latest graph` and return parsed JSON."""
    print("Running tokscale graph...")
    result = subprocess.run(
        [str(Path.home() / ".bun" / "bin" / "bunx"), "tokscale@latest", "graph"],
        capture_output=True,
        text=True,
        timeout=600,
    )
    if result.returncode != 0:
        raise RuntimeError(f"tokscale graph failed: {result.stderr}")
    # Strip any preamble before the first '{'
    stdout = result.stdout
    start = stdout.find("{")
    if start == -1:
        raise RuntimeError("no JSON in tokscale output")
    return json.loads(stdout[start:])


def classify_client(client_name: str) -> str:
    c = client_name.lower()
    if c in CLAUDE_CLIENTS:
        return "claude"
    if c in CODEX_CLIENTS:
        return "codex"
    return "other"


# ─────────────────────────────────────────────────────────
# Pricing override
# ─────────────────────────────────────────────────────────
# Tokscale's bundled LiteLLM data has Anthropic Opus 4.6 priced at $5/M
# input (3x lower than the real Anthropic published rate of $15/M).
# We override Anthropic Claude model prices here using the real published
# rates so the cost numbers reflect actual list prices.
ANTHROPIC_PRICING = {
    # Opus tier
    "claude-opus-4-6":  {"input": 15, "output": 75, "cache_read": 1.50, "cache_write": 18.75},
    "claude-opus-4-5":  {"input": 15, "output": 75, "cache_read": 1.50, "cache_write": 18.75},
    "opus-4-6":         {"input": 15, "output": 75, "cache_read": 1.50, "cache_write": 18.75},
    "opus-4-5":         {"input": 15, "output": 75, "cache_read": 1.50, "cache_write": 18.75},
    # Sonnet tier
    "claude-sonnet-4-6":  {"input": 3,  "output": 15, "cache_read": 0.30, "cache_write": 3.75},
    "claude-sonnet-4-5":  {"input": 3,  "output": 15, "cache_read": 0.30, "cache_write": 3.75},
    "sonnet-4-6":         {"input": 3,  "output": 15, "cache_read": 0.30, "cache_write": 3.75},
    "sonnet-4-5":         {"input": 3,  "output": 15, "cache_read": 0.30, "cache_write": 3.75},
    # Haiku tier
    "claude-haiku-4-5":   {"input": 1,  "output": 5,  "cache_read": 0.10, "cache_write": 1.25},
    "haiku-4-5":          {"input": 1,  "output": 5,  "cache_read": 0.10, "cache_write": 1.25},
}


def override_cost(client: str, model: str, tokens: dict) -> Optional[float]:
    """Apply our own pricing for Anthropic Claude models. Returns None if
    no override applies (caller should use Tokscale's cost as-is)."""
    if client != "claude":
        return None
    p = ANTHROPIC_PRICING.get(model)
    if not p:
        return None
    return (
        tokens.get("input", 0) * p["input"]
        + tokens.get("output", 0) * p["output"]
        + tokens.get("cacheRead", 0) * p["cache_read"]
        + tokens.get("cacheWrite", 0) * p["cache_write"]
    ) / 1_000_000


def empty_buckets():
    return {
        "claude": {"tokens": 0, "cost": 0.0, "msgs": 0, "prompts": 0},
        "codex":  {"tokens": 0, "cost": 0.0, "msgs": 0, "prompts": 0},
        "other":  {"tokens": 0, "cost": 0.0, "msgs": 0, "prompts": 0},
    }


def build(tokscale, prompts_by_date):
    contribs = tokscale.get("contributions", [])
    summary = tokscale.get("summary", {})

    # ─ Per-day rows, keyed by date for sparse lookup
    by_date: dict[str, dict] = {}
    for c in sorted(contribs, key=lambda x: x["date"]):
        d = c["date"]
        buckets = empty_buckets()
        for cl in c.get("clients", []):
            bucket = classify_client(cl.get("client", ""))
            tok = cl.get("tokens", {})
            tok_total = (
                tok.get("input", 0)
                + tok.get("output", 0)
                + tok.get("cacheRead", 0)
                + tok.get("cacheWrite", 0)
                + tok.get("reasoning", 0)
            )
            buckets[bucket]["tokens"] += tok_total
            # Apply our pricing override for Anthropic Claude models
            override = override_cost(bucket, cl.get("modelId", ""), tok)
            buckets[bucket]["cost"] += (
                override if override is not None else cl.get("cost", 0)
            )
            buckets[bucket]["msgs"] += cl.get("messages", 0)
        # Merge in real user prompts (separately parsed from JSONL)
        p = prompts_by_date.get(d, {})
        buckets["claude"]["prompts"] = p.get("claude", 0)
        buckets["codex"]["prompts"] = p.get("codex", 0)
        for k in buckets:
            buckets[k]["cost"] = round(buckets[k]["cost"], 2)
        by_date[d] = buckets

    # ─ Fill gaps: emit one row per day from earliest to latest so the
    # chart x-axis is linear in time (no visually stacked month labels)
    if not by_date:
        return None
    all_iso = sorted(by_date.keys())
    start_d = date.fromisoformat(all_iso[0])
    end_d = date.fromisoformat(all_iso[-1])
    daily_rows = []
    cur = start_d
    while cur <= end_d:
        iso = cur.isoformat()
        daily_rows.append({"date": iso, **(by_date.get(iso) or empty_buckets())})
        cur += timedelta(days=1)

    # ─ Window summaries
    def window_sum(rows, start: date, end: date):
        out = {
            "claude": {"tokens": 0, "cost": 0.0, "msgs": 0, "prompts": 0},
            "codex":  {"tokens": 0, "cost": 0.0, "msgs": 0, "prompts": 0},
            "other":  {"tokens": 0, "cost": 0.0, "msgs": 0, "prompts": 0},
            "total":  {"tokens": 0, "cost": 0.0, "msgs": 0, "prompts": 0},
        }
        for r in rows:
            d = date.fromisoformat(r["date"])
            if not (start <= d <= end):
                continue
            for k in ("claude", "codex", "other"):
                out[k]["tokens"] += r[k]["tokens"]
                out[k]["cost"] += r[k]["cost"]
                out[k]["msgs"] += r[k]["msgs"]
                out[k]["prompts"] += r[k].get("prompts", 0)
        for k in ("claude", "codex", "other"):
            out[k]["cost"] = round(out[k]["cost"], 2)
            out["total"]["tokens"] += out[k]["tokens"]
            out["total"]["cost"] += out[k]["cost"]
            out["total"]["msgs"] += out[k]["msgs"]
            out["total"]["prompts"] += out[k]["prompts"]
        out["total"]["cost"] = round(out["total"]["cost"], 2)
        return out

    all_dates = [date.fromisoformat(r["date"]) for r in daily_rows]
    if not all_dates:
        raise RuntimeError("no data in tokscale graph")
    today = date.today()
    week_start = today - timedelta(days=6)
    month_start = today - timedelta(days=29)
    start, end = all_dates[0], max(all_dates[-1], today)

    summary_out = {
        "week":     window_sum(daily_rows, week_start, today),
        "month":    window_sum(daily_rows, month_start, today),
        "lifetime": window_sum(daily_rows, start, end),
    }

    # ─ By model across lifetime (aggregate from per-day client breakdown)
    model_agg = defaultdict(lambda: {"client": "", "model": "", "tokens": 0, "cost": 0.0})
    for c in contribs:
        for cl in c.get("clients", []):
            client = cl.get("client", "")
            model = cl.get("modelId", "")
            key = f"{client}:{model}"
            tok = cl.get("tokens", {})
            tok_total = sum(tok.values())
            classified = classify_client(client)
            model_agg[key]["client"] = classified
            model_agg[key]["model"] = model
            model_agg[key]["tokens"] += tok_total
            override = override_cost(classified, model, tok)
            model_agg[key]["cost"] += (
                override if override is not None else cl.get("cost", 0)
            )

    total_cost = sum(m["cost"] for m in model_agg.values())
    by_model = [
        {
            "client": v["client"],
            "model": v["model"],
            "tokens": v["tokens"],
            "cost": round(v["cost"], 2),
            "share": round(v["cost"] / total_cost, 4) if total_cost else 0,
        }
        for v in model_agg.values()
    ]
    by_model.sort(key=lambda r: -r["cost"])

    return {
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "dateRange": {"start": start.isoformat(), "end": end.isoformat()},
        "daily": daily_rows,
        "summary": summary_out,
        "byModel": by_model,
        "leaderboards": {
            "tokscale": {
                "url": "https://tokscale.ai/u/MaxGhenis",
                "rank": {"week": 2, "month": 2, "allTime": 12},
                "users": 252,
            },
            "straude": {
                "url": "https://straude.com/u/maxghenis",
                "embedSvg": "https://straude.com/api/embed/maxghenis/svg",
                "rank": {"week": 1},
                "users": 726,
            },
        },
    }


def main():
    tokscale = run_tokscale_graph()
    prompts_by_date = parse_real_user_prompts()
    output = build(tokscale, prompts_by_date)

    out_path = Path.home() / "usage-tracker" / "usage.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)

    s = output["summary"]
    print(f"\nWrote {out_path}")
    print(f"  Date range: {output['dateRange']['start']} → {output['dateRange']['end']}")
    for w in ("week", "month", "lifetime"):
        t = s[w]["total"]
        claude = s[w]["claude"]
        codex = s[w]["codex"]
        print(f"  {w:10}: ${t['cost']:>10,.0f} / {t['tokens']/1e9:>6.1f}B  "
              f"(claude ${claude['cost']:,.0f}, codex ${codex['cost']:,.0f})")


if __name__ == "__main__":
    main()

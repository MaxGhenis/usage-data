#!/usr/bin/env python3
"""Build usage.json for maxghenis.com/usage page.

Data source: Tokscale's `graph` JSON output, which already provides accurate
LiteLLM pricing for Claude Code + Codex + Gemini + OpenClaw.

We reshape Tokscale's data into our own format that's convenient for the
React chart + the leaderboard cards, and add leaderboard standings.

Output schema:
{
  "generatedAt": ISO8601,
  "dateRange": {start, end},
  "daily": [
    {"date", "claude": {tokens, cost, msgs}, "codex": {...}, "other": {...}}
  ],
  "summary": {"week", "month", "lifetime": {claude, codex, other, total}},
  "byModel": [{client, model, tokens, cost, share}],
  "leaderboards": {
    "tokscale": {url, rank: {week, month, allTime}, users},
    "straude":  {url, embedSvg, rank: {week}, users},
    "viberank": {url, ...},
    "ccgather": {url, ...}
  }
}
"""

import json
import subprocess
from datetime import datetime, date, timedelta, timezone
from pathlib import Path
from collections import defaultdict

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


def build(tokscale):
    contribs = tokscale.get("contributions", [])
    summary = tokscale.get("summary", {})

    # ─ Per-day rows
    daily_rows = []
    for c in sorted(contribs, key=lambda x: x["date"]):
        d = c["date"]
        totals = c.get("totals", {})
        buckets = {"claude": {"tokens": 0, "cost": 0.0, "msgs": 0},
                   "codex":  {"tokens": 0, "cost": 0.0, "msgs": 0},
                   "other":  {"tokens": 0, "cost": 0.0, "msgs": 0}}
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
            buckets[bucket]["cost"] += cl.get("cost", 0)
            buckets[bucket]["msgs"] += cl.get("messages", 0)
        for k in buckets:
            buckets[k]["cost"] = round(buckets[k]["cost"], 2)
        daily_rows.append({"date": d, **buckets})

    # ─ Window summaries
    def window_sum(rows, start: date, end: date):
        out = {
            "claude": {"tokens": 0, "cost": 0.0, "msgs": 0},
            "codex":  {"tokens": 0, "cost": 0.0, "msgs": 0},
            "other":  {"tokens": 0, "cost": 0.0, "msgs": 0},
            "total":  {"tokens": 0, "cost": 0.0, "msgs": 0},
        }
        for r in rows:
            d = date.fromisoformat(r["date"])
            if not (start <= d <= end):
                continue
            for k in ("claude", "codex", "other"):
                out[k]["tokens"] += r[k]["tokens"]
                out[k]["cost"] += r[k]["cost"]
                out[k]["msgs"] += r[k]["msgs"]
        for k in ("claude", "codex", "other"):
            out[k]["cost"] = round(out[k]["cost"], 2)
            out["total"]["tokens"] += out[k]["tokens"]
            out["total"]["cost"] += out[k]["cost"]
            out["total"]["msgs"] += out[k]["msgs"]
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
            model_agg[key]["client"] = classify_client(client)
            model_agg[key]["model"] = model
            model_agg[key]["tokens"] += tok_total
            model_agg[key]["cost"] += cl.get("cost", 0)

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
    output = build(tokscale)

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

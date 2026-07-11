"""Token-usage extraction from raw Claude Code + Codex session JSONL.

This is the measurement core behind usage.json. Design goals, in order:
1. Bill-faithful token counts (what the API would have charged).
2. Usage dated to when it happened (per-event timestamps, UTC).
3. Durable history (scan cache retains results after source files rotate).

Codex accounting
----------------
Codex rollout files carry cumulative ``total_token_usage`` counters in
``token_count`` events. Two traps:

* Resume/fork snapshots: resuming a session writes a NEW rollout file
  containing a full copy of the prior history, re-stamped to the write
  time (thousands of token_count events inside one wall-clock second) and
  given a fresh session id. Counting file-final totals therefore counts
  the same history once per snapshot. We detect the leading same-second
  burst (like ccusage does) and skip it, keeping it only as the delta
  baseline, so each file contributes just its live continuation.
* Counter resets: deltas are clamped at zero per component.

Claude accounting
-----------------
Assistant events carry per-request ``usage``. Resumed sessions copy
history into new files, so messages are deduplicated globally by
(message.id, requestId). cache_creation is captured, split 5m/1h when the
breakdown is present (they bill differently).

Both scanners cache per-file results keyed by (size, mtime) in SQLite.
Files that later disappear (Claude Code rotates transcripts after ~30
days; codex sessions get archived) keep contributing from the cache.
"""

from __future__ import annotations

import glob
import json
import os
import sqlite3
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

HOME = Path.home()
TRACKER_DIR = Path(__file__).resolve().parent
CACHE_DB = TRACKER_DIR / "scan-cache.sqlite"
LOGPILE_DB = HOME / "logpile" / "logpile.db"
SEED_PATH = TRACKER_DIR / "claude_history_seed.json"

CODEX_ROOTS = [
    HOME / ".codex" / "sessions",
    HOME / ".codex" / "archived_sessions",
    HOME / ".codex-2" / "sessions",
    HOME / ".codex-3" / "sessions",
] + [Path(p) for p in glob.glob(str(HOME / ".openclaw/agents/*/agent/codex-home/sessions"))]

CLAUDE_ROOT = HOME / ".claude" / "projects"

# Token vectors are [fresh_input, cache_write_5m, cache_write_1h, cache_read, output]
N_FIELDS = 5

HUMAN_ORIGINS = {"human_direct", "human_delegated"}


# ─────────────────────────────────────────────────────────
# Pricing — public API list prices, $ per 1M tokens.
# Cross-checked against LiteLLM model_prices_and_context_window.json
# (2026-07-11) and provider pricing pages. cache_read is the cache-hit
# rate; cw5m/cw1h are Anthropic prompt-cache write rates (1.25x / 2x
# input). OpenAI does not bill cache writes.
# ─────────────────────────────────────────────────────────
PRICING = {
    # Anthropic
    "claude-fable-5":  {"input": 10.0, "cached": 1.00, "output": 50.0, "cw5m": 12.50, "cw1h": 20.0, "source": "Anthropic list"},
    "claude-opus-4-8": {"input": 5.0,  "cached": 0.50, "output": 25.0, "cw5m": 6.25,  "cw1h": 10.0, "source": "Anthropic list"},
    "claude-opus-4-7": {"input": 5.0,  "cached": 0.50, "output": 25.0, "cw5m": 6.25,  "cw1h": 10.0, "source": "Anthropic list"},
    "claude-opus-4-6": {"input": 5.0,  "cached": 0.50, "output": 25.0, "cw5m": 6.25,  "cw1h": 10.0, "source": "Anthropic list"},
    "claude-opus-4-5": {"input": 5.0,  "cached": 0.50, "output": 25.0, "cw5m": 6.25,  "cw1h": 10.0, "source": "Anthropic list"},
    "claude-opus":     {"input": 15.0, "cached": 1.50, "output": 75.0, "cw5m": 18.75, "cw1h": 30.0, "source": "Anthropic list"},  # Opus <= 4.1
    "claude-sonnet-5": {"input": 2.0,  "cached": 0.20, "output": 10.0, "cw5m": 2.50,  "cw1h": 4.0,  "source": "Anthropic list"},
    "claude-sonnet":   {"input": 3.0,  "cached": 0.30, "output": 15.0, "cw5m": 3.75,  "cw1h": 6.0,  "source": "Anthropic list"},
    "claude-haiku":    {"input": 1.0,  "cached": 0.10, "output": 5.0,  "cw5m": 1.25,  "cw1h": 2.0,  "source": "Anthropic list"},
    # OpenAI
    "gpt-5.6-sol":     {"input": 5.0,  "cached": 0.50, "output": 30.0, "source": "OpenAI list"},
    "gpt-5.6-terra":   {"input": 2.5,  "cached": 0.25, "output": 15.0, "source": "OpenAI list"},
    "gpt-5.6-luna":    {"input": 1.0,  "cached": 0.10, "output": 6.0,  "source": "OpenAI list"},
    "gpt-5.6":         {"input": 5.0,  "cached": 0.50, "output": 30.0, "source": "OpenAI list"},
    "gpt-5.5":         {"input": 5.0,  "cached": 0.50, "output": 30.0, "source": "OpenAI list"},
    "gpt-5.4-mini":    {"input": 0.75, "cached": 0.075, "output": 4.5, "source": "OpenAI list"},
    "gpt-5.4":         {"input": 2.5,  "cached": 0.25, "output": 15.0, "source": "OpenAI list"},
    "gpt-5.3":         {"input": 1.75, "cached": 0.175, "output": 14.0, "source": "OpenAI list (codex rate)"},
    "gpt-5.2":         {"input": 1.75, "cached": 0.175, "output": 14.0, "source": "OpenAI list (codex rate)"},
    "gpt-5.1":         {"input": 1.25, "cached": 0.125, "output": 10.0, "source": "OpenAI list"},
    "gpt-5":           {"input": 1.25, "cached": 0.125, "output": 10.0, "source": "OpenAI list"},
    "gpt-4.1":         {"input": 2.0,  "cached": 0.20, "output": 8.0,  "source": "OpenAI list"},
    "o3":              {"input": 2.0,  "cached": 0.50, "output": 8.0,  "source": "OpenAI list"},
    "o4-mini":         {"input": 1.1,  "cached": 0.275, "output": 4.4, "source": "OpenAI list"},
    "codex-mini":      {"input": 1.5,  "cached": 0.375, "output": 6.0, "source": "OpenAI list"},
}

_UNPRICED = {"input": 0.0, "cached": 0.0, "output": 0.0, "source": "unpriced"}


def resolve_price(model: str) -> dict:
    """Longest-prefix match into PRICING; date-versioned ids collapse to tier."""
    m = (model or "").lower()
    best = None
    for tier in PRICING:
        if m.startswith(tier) and (best is None or len(tier) > len(best)):
            best = tier
    if best:
        return PRICING[best]
    return _UNPRICED


def cost_usd(model: str, v: list[int]) -> float:
    """Cost of a token vector [fresh, cw5m, cw1h, cache_read, output]."""
    r = resolve_price(model)
    return (
        v[0] * r["input"]
        + v[1] * r.get("cw5m", 0.0)
        + v[2] * r.get("cw1h", 0.0)
        + v[3] * r["cached"]
        + v[4] * r["output"]
    ) / 1_000_000


def _fallback_codex_model(first_day: str) -> str:
    """Model guess for rollout files with no turn_context (rare)."""
    if first_day >= "2026-07-01":
        return "gpt-5.6-sol"
    if first_day >= "2026-04-23":
        return "gpt-5.5"
    if first_day >= "2026-03-01":
        return "gpt-5.4"
    return "gpt-5"


# ─────────────────────────────────────────────────────────
# Codex scanner
# ─────────────────────────────────────────────────────────
def scan_codex_file(path: str):
    """One rollout file -> {'daily': {day: {model: [5]}}, 'meta': {...}}.

    Live usage only: the leading replay burst of a resume snapshot (all
    token_count events re-stamped into one second) is folded into the
    delta baseline instead of being counted again.
    """
    model = None
    first_model = None
    source = None
    originator = None
    events = []  # (ts, model_at_event, input, cached, output, total)
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                if '"token_count"' in line:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "event_msg":
                        continue
                    p = obj.get("payload", {})
                    if p.get("type") != "token_count":
                        continue
                    tot = (p.get("info") or {}).get("total_token_usage") or {}
                    if not tot:
                        continue
                    inp = int(tot.get("input_tokens", 0) or 0)
                    cached = int(tot.get("cached_input_tokens", 0) or 0)
                    out = int(tot.get("output_tokens", 0) or 0)
                    ttl = int(tot.get("total_tokens", 0) or 0) or (inp + out)
                    events.append((obj.get("timestamp") or "", model, inp, cached, out, ttl))
                elif '"turn_context"' in line:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "turn_context":
                        m = obj.get("payload", {}).get("model")
                        if m:
                            model = m
                            if first_model is None:
                                first_model = m
                elif '"session_meta"' in line and source is None:
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") != "session_meta":
                        continue
                    p = obj.get("payload", {})
                    src = p.get("source")
                    source = src if isinstance(src, str) else "subagent" if src else None
                    originator = p.get("originator")
    except OSError:
        return None
    if not events:
        return None

    default_model = first_model or _fallback_codex_model((events[0][0] or "")[:10])

    # Leading replay burst: first two token_count events share a wall-clock
    # second -> skip the whole same-second run, keep it as the baseline.
    burst_end = 0
    if len(events) >= 2 and events[0][0][:19] == events[1][0][:19]:
        sec = events[0][0][:19]
        while burst_end < len(events) and events[burst_end][0][:19] == sec:
            burst_end += 1
    prev = [0, 0, 0]
    for e in events[:burst_end]:
        prev = [max(prev[0], e[2]), max(prev[1], e[3]), max(prev[2], e[4])]

    daily: dict = defaultdict(lambda: defaultdict(lambda: [0] * N_FIELDS))
    for ts, mdl, inp, cached, out, _ttl in events[burst_end:]:
        m = mdl or default_model
        di = max(0, inp - prev[0])
        dc = max(0, cached - prev[1])
        do = max(0, out - prev[2])
        if di or dc or do:
            b = daily[ts[:10]][m]
            b[0] += max(0, di - dc)  # fresh (input includes cached)
            b[3] += dc
            b[4] += do
        prev = [max(prev[0], inp), max(prev[1], cached), max(prev[2], out)]

    return {
        "daily": {d: dict(ms) for d, ms in daily.items()},
        "meta": {"source": source, "originator": originator},
    }


# ─────────────────────────────────────────────────────────
# Claude scanner
# ─────────────────────────────────────────────────────────
def scan_claude_file(path: str):
    """One transcript -> rows [(dedup_key, day, model, [5])].

    Dedup happens at aggregation time (across files), not here.
    """
    rows = []
    try:
        with open(path, "r", errors="replace") as f:
            for i, line in enumerate(f):
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") != "assistant":
                    continue
                msg = obj.get("message", {})
                usage = msg.get("usage")
                if not usage:
                    continue
                model = msg.get("model") or "(unknown)"
                if model == "<synthetic>":
                    continue
                mid = msg.get("id")
                rid = obj.get("requestId")
                key = f"{mid}:{rid}" if (mid and rid) else (obj.get("uuid") or f"{os.path.basename(path)}:{i}")
                cw_total = int(usage.get("cache_creation_input_tokens", 0) or 0)
                cc = usage.get("cache_creation") or {}
                cw5 = int(cc.get("ephemeral_5m_input_tokens", 0) or 0)
                cw1 = int(cc.get("ephemeral_1h_input_tokens", 0) or 0)
                if cw5 + cw1 == 0:
                    cw5 = cw_total  # no breakdown -> assume 5m
                rows.append((
                    key,
                    (obj.get("timestamp") or "")[:10],
                    model,
                    [
                        int(usage.get("input_tokens", 0) or 0),
                        cw5,
                        cw1,
                        int(usage.get("cache_read_input_tokens", 0) or 0),
                        int(usage.get("output_tokens", 0) or 0),
                    ],
                ))
    except OSError:
        return None
    return {"rows": rows}


# ─────────────────────────────────────────────────────────
# Scan cache — per-file results survive source-file rotation.
# ─────────────────────────────────────────────────────────
def _cache_conn():
    con = sqlite3.connect(str(CACHE_DB))
    con.execute(
        """CREATE TABLE IF NOT EXISTS files (
               path TEXT PRIMARY KEY,
               stem TEXT NOT NULL,
               client TEXT NOT NULL,
               size INTEGER NOT NULL,
               mtime REAL NOT NULL,
               present INTEGER NOT NULL DEFAULT 1,
               result TEXT NOT NULL
           )"""
    )
    con.execute("CREATE INDEX IF NOT EXISTS idx_files_stem ON files(stem)")
    return con


def _list_files(roots):
    out = []
    for root in roots:
        if root.is_dir():
            for dirpath, _dn, filenames in os.walk(root):
                for fn in filenames:
                    if fn.endswith(".jsonl"):
                        out.append(os.path.join(dirpath, fn))
    return out


def _scan_with_cache(con, client, files, scan_fn, workers=8):
    """Return {path: result} for all files, using and refreshing the cache."""
    cached = {
        p: (sz, mt, res)
        for p, sz, mt, res in con.execute(
            "SELECT path, size, mtime, result FROM files WHERE client = ?", (client,)
        )
    }
    todo = []
    results = {}
    on_disk = set(files)
    for p in files:
        try:
            st = os.stat(p)
        except OSError:
            continue
        hit = cached.get(p)
        if hit and hit[0] == st.st_size and abs(hit[1] - st.st_mtime) < 1e-6:
            results[p] = json.loads(hit[2])
        else:
            todo.append((p, st.st_size, st.st_mtime))

    if todo:
        with ProcessPoolExecutor(max_workers=workers) as ex:
            for (p, size, mtime), res in zip(
                todo, ex.map(scan_fn, [t[0] for t in todo], chunksize=16)
            ):
                if res is None:
                    res = {}
                results[p] = res
                con.execute(
                    "INSERT OR REPLACE INTO files (path, stem, client, size, mtime, present, result) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (p, Path(p).stem, client, size, mtime, json.dumps(res)),
                )
        con.commit()

    # Rotated/archived files: keep cached results, marked absent.
    for p, (sz, mt, res) in cached.items():
        if p not in on_disk and p not in results:
            results[p] = json.loads(res)
    con.executemany(
        "UPDATE files SET present = ? WHERE path = ?",
        [(1 if p in on_disk else 0, p) for p in cached],
    )
    con.commit()
    return results


def _dedupe_codex_paths(results):
    """One result per rollout stem: prefer live sessions/ > archived > cache-only."""
    def rank(p):
        if "/archived_sessions/" in p:
            r = 1
        else:
            r = 0 if os.path.exists(p) else 2
        return r

    by_stem = {}
    for p, res in results.items():
        if not res:
            continue
        s = Path(p).stem
        if s not in by_stem or rank(p) < rank(by_stem[s][0]):
            by_stem[s] = (p, res)
    return by_stem


# ─────────────────────────────────────────────────────────
# Origin classification (human vs automated), via the Logpile ledger.
# ─────────────────────────────────────────────────────────
def load_origin_map(cache_con=None):
    """{session_stem: 'human'|'automated'} from logpile.db.

    The ledger may be locked by a concurrent `logpile sync`; retry briefly,
    then fall back to the last successful copy persisted in the scan cache
    (which also preserves origins for sessions the ledger later drops).
    """
    import time

    rows = None
    if LOGPILE_DB.exists():
        for attempt in range(3):
            try:
                con = sqlite3.connect(
                    f"file:{LOGPILE_DB}?mode=ro", uri=True, timeout=30
                )
                rows = con.execute(
                    "SELECT session_id, session_origin FROM sessions"
                ).fetchall()
                con.close()
                break
            except sqlite3.Error:
                time.sleep(2 * (attempt + 1))

    if cache_con is not None:
        cache_con.execute(
            "CREATE TABLE IF NOT EXISTS origins (stem TEXT PRIMARY KEY, origin TEXT NOT NULL)"
        )
        if rows is not None:
            cache_con.executemany(
                "INSERT OR REPLACE INTO origins (stem, origin) VALUES (?, ?)",
                [(sid, o or "") for sid, o in rows],
            )
            cache_con.commit()
        else:
            rows = cache_con.execute("SELECT stem, origin FROM origins").fetchall()

    return {
        sid: ("human" if origin in HUMAN_ORIGINS else "automated")
        for sid, origin in (rows or [])
    }


def _codex_origin(stem, meta, origin_map):
    o = origin_map.get(stem)
    if o:
        return o
    meta = meta or {}
    if meta.get("source") in ("exec", "subagent"):
        return "automated"
    if meta.get("originator") in ("Codex Desktop", "codex_vscode", "vscode"):
        return "human"
    return "automated"


def _claude_origin(path, origin_map):
    stem = Path(path).stem
    o = origin_map.get(stem)
    if o:
        return o
    # Subagent transcripts inherit the parent session's origin.
    parts = Path(path).parts
    if "subagents" in parts:
        parent = parts[parts.index("subagents") - 1]
        o = origin_map.get(parent)
        if o:
            return o
        return "automated"
    return "human"


# ─────────────────────────────────────────────────────────
# Aggregation
# ─────────────────────────────────────────────────────────
def _new_day():
    return {
        "human": defaultdict(lambda: defaultdict(lambda: [0] * N_FIELDS)),
        "automated": defaultdict(lambda: defaultdict(lambda: [0] * N_FIELDS)),
    }


def extract_daily(workers: int = 8):
    """Full extraction -> {day: {origin: {client: {model: [5]}}}}."""
    con = _cache_conn()
    origin_map = load_origin_map(con)

    daily: dict = defaultdict(_new_day)

    codex_results = _scan_with_cache(
        con, "codex", _list_files(CODEX_ROOTS), scan_codex_file, workers
    )
    for stem, (path, res) in _dedupe_codex_paths(codex_results).items():
        origin = _codex_origin(stem, res.get("meta"), origin_map)
        for day, models in res.get("daily", {}).items():
            for model, v in models.items():
                b = daily[day][origin]["codex"][model]
                for i in range(N_FIELDS):
                    b[i] += v[i]

    claude_results = _scan_with_cache(
        con, "claudecode", _list_files([CLAUDE_ROOT]), scan_claude_file, workers
    )
    seen = set()
    for path in sorted(claude_results):
        res = claude_results[path]
        if not res:
            continue
        origin = _claude_origin(path, origin_map)
        for key, day, model, v in res.get("rows", []):
            if key in seen:
                continue
            seen.add(key)
            b = daily[day][origin]["claude"][model]
            for i in range(N_FIELDS):
                b[i] += v[i]
    con.close()

    _merge_seed(daily)

    return {
        day: {
            origin: {
                client: {model: v for model, v in models.items()}
                for client, models in clients.items()
            }
            for origin, clients in groups.items()
        }
        for day, groups in daily.items()
    }


def _merge_seed(daily):
    """Fold in pre-rotation Claude history where the ledger knows more.

    For seed days, compare per-(day, model) token totals: if the seed
    (Logpile session ledger) has more than what survives on disk, replace
    the scanned rows for that model with the seed rows.
    """
    if not SEED_PATH.exists():
        return
    try:
        seed = json.loads(SEED_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return
    for day, models in seed.get("daily", {}).items():
        for model, origins in models.items():
            seed_vecs = {}
            seed_total = 0
            for origin, v4 in origins.items():
                v = [int(v4[0]), int(v4[1]), 0, int(v4[2]), int(v4[3])]
                seed_vecs[origin] = v
                seed_total += sum(v)
            scanned_total = sum(
                sum(daily[day][o]["claude"].get(model, [0] * N_FIELDS))
                for o in ("human", "automated")
            ) if day in daily else 0
            if seed_total > scanned_total:
                for o in ("human", "automated"):
                    if model in daily[day][o]["claude"]:
                        del daily[day][o]["claude"][model]
                for origin, v in seed_vecs.items():
                    daily[day][origin]["claude"][model] = v

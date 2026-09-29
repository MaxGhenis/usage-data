"""Property-based tests for Claude accounting and model pricing.

Invariants (they hold for every input):

1. Each billed request (message.id, requestId) is counted exactly once,
   however many stream snapshots, verbatim copies or truncated copies of
   it exist and whichever files they sit in.
2. A day's total equals the sum over that day's unique requests of the
   billed (most complete) record; so does its cost.
3. The result does not depend on file names, line order or which origin
   a file has; the kept record is always one of the request's own
   records, and it is the dominating one whenever one exists.
4. A model id is never priced at another generation's rates: a tier key
   followed by a version component resolves to that version's own row or
   to "unpriced".

Run: uv run --no-project --with pytest --with hypothesis python -m pytest -q
"""

import json
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import pytest
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import build_usage
import extract
from extract import PRICING, N_FIELDS, cost_usd, resolve_price, scan_claude_file

MODELS = [
    "claude-opus-5-5",
    "claude-opus-5",
    "claude-fable-5-1",
    "claude-fable-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-haiku-4-5-20251001",
]
DAYS = ["2026-09-23", "2026-09-24", "2026-09-25"]
N_FILES = 4


def _record(day, model, mid, rid, v):
    """A Claude Code assistant record carrying usage vector v = [5]."""
    fresh, cw5m, cw1h, cread, out = v
    return json.dumps({
        "type": "assistant",
        "timestamp": f"{day}T12:00:00.000Z",
        "requestId": rid,
        "message": {
            "id": mid,
            "model": model,
            "usage": {
                "input_tokens": fresh,
                "cache_creation_input_tokens": cw5m + cw1h,
                "cache_creation": {
                    "ephemeral_5m_input_tokens": cw5m,
                    "ephemeral_1h_input_tokens": cw1h,
                },
                "cache_read_input_tokens": cread,
                "output_tokens": out,
            },
        },
    })


_count = st.integers(min_value=0, max_value=2_000_000)
ORIGINS = ("human", "automated")


def _dominates(a, b):
    return all(x >= y for x, y in zip(a, b))


@st.composite
def corpora(draw, realistic=True):
    """Records for up to 10 requests, spread over N_FILES transcripts.

    realistic=True: each request has a billed final vector. Its home file
    holds 0-2 message_start placeholders (output_tokens <= 1, everything
    else equal) and the final; other files hold verbatim or truncated
    (componentwise <=) copies — the shapes resumed sessions and subagent
    streams produce.

    realistic=False: additionally, copies with arbitrary vectors, days and
    models, so no record need dominate the others.

    Each file gets an origin, and every file two independent line orders
    and two independent name orders, for the order-independence checks.
    """
    n = draw(st.integers(min_value=1, max_value=10))
    requests, records = [], []
    for i in range(n):
        mid, rid = f"msg_{i}", f"req_{i}"
        day = draw(st.sampled_from(DAYS))
        model = draw(st.sampled_from(MODELS))
        final = [draw(_count) for _ in range(N_FIELDS)]
        requests.append({"key": f"{mid}:{rid}", "day": day, "model": model,
                         "final": final})
        home = draw(st.integers(0, N_FILES - 1))
        for _ in range(draw(st.integers(0, 2))):
            placeholder = final[:4] + [min(1, final[4])]
            records.append((home, day, model, mid, rid, placeholder))
        records.append((home, day, model, mid, rid, final))
        for _ in range(draw(st.integers(0, 3))):
            where = draw(st.integers(0, N_FILES - 1))
            kind = draw(st.sampled_from(
                ["verbatim", "truncated"] + ([] if realistic else ["arbitrary"])
            ))
            if kind == "verbatim":
                records.append((where, day, model, mid, rid, list(final)))
            elif kind == "truncated":
                copy = [draw(st.integers(0, x)) for x in final]
                records.append((where, day, model, mid, rid, copy))
            else:
                records.append((
                    where, draw(st.sampled_from(DAYS)),
                    draw(st.sampled_from(MODELS)), mid, rid,
                    [draw(_count) for _ in range(N_FIELDS)],
                ))
    files = [
        [_record(d, m, mid, rid, v) for f, d, m, mid, rid, v in records if f == idx]
        for idx in range(N_FILES)
    ]
    line_orders = [
        [draw(st.permutations(lines)) for lines in files] for _ in range(2)
    ]
    names = [draw(st.permutations(range(N_FILES))) for _ in range(2)]
    origins = [draw(st.sampled_from(ORIGINS)) for _ in range(N_FILES)]
    return {"requests": requests, "records": records, "line_orders": line_orders,
            "names": names, "origins": origins}


def _run_pipeline(files, name_order, origins):
    """Scan, dedup and aggregate exactly as extract_daily does for Claude."""
    with tempfile.TemporaryDirectory() as tmp:
        results, origin_of = {}, {}
        for idx, lines in enumerate(files):
            path = Path(tmp) / f"{name_order[idx]:02d}-session.jsonl"
            path.write_text("\n".join(lines) + "\n" if lines else "")
            results[str(path)] = scan_claude_file(str(path))
            origin_of[str(path)] = origins[idx]
        picked = extract.dedupe_claude_rows(results, origin_of.__getitem__)
    daily = defaultdict(extract._new_day)
    extract.aggregate_claude(picked, daily)
    return picked, daily


@settings(max_examples=300, deadline=None)
@given(corpora(realistic=True))
def test_each_request_counted_once_at_its_billed_vector(c):
    picked, daily = _run_pipeline(c["line_orders"][0], c["names"][0], c["origins"])
    requests = c["requests"]

    # No request is counted twice, and none is lost.
    assert sorted(picked) == sorted(r["key"] for r in requests)
    for r in requests:
        day, _origin, model, v = picked[r["key"]]
        assert (day, model, v) == (r["day"], r["model"], r["final"])

    # Per-day, per-model totals and costs = sums over unique requests
    # (summed over origins: a request lands in exactly one origin).
    expected = defaultdict(lambda: [0] * N_FIELDS)
    expected_cost = defaultdict(float)
    for r in requests:
        cell = expected[(r["day"], r["model"])]
        for i in range(N_FIELDS):
            cell[i] += r["final"][i]
        expected_cost[r["day"]] += cost_usd(r["model"], r["final"])
    got = defaultdict(lambda: [0] * N_FIELDS)
    got_cost = defaultdict(float)
    for day, groups in daily.items():
        for origin in ORIGINS:
            for model, v in groups[origin]["claude"].items():
                for i in range(N_FIELDS):
                    got[(day, model)][i] += v[i]
                got_cost[day] += cost_usd(model, v)
    assert dict(got) == dict(expected)
    for day in DAYS:
        assert got_cost[day] == pytest.approx(expected_cost[day], rel=1e-12, abs=1e-9)


@settings(max_examples=300, deadline=None)
@given(corpora(realistic=False))
def test_dedup_invariants_hold_for_arbitrary_records(c):
    picked, _ = _run_pipeline(c["line_orders"][0], c["names"][0], c["origins"])
    by_key = defaultdict(list)
    for f, d, m, mid, rid, v in c["records"]:
        by_key[f"{mid}:{rid}"].append((d, c["origins"][f], m, v))

    # Exactly one pick per request, and it is one of that request's records.
    assert sorted(picked) == sorted(by_key)
    for key, recs in by_key.items():
        assert picked[key] in recs
        # When some record dominates all the others, it is the one kept.
        top = [r for r in recs if all(_dominates(r[3], o[3]) for o in recs)]
        if top:
            assert picked[key][3] == top[0][3]


@settings(max_examples=200, deadline=None)
@given(corpora(realistic=False))
def test_dedup_is_independent_of_file_names_and_line_order(c):
    picked_a, daily_a = _run_pipeline(c["line_orders"][0], c["names"][0], c["origins"])
    picked_b, daily_b = _run_pipeline(c["line_orders"][1], c["names"][1], c["origins"])
    assert picked_a == picked_b
    assert daily_a == daily_b


def test_truncated_copy_in_later_file_does_not_replace_billed_record(tmp_path):
    # Regression: 2026-07-25 resume copies carried [0, 0, 324, 0, 0] for
    # requests billed as [2, 0, 324, 943678, 1327]; the copy's file sorted
    # later, so the old last-wins rule kept the truncated vector.
    billed = _record("2026-07-25", "claude-opus-5", "msg_x", "req_x",
                     [2, 324, 0, 943678, 1327])
    truncated = _record("2026-07-25", "claude-opus-5", "msg_x", "req_x",
                        [0, 324, 0, 0, 0])
    a = tmp_path / "20b9f954.jsonl"
    b = tmp_path / "59df4b28.jsonl"
    a.write_text(billed + "\n")
    b.write_text(truncated + "\n")
    results = {str(p): scan_claude_file(str(p)) for p in (a, b)}
    (_, _, _, v), = extract.dedupe_claude_rows(results, lambda p: "human").values()
    assert v == [2, 324, 0, 943678, 1327]


def test_fast_mode_scanned_as_its_own_tier(tmp_path):
    line = json.loads(_record("2026-09-25", "claude-opus-5-5", "msg_f", "req_f",
                              [10, 0, 0, 1000, 100]))
    line["message"]["usage"]["speed"] = "fast"
    f = tmp_path / "fast.jsonl"
    f.write_text(json.dumps(line) + "\n")
    (_, _, model, v), = scan_claude_file(str(f))["rows"]
    assert model == "claude-opus-5-5-fast"
    # 2x Opus 5.5 input/output, cache read 0.05x of the fast input rate.
    assert cost_usd(model, v) == pytest.approx((10 * 8 + 1000 * 0.40 + 100 * 40) / 1e6)


# ─────────────────────────────────────────────────────────
# Pricing
# ─────────────────────────────────────────────────────────

# platform.claude.com/docs/en/about-claude/pricing, model pricing table,
# read 2026-09-29: (input, 5m write, 1h write, cache hit, output) $/MTok.
ANTHROPIC_LIST = {
    "claude-fable-5-1": (10.0, 12.50, 20.0, 0.25, 50.0),
    "claude-fable-5": (10.0, 12.50, 20.0, 1.00, 50.0),
    "claude-opus-5-5": (4.0, 5.00, 8.0, 0.20, 20.0),
    "claude-opus-5": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-8": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-7": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-6": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-5-20251101": (5.0, 6.25, 10.0, 0.50, 25.0),
    "claude-opus-4-1-20250805": (15.0, 18.75, 30.0, 1.50, 75.0),
    "claude-opus-4-20250514": (15.0, 18.75, 30.0, 1.50, 75.0),
    "claude-sonnet-5-5": (2.0, 2.50, 4.0, 0.20, 10.0),
    "claude-sonnet-5": (2.0, 2.50, 4.0, 0.20, 10.0),
    "claude-sonnet-4-6": (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4-5-20250929": (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-sonnet-4-20250514": (3.0, 3.75, 6.0, 0.30, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 1.25, 2.0, 0.10, 5.0),
}


@pytest.mark.parametrize("model,rates", sorted(ANTHROPIC_LIST.items()))
def test_anthropic_rates_match_list(model, rates):
    r = resolve_price(model)
    assert (r["input"], r["cw5m"], r["cw1h"], r["cached"], r["output"]) == rates


def test_september_2026_models_not_priced_as_legacy_opus():
    # Regression: prefix matching sent both to the Opus <= 4.1 row
    # ($15 / $1.50 / $75), overstating Sep 2026 Claude by ~$252k.
    assert resolve_price("claude-opus-5-5")["input"] == 4.0
    assert resolve_price("claude-opus-5")["input"] == 5.0
    # ... and Fable 5.1 cache reads at Fable 5's $1.00 (~$17k).
    assert resolve_price("claude-fable-5-1")["cached"] == 0.25


# Codex ids seen in the data through 2026-09-28 and the rates they carried
# before the resolver change; the change must not move any of them.
CODEX_RATES_BEFORE = {
    "gpt-5": (1.25, 0.125, 10.0),
    "gpt-5.1-codex-max": (1.25, 0.125, 10.0),
    "gpt-5.2": (1.75, 0.175, 14.0),
    "gpt-5.2-codex": (1.75, 0.175, 14.0),
    "gpt-5.3-codex": (1.75, 0.175, 14.0),
    "gpt-5.3-codex-spark": (1.75, 0.175, 14.0),
    "gpt-5.4": (2.5, 0.25, 15.0),
    "gpt-5.4-2026-03-05": (2.5, 0.25, 15.0),
    "gpt-5.4-mini": (0.75, 0.075, 4.5),
    "gpt-5.5": (5.0, 0.50, 30.0),
    "gpt-5.6-sol": (5.0, 0.50, 30.0),
    "gpt-5.6-terra": (2.5, 0.25, 15.0),
    "gpt-5.6-luna": (1.0, 0.10, 6.0),
}


@pytest.mark.parametrize("model,rates", sorted(CODEX_RATES_BEFORE.items()))
def test_codex_rates_unchanged_by_resolver(model, rates):
    r = resolve_price(model)
    assert (r["input"], r["cached"], r["output"]) == rates


@settings(max_examples=500, deadline=None)
@given(
    tier=st.sampled_from(sorted(PRICING)),
    suffix=st.from_regex(r"[.-][0-9]{1,2}([.-][0-9]{1,2})?", fullmatch=True),
)
def test_new_generation_never_borrows_an_older_tier(tier, suffix):
    """tier + version component resolves to that exact model's own row
    (a longer key the id starts with) or to "unpriced" — never to tier's
    row or any shorter one."""
    model = tier + suffix
    r = resolve_price(model)
    if r["source"] == "unpriced":
        return
    owners = [k for k, v in PRICING.items() if v is r]
    assert any(model.startswith(k) and len(k) > len(tier) for k in owners)


@settings(max_examples=200, deadline=None)
@given(
    tier=st.sampled_from(sorted(PRICING)),
    stamp=st.from_regex(r"-20[0-9]{6}|-20[0-9]{2}-[01][0-9]-[0-3][0-9]",
                        fullmatch=True),
)
def test_date_stamped_ids_keep_their_tier(tier, stamp):
    assert resolve_price(tier + stamp) is PRICING[tier]


@pytest.mark.parametrize("model", [
    "gpt-5.1-codex-mini",      # its own, cheaper tier; not gpt-5.1's
    "gpt-5-mini",
    "claude-opus-5-5[1m]",
    # Seen in the data through 2026-09-28; rows pending the Codex audit.
    "gpt-6-astra",
    "gpt-6-sol",
    "codex-auto-review",
])
def test_ids_without_their_own_row_are_unpriced(model):
    assert resolve_price(model)["source"] == "unpriced"


@pytest.mark.parametrize("model,rates", [
    ("claude-opus-5-5-fast", (8.0, 10.0, 16.0, 0.40, 40.0)),
    ("claude-opus-5-fast", (10.0, 12.50, 20.0, 1.00, 50.0)),
    ("claude-opus-4-8-fast", (10.0, 12.50, 20.0, 1.00, 50.0)),
    # "Claude Opus 4.6 (requests run at standard speed and are billed at
    # standard rates)" — pricing page, fast mode section.
    ("claude-opus-4-6-fast", (5.0, 6.25, 10.0, 0.50, 25.0)),
])
def test_fast_mode_rates(model, rates):
    r = resolve_price(model)
    assert (r["input"], r["cw5m"], r["cw1h"], r["cached"], r["output"]) == rates


def test_unknown_models_are_reported_not_hidden():
    daily = {
        "2026-09-25": {
            "human": {"codex": {"gpt-6-astra": [100, 0, 0, 900, 10]},
                      "claude": {"claude-opus-5-5": [1, 0, 0, 0, 1]}},
            "automated": {},
        }
    }
    out = build_usage.build(daily, {}, {}, {})
    assert out["pricing"]["unpriced"] == [
        {"client": "codex", "model": "gpt-6-astra", "tokens": 1010}
    ]

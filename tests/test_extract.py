"""Unit tests for the usage-extraction accounting rules."""

import json
import sys
from collections import defaultdict
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import extract
from extract import (
    N_FIELDS,
    cost_usd,
    resolve_price,
    scan_claude_file,
    scan_codex_file,
)


def _codex_line(ts, inp, cached, out, event_type="token_count"):
    return json.dumps({
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": event_type,
            "info": {
                "total_token_usage": {
                    "input_tokens": inp,
                    "cached_input_tokens": cached,
                    "output_tokens": out,
                    "total_tokens": inp + out,
                }
            },
        },
    })


def _turn_context(ts, model):
    return json.dumps({
        "timestamp": ts,
        "type": "turn_context",
        "payload": {"model": model},
    })


def _session_meta(ts, source="vscode"):
    return json.dumps({
        "timestamp": ts,
        "type": "session_meta",
        "payload": {"id": "abc", "timestamp": ts, "source": source,
                    "originator": "Codex Desktop"},
    })


def write_lines(path, lines):
    path.write_text("\n".join(lines) + "\n")


class TestCodexScanner:
    def test_fresh_session_counts_full_totals_per_event_date(self, tmp_path):
        f = tmp_path / "rollout-fresh.jsonl"
        write_lines(f, [
            _session_meta("2026-06-30T23:00:00.000Z"),
            _turn_context("2026-06-30T23:00:00.100Z", "gpt-5.5"),
            _codex_line("2026-06-30T23:01:00.000Z", 1000, 600, 50),
            _codex_line("2026-07-01T01:00:00.000Z", 3000, 2400, 120),
        ])
        res = scan_codex_file(str(f))
        june = res["daily"]["2026-06-30"]["gpt-5.5"]
        july = res["daily"]["2026-07-01"]["gpt-5.5"]
        # june: fresh=1000-600=400, cached=600, out=50
        assert june == [400, 0, 0, 600, 50]
        # july delta: input 2000 (cached 1800 -> fresh 200), out 70
        assert july == [200, 0, 0, 1800, 70]

    def test_replay_burst_is_skipped_but_baselines_deltas(self, tmp_path):
        f = tmp_path / "rollout-replay.jsonl"
        burst_ts = "2026-06-08T11:39:04"
        write_lines(f, [
            _session_meta("2026-04-30T21:05:22.000Z"),
            _turn_context(burst_ts + ".100Z", "gpt-5.5"),
            # replayed history: many events inside one second
            _codex_line(burst_ts + ".200Z", 10_000, 8_000, 500),
            _codex_line(burst_ts + ".300Z", 500_000, 400_000, 9_000),
            _codex_line(burst_ts + ".400Z", 1_000_000, 800_000, 20_000),
            # live continuation
            _codex_line("2026-06-08T12:00:00.000Z", 1_050_000, 840_000, 21_000),
        ])
        res = scan_codex_file(str(f))
        assert list(res["daily"].keys()) == ["2026-06-08"]
        live = res["daily"]["2026-06-08"]["gpt-5.5"]
        # only the post-burst delta: input 50k (cached 40k), out 1k
        assert live == [10_000, 0, 0, 40_000, 1_000]

    def test_counter_reset_clamps_to_zero(self, tmp_path):
        f = tmp_path / "rollout-reset.jsonl"
        write_lines(f, [
            _session_meta("2026-05-01T00:00:00.000Z"),
            _turn_context("2026-05-01T00:00:00.100Z", "gpt-5.5"),
            _codex_line("2026-05-01T01:00:00.000Z", 1000, 800, 100),
            _codex_line("2026-05-01T02:00:00.000Z", 200, 100, 10),   # reset
            _codex_line("2026-05-01T03:00:00.000Z", 900, 700, 60),
        ])
        res = scan_codex_file(str(f))
        v = res["daily"]["2026-05-01"]["gpt-5.5"]
        assert all(x >= 0 for x in v)
        # first event: fresh 200 cached 800 out 100; reset ignored; third event
        # clamps against running max (1000/800/100) -> no double count
        assert v == [200, 0, 0, 800, 100]

    def test_model_backfill_for_events_before_turn_context(self, tmp_path):
        f = tmp_path / "rollout-late-model.jsonl"
        write_lines(f, [
            _session_meta("2026-07-02T00:00:00.000Z"),
            _codex_line("2026-07-02T01:00:00.000Z", 1000, 0, 100),
            _turn_context("2026-07-02T01:00:01.000Z", "gpt-5.6-sol"),
            _codex_line("2026-07-02T02:00:00.000Z", 2000, 0, 200),
        ])
        res = scan_codex_file(str(f))
        assert set(res["daily"]["2026-07-02"].keys()) == {"gpt-5.6-sol"}

    def test_no_turn_context_uses_date_fallback(self, tmp_path):
        f = tmp_path / "rollout-nomodel.jsonl"
        write_lines(f, [
            _session_meta("2026-07-02T00:00:00.000Z"),
            _codex_line("2026-07-02T01:00:00.000Z", 1000, 0, 100),
        ])
        res = scan_codex_file(str(f))
        assert set(res["daily"]["2026-07-02"].keys()) == {"gpt-5.6-sol"}


def _claude_line(ts, model, mid, rid, inp, cw, cr, out, cc_breakdown=None):
    usage = {
        "input_tokens": inp,
        "cache_creation_input_tokens": cw,
        "cache_read_input_tokens": cr,
        "output_tokens": out,
    }
    if cc_breakdown:
        usage["cache_creation"] = cc_breakdown
    return json.dumps({
        "type": "assistant",
        "timestamp": ts,
        "requestId": rid,
        "message": {"id": mid, "model": model, "usage": usage},
    })


class TestClaudeScanner:
    def test_rows_and_5m_fallback(self, tmp_path):
        f = tmp_path / "session.jsonl"
        write_lines(f, [
            _claude_line("2026-07-02T10:00:00.000Z", "claude-fable-5",
                         "msg_1", "req_1", 100, 5000, 20000, 300),
        ])
        res = scan_claude_file(str(f))
        key, day, model, v = res["rows"][0]
        assert key == "msg_1:req_1"
        assert day == "2026-07-02"
        assert v == [100, 5000, 0, 20000, 300]  # no breakdown -> all 5m

    def test_1h_split(self, tmp_path):
        f = tmp_path / "session.jsonl"
        write_lines(f, [
            _claude_line("2026-07-02T10:00:00.000Z", "claude-opus-4-8",
                         "msg_2", "req_2", 10, 9000, 0, 50,
                         cc_breakdown={"ephemeral_5m_input_tokens": 1000,
                                       "ephemeral_1h_input_tokens": 8000}),
        ])
        (_, _, _, v), = scan_claude_file(str(f))["rows"]
        assert v == [10, 1000, 8000, 0, 50]

    def test_synthetic_model_skipped(self, tmp_path):
        f = tmp_path / "session.jsonl"
        write_lines(f, [
            _claude_line("2026-07-02T10:00:00.000Z", "<synthetic>",
                         "msg_3", "req_3", 10, 0, 0, 5),
        ])
        assert scan_claude_file(str(f))["rows"] == []


class TestPricing:
    def test_recent_opus_is_not_legacy_opus(self):
        assert resolve_price("claude-opus-4-8")["input"] == 5.0
        assert resolve_price("claude-opus-4-1-20250805")["input"] == 15.0

    def test_fable_and_sol_priced(self):
        assert resolve_price("claude-fable-5")["input"] == 10.0
        assert resolve_price("gpt-5.6-sol")["cached"] == 0.50

    def test_sonnet_5_vs_sonnet_4(self):
        assert resolve_price("claude-sonnet-5")["input"] == 2.0
        assert resolve_price("claude-sonnet-4-6")["input"] == 3.0

    def test_dateversioned_id_collapses_to_tier(self):
        assert resolve_price("gpt-5.4-2026-03-05")["input"] == 2.5

    def test_unknown_unpriced(self):
        assert resolve_price("mystery-model")["source"] == "unpriced"

    def test_cost_vector(self):
        # 1M of each bucket on fable-5: 10 + 12.5 + 20 + 1 + 50
        v = [1_000_000] * N_FIELDS
        assert cost_usd("claude-fable-5", v) == pytest.approx(93.5)


class TestSeedMerge:
    def test_seed_wins_when_larger(self, tmp_path, monkeypatch):
        seed = {
            "cutoff": "2026-05-09",
            "daily": {"2026-04-01": {"claude-opus-4-6": {
                "human": [100, 0, 900, 50]}}},
        }
        p = tmp_path / "seed.json"
        p.write_text(json.dumps(seed))
        monkeypatch.setattr(extract, "SEED_PATH", p)
        daily = defaultdict(extract._new_day)
        daily["2026-04-01"]["human"]["claude"]["claude-opus-4-6"] = [10, 0, 0, 90, 5]
        extract._merge_seed(daily)
        assert daily["2026-04-01"]["human"]["claude"]["claude-opus-4-6"] == \
            [100, 0, 0, 900, 50]

    def test_scan_wins_when_larger(self, tmp_path, monkeypatch):
        seed = {
            "cutoff": "2026-05-09",
            "daily": {"2026-05-05": {"claude-opus-4-7": {
                "human": [10, 0, 90, 5]}}},
        }
        p = tmp_path / "seed.json"
        p.write_text(json.dumps(seed))
        monkeypatch.setattr(extract, "SEED_PATH", p)
        daily = defaultdict(extract._new_day)
        daily["2026-05-05"]["human"]["claude"]["claude-opus-4-7"] = [100, 50, 0, 900, 50]
        extract._merge_seed(daily)
        assert daily["2026-05-05"]["human"]["claude"]["claude-opus-4-7"] == \
            [100, 50, 0, 900, 50]

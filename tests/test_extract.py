"""Unit tests for the usage-extraction accounting rules."""

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
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

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "codex"


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


def _rate_limit_line(ts, totals=None):
    """token_count heartbeat with no usage components."""
    info = {"rate_limits": {"primary_used_percent": 50}}
    if totals is not None:
        info["total_token_usage"] = totals
    return json.dumps({
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "token_count", "info": info},
    })


def _task_started(ts, started_at):
    return json.dumps({
        "timestamp": ts,
        "type": "event_msg",
        "payload": {"type": "task_started", "turn_id": "t1",
                    "started_at": started_at},
    })


def _turn_context(ts, model):
    return json.dumps({
        "timestamp": ts,
        "type": "turn_context",
        "payload": {"model": model},
    })


def _session_meta(ts, source="vscode", sid="abc", forked_from=None):
    payload = {"id": sid, "timestamp": ts, "source": source,
               "originator": "Codex Desktop"}
    if forked_from is not None:
        payload["forked_from_id"] = forked_from
    return json.dumps({"timestamp": ts, "type": "session_meta",
                       "payload": payload})


def _epoch(ts):
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00"))
               .astimezone(timezone.utc).timestamp())


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

    def test_fresh_same_second_burst_is_counted(self, tmp_path):
        # Same-second timing alone is NOT replay evidence: a fresh session
        # writing several counters inside one second keeps all its work
        # (the old ccusage-style heuristic wrongly discarded this).
        f = tmp_path / "rollout-burst.jsonl"
        sec = "2026-06-08T11:39:04"
        write_lines(f, [
            _session_meta(sec + ".000Z", sid="fresh-thread"),
            _turn_context(sec + ".050Z", "gpt-5.5"),
            _codex_line(sec + ".100Z", 1000, 600, 50),
            _codex_line(sec + ".500Z", 2000, 1400, 90),
            _codex_line(sec + ".900Z", 3000, 2400, 120),
        ])
        res = scan_codex_file(str(f))
        assert res["daily"] == {
            "2026-06-08": {"gpt-5.5": [600, 0, 0, 2400, 120]}
        }

    def test_structural_replay_skipped_but_baselines_deltas(self, tmp_path):
        # Copied prefix spanning multiple wall-clock seconds: identified by
        # leaf/ancestor session_meta adjacency, ended by the first
        # task_started whose started_at agrees with its own outer clock.
        f = tmp_path / "rollout-replay.jsonl"
        boundary_ts = "2026-06-08T12:00:00.000Z"
        write_lines(f, [
            _session_meta("2026-06-08T11:39:04.000Z", sid="leaf",
                          forked_from="parent"),
            _session_meta("2026-06-08T11:39:04.001Z", sid="parent"),
            _turn_context("2026-06-08T11:39:04.100Z", "gpt-5.5"),
            # replayed history: re-stamped outer clocks, stale started_at
            _task_started("2026-06-08T11:39:04.200Z",
                          _epoch("2026-04-01T00:00:00.000Z")),
            _codex_line("2026-06-08T11:39:04.300Z", 10_000, 8_000, 500),
            _codex_line("2026-06-08T11:39:05.400Z", 500_000, 400_000, 9_000),
            _codex_line("2026-06-08T11:39:07.500Z", 1_000_000, 800_000, 20_000),
            # native boundary: started_at == outer clock
            _task_started(boundary_ts, _epoch(boundary_ts)),
            _codex_line("2026-06-08T12:01:00.000Z", 1_050_000, 840_000, 21_000),
        ])
        res = scan_codex_file(str(f))
        assert list(res["daily"].keys()) == ["2026-06-08"]
        live = res["daily"]["2026-06-08"]["gpt-5.5"]
        # only the post-replay delta: input 50k (cached 40k), out 1k
        assert live == [10_000, 0, 0, 40_000, 1_000]

    def test_fork_metadata_without_adjacent_parent_meta_is_fresh(self, tmp_path):
        # forked_from_id alone is not enough: fresh child threads carry it
        # with no copied prefix. Everything counts.
        f = tmp_path / "rollout-child.jsonl"
        sec = "2026-06-08T11:39:04"
        write_lines(f, [
            _session_meta(sec + ".000Z", sid="child", forked_from="parent"),
            _turn_context(sec + ".050Z", "gpt-5.5"),
            _codex_line(sec + ".100Z", 1000, 600, 50),
            _codex_line(sec + ".900Z", 2000, 1400, 90),
        ])
        res = scan_codex_file(str(f))
        assert res["daily"] == {
            "2026-06-08": {"gpt-5.5": [600, 0, 0, 1400, 90]}
        }

    def test_replay_without_native_boundary_contributes_nothing(self, tmp_path):
        # A structurally identified snapshot still being written (no
        # clock-agreeing task_started yet) is inherited in full.
        f = tmp_path / "rollout-snapshot.jsonl"
        write_lines(f, [
            _session_meta("2026-06-08T11:39:04.000Z", sid="leaf",
                          forked_from="parent"),
            _session_meta("2026-06-08T11:39:04.001Z", sid="parent"),
            _task_started("2026-06-08T11:39:04.200Z",
                          _epoch("2026-04-01T00:00:00.000Z")),
            _codex_line("2026-06-08T11:39:04.300Z", 10_000, 8_000, 500),
            _codex_line("2026-06-08T11:39:06.400Z", 500_000, 400_000, 9_000),
        ])
        res = scan_codex_file(str(f))
        assert res["daily"] == {}

    def test_explicit_reset_starts_new_epoch_and_epochs_are_summed(self, tmp_path):
        f = tmp_path / "rollout-reset.jsonl"
        write_lines(f, [
            _session_meta("2026-05-01T00:00:00.000Z"),
            _turn_context("2026-05-01T00:00:00.100Z", "gpt-5.5"),
            _codex_line("2026-05-01T01:00:00.000Z", 1000, 800, 100),
            _codex_line("2026-05-01T02:00:00.000Z", 0, 0, 0),  # explicit reset
            _codex_line("2026-05-01T03:00:00.000Z", 900, 700, 60),
        ])
        res = scan_codex_file(str(f))
        v = res["daily"]["2026-05-01"]["gpt-5.5"]
        # epoch 1: fresh 200, cached 800, out 100
        # epoch 2 (post-reset counts from zero): fresh 200, cached 700, out 60
        assert v == [400, 0, 0, 1500, 160]

    def test_partial_wobble_clamps_within_epoch(self, tmp_path):
        # A downward dip that is not an explicit all-zero vector is
        # telemetry noise: clamps to zero, creates no epoch, never
        # double-counts when the counter recovers.
        f = tmp_path / "rollout-wobble.jsonl"
        write_lines(f, [
            _session_meta("2026-05-01T00:00:00.000Z"),
            _turn_context("2026-05-01T00:00:00.100Z", "gpt-5.5"),
            _codex_line("2026-05-01T01:00:00.000Z", 1000, 800, 100),
            _codex_line("2026-05-01T02:00:00.000Z", 900, 700, 90),   # wobble
            _codex_line("2026-05-01T03:00:00.000Z", 1000, 800, 100),  # recover
        ])
        res = scan_codex_file(str(f))
        assert res["daily"]["2026-05-01"]["gpt-5.5"] == [200, 0, 0, 800, 100]

    def test_reset_inside_replay_baselines_terminal_epoch(self, tmp_path):
        # A reset within the copied prefix: only the terminal inherited
        # epoch baselines the native continuation.
        f = tmp_path / "rollout-replay-reset.jsonl"
        boundary_ts = "2026-06-08T12:00:00.000Z"
        write_lines(f, [
            _session_meta("2026-06-08T11:39:04.000Z", sid="leaf",
                          forked_from="parent"),
            _session_meta("2026-06-08T11:39:04.001Z", sid="parent"),
            _turn_context("2026-06-08T11:39:04.100Z", "gpt-5.5"),
            _codex_line("2026-06-08T11:39:04.300Z", 1000, 800, 100),
            _codex_line("2026-06-08T11:39:05.000Z", 0, 0, 0),  # replayed reset
            _codex_line("2026-06-08T11:39:06.000Z", 500, 400, 50),
            _task_started(boundary_ts, _epoch(boundary_ts)),
            _codex_line("2026-06-08T12:01:00.000Z", 600, 450, 70),
        ])
        res = scan_codex_file(str(f))
        # native deltas vs terminal inherited epoch (500/400/50):
        # input 100 (cached 50 -> fresh 50), out 20
        assert res["daily"] == {
            "2026-06-08": {"gpt-5.5": [50, 0, 0, 50, 20]}
        }

    def test_rate_limit_heartbeats_neither_reset_nor_contribute(self, tmp_path):
        f = tmp_path / "rollout-heartbeat.jsonl"
        write_lines(f, [
            _session_meta("2026-05-01T00:00:00.000Z"),
            _turn_context("2026-05-01T00:00:00.100Z", "gpt-5.5"),
            _codex_line("2026-05-01T01:00:00.000Z", 1000, 800, 100),
            # no total_token_usage at all
            _rate_limit_line("2026-05-01T01:30:00.000Z"),
            # total_token_usage present but without any component key
            _rate_limit_line("2026-05-01T01:45:00.000Z",
                             totals={"total_tokens": 5}),
            _codex_line("2026-05-01T02:00:00.000Z", 1100, 850, 120),
        ])
        res = scan_codex_file(str(f))
        # e1: fresh 200, cached 800, out 100; e2 delta: fresh 50, cached 50,
        # out 20 — heartbeats created no zero-reset epoch in between.
        assert res["daily"]["2026-05-01"]["gpt-5.5"] == [250, 0, 0, 850, 120]

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


class TestCodexFixtures:
    """Real-format fixtures mirrored from logpile tests/fixtures/codex/."""

    def test_replay_multisecond_counts_only_native_continuation(self):
        res = scan_codex_file(str(FIXTURES / "replay-multisecond.jsonl"))
        # 236.7M inherited input folds into the baseline; only the native
        # delta (input 5000 / cached 3000 / out 500) counts.
        assert res["daily"] == {
            "2026-06-08": {"gpt-5.5": [2000, 0, 0, 3000, 500]}
        }

    def test_counter_reset_epochs_are_summed(self):
        res = scan_codex_file(str(FIXTURES / "counter-reset-epochs.jsonl"))
        assert res["daily"] == {
            "2026-05-01": {"gpt-5.5": [400, 0, 0, 1500, 160]}
        }

    def test_fresh_same_second_burst_is_kept(self):
        res = scan_codex_file(str(FIXTURES / "fresh-same-second.jsonl"))
        assert res["daily"] == {
            "2026-06-08": {"gpt-5.5": [600, 0, 0, 2400, 120]}
        }


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


class TestClaudeDedup:
    def test_stream_snapshots_last_wins(self, tmp_path):
        # Subagent transcripts emit several records per API request: the
        # first is the message_start placeholder (output_tokens ~1), the
        # last carries the billed totals. Last occurrence must win.
        f = tmp_path / "agent-a1.jsonl"
        write_lines(f, [
            _claude_line("2026-07-02T10:00:00.000Z", "claude-fable-5",
                         "msg_1", "req_1", 100, 5000, 20000, 1),
            _claude_line("2026-07-02T10:00:05.000Z", "claude-fable-5",
                         "msg_1", "req_1", 100, 5000, 20000, 87),
        ])
        results = {str(f): scan_claude_file(str(f))}
        picked = extract.dedupe_claude_rows(results, lambda p: "human")
        (day, origin, model, v), = picked.values()
        assert v == [100, 5000, 0, 20000, 87]

    def test_cross_file_resume_copy_counted_once(self, tmp_path):
        line = _claude_line("2026-07-02T10:00:00.000Z", "claude-fable-5",
                            "msg_1", "req_1", 100, 0, 0, 50)
        f1 = tmp_path / "a-original.jsonl"
        f2 = tmp_path / "b-resumed-copy.jsonl"
        write_lines(f1, [line])
        write_lines(f2, [line])
        results = {str(f1): scan_claude_file(str(f1)),
                   str(f2): scan_claude_file(str(f2))}
        picked = extract.dedupe_claude_rows(results, lambda p: "human")
        assert len(picked) == 1
        (_, _, _, v), = picked.values()
        assert v == [100, 0, 0, 0, 50]


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
    def test_seed_wins_when_day_total_larger(self, tmp_path, monkeypatch):
        seed = {
            "cutoff": "2026-07-12",
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

    def test_scan_wins_when_day_total_larger(self, tmp_path, monkeypatch):
        seed = {
            "cutoff": "2026-07-12",
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

    def test_day_level_replacement_no_cross_model_double_count(
        self, tmp_path, monkeypatch
    ):
        # Ledger attributes a session's tokens to ONE model; the scan
        # splits by per-message model. A winning seed day must replace
        # the day wholesale — mixing seed cells with leftover scan cells
        # for other models would count the same tokens twice.
        seed = {
            "cutoff": "2026-07-12",
            "daily": {"2026-06-01": {"claude-fable-5": {
                "human": [0, 0, 1000, 100]}}},
        }
        p = tmp_path / "seed.json"
        p.write_text(json.dumps(seed))
        monkeypatch.setattr(extract, "SEED_PATH", p)
        daily = defaultdict(extract._new_day)
        daily["2026-06-01"]["human"]["claude"]["claude-fable-5"] = [0, 0, 0, 700, 60]
        daily["2026-06-01"]["human"]["claude"]["claude-haiku-4-5"] = [0, 0, 0, 200, 20]
        extract._merge_seed(daily)
        assert dict(daily["2026-06-01"]["human"]["claude"]) == {
            "claude-fable-5": [0, 0, 0, 1000, 100]
        }
        assert dict(daily["2026-06-01"]["automated"]["claude"]) == {}

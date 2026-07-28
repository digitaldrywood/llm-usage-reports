#!/usr/bin/env python3
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

import generate_report as report


class GenerateReportTests(unittest.TestCase):
    def test_provider_classification_uses_model_prefix(self):
        self.assertEqual(report.agent_of("claude-fable-5"), "claude")
        self.assertEqual(report.agent_of("gpt-5.6-sol"), "codex")
        self.assertEqual(report.agent_of("gemini-3.1-pro-preview"), "gemini")
        self.assertEqual(report.agent_of("unrecognized-model"), "other")
        self.assertEqual(report.group_of("claude-opus-4-6"), "opusother")

    def test_bracketed_agent_prefix_is_stripped(self):
        """ccusage reports some sources as "[openclaw] gpt-5.4".

        Unstripped, that GPT spend is misfiled as Other/Other instead of
        counting toward the GPT model group and the Codex agent total.
        """
        self.assertEqual(report.group_of("[openclaw] gpt-5.4"), "gptother")
        self.assertEqual(report.agent_of("[openclaw] gpt-5.4"), "codex")
        self.assertEqual(report.group_of("[openclaw] claude-opus-5"), "opus5")
        self.assertEqual(report.agent_of("[some-agent] gemini-3.5-flash"), "gemini")

    def test_opus_5_has_its_own_group(self):
        self.assertEqual(report.group_of("claude-opus-5"), "opus5")
        self.assertEqual(report.group_of("claude-opus-5-20260701"), "opus5")
        self.assertEqual(report.agent_of("claude-opus-5"), "claude")
        self.assertIn("opus5", report.GLABEL)
        # A future minor line must not be silently mislabeled as Opus 5.
        self.assertNotEqual(report.group_of("claude-opus-5-1"), "opus5")

    @staticmethod
    def _unified_day(period, codex_rows, claude_rows=()):
        agents = [{"agent": "codex", "modelBreakdowns": list(codex_rows)}]
        if claude_rows:
            agents.append({"agent": "claude",
                           "modelBreakdowns": list(claude_rows)})
        return {"period": period, "totalCost": 0.0, "agents": agents}

    def test_fast_codex_costs_are_normalized_per_model(self):
        unified = {
            "daily": [self._unified_day("2026-07-14", [
                {"modelName": "gpt-5.5", "cost": 25.0},
                {"modelName": "gpt-5.6-sol", "cost": 20.0},
                {"modelName": "gpt-5.4-mini", "cost": 2.0},
            ], [
                {"modelName": "claude-fable-5", "cost": 3.0},
            ])],
            "totals": {"totalCost": 50.0},
        }
        codex = {
            "daily": [{"date": "2026-07-14", "costUSD": 21.0}],
            "totals": {"costUSD": 21.0},
        }

        normalized = json.loads(report.normalize_codex_standard(
            json.dumps(unified), json.dumps(codex)))
        costs = {row["modelName"]: row["cost"]
                 for row in normalized["daily"][0]["modelBreakdowns"]}
        self.assertEqual(costs["gpt-5.5"], 10.0)
        self.assertEqual(costs["gpt-5.6-sol"], 10.0)
        self.assertEqual(costs["gpt-5.4-mini"], 1.0)
        self.assertEqual(costs["claude-fable-5"], 3.0)
        self.assertEqual(normalized["totals"]["totalCost"], 24.0)
        self.assertTrue(normalized["_llmUsageReport"]["codexFastPricingDetected"])

    def test_claude_sourced_gpt_is_excluded_from_codex_reconciliation(self):
        """Claude Code delegating to Codex logs GPT rows under the claude agent.

        `ccusage codex daily` never sees that source, so those rows must not
        trigger Fast-mode detection or be rescaled.
        """
        unified = {
            "daily": [self._unified_day("2026-07-22", [
                {"modelName": "gpt-5.6-sol", "cost": 14.0},
            ], [
                {"modelName": "gpt-5.6-sol", "cost": 14.3},
                {"modelName": "claude-fable-5", "cost": 33.9},
            ])],
            "totals": {"totalCost": 62.2},
        }
        codex = {
            "daily": [{"date": "2026-07-22", "costUSD": 14.0}],
            "totals": {"costUSD": 14.0},
        }

        normalized = json.loads(report.normalize_codex_standard(
            json.dumps(unified), json.dumps(codex)))
        meta = normalized["_llmUsageReport"]
        self.assertFalse(meta["codexFastPricingDetected"])
        self.assertAlmostEqual(meta["delegatedGptCost"], 14.3)
        # Both GPT sources merge into one charted row at full, unscaled cost.
        costs = {row["modelName"]: row["cost"]
                 for row in normalized["daily"][0]["modelBreakdowns"]}
        self.assertAlmostEqual(costs["gpt-5.6-sol"], 28.3)
        self.assertAlmostEqual(normalized["totals"]["totalCost"], 62.2)

    def test_live_day_is_exempt_from_strict_reconciliation(self):
        """Today is still being written, so its two ccusage reads disagree.

        The unified and Codex passes are separate invocations seconds apart;
        an agent logging in between makes today's numbers legitimately differ.
        Settled days must still reconcile to the cent.
        """
        unified = {
            "daily": [
                self._unified_day("2026-07-27", [
                    {"modelName": "gpt-5.6-sol", "cost": 100.0}]),
                # Unified saw $40 for today; the later Codex pass saw $55.
                self._unified_day("2026-07-28", [
                    {"modelName": "gpt-5.6-sol", "cost": 40.0}]),
            ],
            "totals": {"totalCost": 140.0},
        }
        codex = {
            "daily": [{"date": "2026-07-27", "costUSD": 100.0},
                      {"date": "2026-07-28", "costUSD": 55.0}],
            "totals": {"costUSD": 155.0},
        }

        normalized = json.loads(report.normalize_codex_standard(
            json.dumps(unified), json.dumps(codex), live_date="2026-07-28"))
        # No false Fast detection, and today's $40 is left untouched.
        self.assertFalse(normalized["_llmUsageReport"]["codexFastPricingDetected"])
        today = normalized["daily"][1]["modelBreakdowns"][0]
        self.assertEqual(today["cost"], 40.0)

        # Without the live-day exemption the same payload must still fail.
        with self.assertRaisesRegex(RuntimeError, "2026-07-27"):
            report.normalize_codex_standard(json.dumps(unified), json.dumps(codex))

    def test_unified_payload_without_by_agent_fails_loudly(self):
        unified = {
            "daily": [{"period": "2026-07-14", "totalCost": 5.0,
                       "modelBreakdowns": [{"modelName": "gpt-5.6-sol", "cost": 5.0}]}],
            "totals": {"totalCost": 5.0},
        }
        codex = {"daily": [{"date": "2026-07-14", "costUSD": 5.0}],
                 "totals": {"costUSD": 5.0}}
        with self.assertRaisesRegex(RuntimeError, "--by-agent"):
            report.normalize_codex_standard(json.dumps(unified), json.dumps(codex))

    def test_archive_window_bounds(self):
        import datetime as dt
        # previous_month must handle the Jan rollover and short months.
        self.assertEqual(report.previous_month(dt.date(2026, 1, 15)),
                         (dt.date(2025, 12, 1), dt.date(2025, 12, 31)))
        self.assertEqual(report.previous_month(dt.date(2026, 3, 1)),
                         (dt.date(2026, 2, 1), dt.date(2026, 2, 28)))
        self.assertEqual(report.month_bounds("2026-02"),
                         (dt.date(2026, 2, 1), dt.date(2026, 2, 28)))
        self.assertEqual(report.month_bounds("2024-02")[1], dt.date(2024, 2, 29))
        self.assertEqual(report.month_bounds("2026-12"),
                         (dt.date(2026, 12, 1), dt.date(2026, 12, 31)))
        with self.assertRaises(SystemExit):
            report.month_bounds("2026-13")

    def test_archive_is_skipped_when_already_frozen(self):
        """Archives are written once and never overwritten — they're the
        durable record that outlives agent log pruning."""
        original = report.RAW
        with tempfile.TemporaryDirectory() as tmp:
            report.RAW = tmp
            try:
                cfg = {"timezone": "UTC", "machines": [{"id": "m1"}]}
                path = report.archive_path("m1", "2026-06-01", "2026-06-30")
                Path(path).write_text('{"already": true}', encoding="utf-8")
                # collect_machine would raise if called — proving the skip.
                written = report.archive_window(
                    cfg, __import__("datetime").date(2026, 6, 1),
                    __import__("datetime").date(2026, 6, 30))
                self.assertEqual(written, [])
                self.assertEqual(Path(path).read_text(), '{"already": true}')
            finally:
                report.RAW = original

    def _write_config(self, tmp, cfg):
        path = Path(tmp) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return str(path)

    def test_config_defaults_and_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = report.load_config(self._write_config(tmp, {
                "machines": [{"id": "here"}, {"id": "there", "ssh": "u@h"}]}))
            self.assertEqual(cfg["timezone"], "America/Chicago")
            self.assertIsNone(cfg["publishCommand"])
            # label falls back to id; remoteShell has a login-shell default
            self.assertEqual(cfg["machines"][0]["label"], "here")
            self.assertEqual(cfg["machines"][1]["remoteShell"], "zsh -lc")

            # Exactly one machine may omit ssh — it's the one running the script.
            for machines in ([{"id": "a"}, {"id": "b"}],          # two local
                             [{"id": "a", "ssh": "u@h"}],          # none local
                             [{"id": "a"}, {"id": "a", "ssh": "x"}]):  # dup id
                with self.assertRaises(SystemExit):
                    report.load_config(self._write_config(tmp, {"machines": machines}))

            with self.assertRaises(SystemExit):
                report.load_config(self._write_config(tmp, {"machines": []}))

    def test_missing_config_points_at_the_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(SystemExit) as ctx:
                report.load_config(str(Path(tmp) / "nope.json"))
        self.assertIn("config.sample.json", str(ctx.exception))

    def test_shipped_sample_config_is_valid(self):
        """The committed template must actually load, or first-run setup breaks."""
        cfg = report.load_config(report.CONFIG_SAMPLE)
        self.assertGreaterEqual(len(cfg["machines"]), 1)
        self.assertIsNone(cfg["publishCommand"],
                          "sample must not publish anywhere by default")
        # No real host should ever land in the committed sample.
        self.assertNotIn("@100.", json.dumps(cfg))

    def test_fast_incident_is_opened_and_resolved(self):
        original = report.FAST_INCIDENTS
        with tempfile.TemporaryDirectory() as tmp:
            report.FAST_INCIDENTS = str(Path(tmp) / "incidents.json")
            try:
                report.update_fast_incidents(
                    {"machine-a": False, "machine-b": True},
                    "2026-06-15", "2026-07-14", "2026-07-15")
                report.update_fast_incidents(
                    {"machine-a": False, "machine-b": False},
                    "2026-06-16", "2026-07-15", "2026-07-16")
                history = json.loads(Path(report.FAST_INCIDENTS).read_text())
            finally:
                report.FAST_INCIDENTS = original

        self.assertEqual(len(history["incidents"]), 1)
        incident = history["incidents"][0]
        self.assertEqual(incident["machine"], "machine-b")
        self.assertEqual(incident["observedReportFrom"], "2026-06-15")
        self.assertEqual(incident["observedReportThrough"], "2026-07-14")
        self.assertEqual(incident["resolvedOn"], "2026-07-16")

    def test_unpriced_warning_ignores_free_local_models(self):
        """A hosted model at $0 is a missing price; a local one is just free."""
        payload = {"daily": [{
            "period": "2026-07-14", "cacheReadTokens": 0, "outputTokens": 0,
            "totalTokens": 0,
            "modelBreakdowns": [
                {"modelName": "gemini-3.5-flash", "cost": 0.0,
                 "inputTokens": 5000, "outputTokens": 10},
                {"modelName": "llama3.2:3b", "cost": 0.0,
                 "inputTokens": 5000, "outputTokens": 10},
            ],
        }], "totals": {"totalCost": 0.0}}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "only.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with contextlib.redirect_stdout(io.StringIO()) as out:
                report.load({"machine-a": path}, ["2026-07-14"])
        warnings = out.getvalue()
        self.assertIn("gemini-3.5-flash reports tokens but $0 cost", warnings)
        self.assertNotIn("llama3.2:3b reports tokens", warnings)
        # The local model is still surfaced as unclassified, not silently dropped.
        self.assertIn("llama3.2:3b is not in MODEL_GROUPS", warnings)

    def test_load_preserves_precision_and_separates_gemini(self):
        payload = {
            "daily": [{
                "period": "2026-07-14",
                "cacheReadTokens": 0,
                "outputTokens": 0,
                "totalTokens": 0,
                "modelBreakdowns": [
                    {"modelName": "claude-fable-5", "cost": 0.004},
                    {"modelName": "gemini-3.1-pro-preview", "cost": 0.004},
                ],
            }],
            "totals": {"totalCost": 0.008},
        }
        with tempfile.TemporaryDirectory() as tmp:
            first = Path(tmp) / "first.json"
            second = Path(tmp) / "second.json"
            first.write_text(json.dumps(payload), encoding="utf-8")
            second.write_text(json.dumps({"daily": [], "totals": {"totalCost": 0}}),
                              encoding="utf-8")
            data = report.load({"machine-a": first, "machine-b": second},
                               ["2026-07-14"])

        self.assertAlmostEqual(data["grand"], 0.008)
        self.assertAlmostEqual(data["agents"]["claude"][0], 0.004)
        self.assertAlmostEqual(data["agents"]["gemini"][0], 0.004)
        self.assertEqual(report.usd(data["grand"]), "$0.01")


if __name__ == "__main__":
    unittest.main()

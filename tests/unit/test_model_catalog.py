"""Offline, deterministic tests for verified model selector catalogs and launch validation."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.agent_dashboard.model_catalog import ModelCatalog


class FakeSupervisor:
    def __init__(self, root: Path):
        self.root = root
        self.bus = root / "coordination"
        self.bus.mkdir(exist_ok=True)
    def _runner_prefix(self, name):
        return ["runner", name]


class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = ModelCatalog(FakeSupervisor(self.root), ttl_seconds=30)

    def tearDown(self):
        self.temp.cleanup()

    def test_codex_cli_metadata_controls_models_and_effort(self):
        payload = {"models": [
            {"slug":"gpt-6.1-sol","display_name":"Sol 6.1","visibility":"list",
             "default_reasoning_level":"medium",
             "supported_reasoning_levels":[{"effort":"low"},{"effort":"high"},{"effort":"ultra"}]},
            {"slug":"secret-review-model","visibility":"hide",
             "supported_reasoning_levels":[{"effort":"high"}]},
            {"slug":"invalid model; shell","visibility":"list",
             "supported_reasoning_levels":[{"effort":"high"}]},
        ]}
        with patch.object(ModelCatalog,"_run",return_value=json.dumps(payload)) as runner:
            result=self.catalog.get("codex",refresh=True)
        self.assertEqual(result["source"],"live_codex_catalog")
        self.assertEqual([m["id"] for m in result["models"]],["gpt-6.1-sol"])
        self.assertEqual(result["models"][0]["efforts"],["low","high","ultra"])
        self.assertEqual(result["models"][0]["default_effort"],"low")
        self.assertEqual(runner.call_args.args[1],self.root.home())
        self.catalog.validate("codex","gpt-6.1-sol","ultra")
        with self.assertRaisesRegex(ValueError,"does not support"):
            self.catalog.validate("codex","gpt-6.1-sol","max")
        with self.assertRaisesRegex(ValueError,"dropdown"):
            self.catalog.validate("codex","secret-review-model","high")

    def test_agy_cli_tiers_are_locked_and_label_is_from_cli(self):
        raw=("gemini-3.8-flash-high\tGemini 3.8 Flash (High)\n"
             "gemini-3.8-flash-medium\tGemini 3.8 Flash (Medium)\n"
             "claude-opus-4-6-thinking\tOpus Thinking\n"
             "malformed no tab\n")
        with patch.object(ModelCatalog,"_run",return_value=raw):
            result=self.catalog.get("antigravity",refresh=True)
        self.assertEqual(len(result["models"]),3)
        high=result["models"][0]
        self.assertTrue(high["effort_embedded"])
        self.assertEqual(high["efforts"],["high"])
        with self.assertRaises(ValueError):
            self.catalog.validate("antigravity",high["id"],"medium")
        self.assertEqual(result["models"][2]["default_effort"],"high")
        self.assertFalse(result["models"][2]["effort_embedded"])

    def test_claude_aliases_and_session_presets(self):
        result=self.catalog.get("claude")
        self.assertIn("opus",[m["id"] for m in result["models"]])
        self.assertIn("sonnet",[m["id"] for m in result["models"]])
        self.assertEqual(result["source"],"cli_documented_aliases")
        self.assertIn("Release Validator",result["agent_presets"])

    def test_last_known_good_catalog_is_clearly_marked_stale(self):
        raw="gemini-3.8-flash-high\tGemini High\n"
        with patch.object(ModelCatalog,"_run",return_value=raw):
            self.catalog.get("antigravity",refresh=True)
        restored=ModelCatalog(FakeSupervisor(self.root),ttl_seconds=0)
        with patch.object(ModelCatalog,"_discover",side_effect=RuntimeError("offline")):
            result=restored.get("antigravity",refresh=True)
        self.assertTrue(result["stale"])
        self.assertEqual(result["source"],"cached_catalog")
        self.assertEqual(result["models"][0]["id"],"gemini-3.8-flash-high")
        self.assertIn("unavailable",result["warning"].lower())

    def test_no_guessing_when_cli_catalog_is_unavailable(self):
        with patch.object(ModelCatalog,"_discover",side_effect=RuntimeError("no auth")):
            result=self.catalog.get("codex")
        self.assertEqual(result["models"],[])
        self.assertTrue(result["stale"])
        with self.assertRaisesRegex(ValueError,"dropdown"):
            self.catalog.validate("codex","gpt-6-sol","high")


if __name__=="__main__":
    unittest.main()

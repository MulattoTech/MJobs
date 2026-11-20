from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from tools.ownership_index import build_index, check_index, render, write_index


class OwnershipIndexTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "docs").mkdir()
        (self.root / "services/example").mkdir(parents=True)
        (self.root / "docs/REPO_MAP.md").write_text(
            "| Path | Ownership |\n| --- | --- |\n"
            "| [services/example](../services/example/) | Example service |\n",
            encoding="utf-8",
        )
        (self.root / "docs/AREA_ROUTING.md").write_text(
            "| Area | Requirement range | Intended code ownership | Area spec |\n"
            "|---|---:|---|---|\n"
            "| `example` | MJ-REQ-001…MJ-REQ-002 | `services/example` | `docs/spec/example.md` |\n",
            encoding="utf-8",
        )
        (self.root / "services/example/AREA.md").write_text(
            "# Example area\n\nOwns example behavior.\n\n"
            "Requirements: MJ-REQ-001, MJ-REF-002.\n",
            encoding="utf-8",
        )

    def test_build_is_sorted_and_deterministic(self):
        first = build_index(self.root)
        second = build_index(self.root)
        self.assertEqual(first, second)
        self.assertEqual(first["repository_map"][0]["paths"], ["services/example/"])
        self.assertEqual(first["area_contracts"][0]["requirements"], ["MJ-REF-002", "MJ-REQ-001"])
        self.assertEqual(render(first), render(second))

    def test_write_and_freshness_check(self):
        self.assertTrue(write_index(self.root))
        self.assertEqual(check_index(self.root), (True, "Ownership index current: docs/OWNERSHIP_INDEX.json"))
        (self.root / "docs/REPO_MAP.md").write_text(
            (self.root / "docs/REPO_MAP.md").read_text(encoding="utf-8") + "\n",
            encoding="utf-8",
        )
        current, message = check_index(self.root)
        self.assertFalse(current)
        self.assertIn("stale", message)

    def test_output_is_valid_json(self):
        self.assertEqual(json.loads(render(build_index(self.root)))["schema_version"], 1)


if __name__ == "__main__":
    unittest.main()

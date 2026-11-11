import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from incident_lab import demo, make_fixture, run


class PipelineTests(unittest.TestCase):
    def test_incident_detection_and_recovery(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            result = demo(root / "demo")
            self.assertEqual(result["broken_status"], "failed")
            self.assertEqual(result["recovered_status"], "passed")
            self.assertTrue(result["reconciled"])
            broken = json.loads((root / "demo" / "broken_result.json").read_text())
            codes = {item["code"] for item in broken["incidents"]}
            self.assertTrue({"duplicate_event", "invalid_event", "missing_partition", "orphan_event"} <= codes)
            self.assertEqual(result["net_cents"], 252000)

    def test_rerun_is_idempotent_and_keeps_run_history(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root / "fixture")
            source = root / "fixture" / "clean"
            dbpath = root / "lab.db"
            first = run(source, dbpath)
            second = run(source, dbpath)
            self.assertEqual(first["source_hash"], second["source_hash"])
            self.assertEqual(first["daily"], second["daily"])
            with closing(sqlite3.connect(dbpath)) as db:
                self.assertEqual(db.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0], 294)
                self.assertEqual(db.execute("SELECT COUNT(*) FROM runs").fetchone()[0], 2)


if __name__ == "__main__":
    unittest.main()

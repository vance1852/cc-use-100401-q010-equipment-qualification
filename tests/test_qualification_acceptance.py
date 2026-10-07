from __future__ import annotations

import unittest
from pathlib import Path

from equipment_qualification.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class AcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertEqual(result["schema"]["schema_version"], "1")
        self.assertTrue(result["evidence_replay"])
        self.assertEqual(len(result["recall_voided"]), 1)
        self.assertEqual(result["final_state"]["deepwater_logging"], "evidence_withdrawn")
        self.assertEqual(result["final_state"]["hpht_logging"], "evidence_withdrawn")
        self.assertIn("calibration", result["missing_evidence_final"]["deepwater_logging"])
        self.assertEqual(
            set(result["missing_evidence_final"]["hpht_logging"]),
            {"calibration", "software", "design_conformance"})
        self.assertEqual(len(result["withdrawal_voided"]), 1)


if __name__ == "__main__":
    unittest.main()

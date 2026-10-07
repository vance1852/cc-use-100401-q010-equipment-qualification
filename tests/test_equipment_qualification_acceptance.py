from __future__ import annotations

import unittest
from pathlib import Path

from equipment_qualification.acceptance import run


ROOT = Path(__file__).resolve().parents[1]


class QualificationAcceptanceTests(unittest.TestCase):
    def test_offline_acceptance(self) -> None:
        result = run(ROOT)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["grant_count"], 5)
        self.assertEqual(result["schema"]["missing_tables"], [])
        self.assertNotEqual(result["executed_release"], result["invalidated_release"])
        self.assertTrue(result["missing_after_withdrawal"])


if __name__ == "__main__":
    unittest.main()

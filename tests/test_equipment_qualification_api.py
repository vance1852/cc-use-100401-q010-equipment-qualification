from __future__ import annotations

import json
import sqlite3
import unittest
from pathlib import Path

from equipment_qualification.api import JsonApplication
from equipment_qualification.jsonio import load_json
from equipment_qualification.service import QualificationService


ROOT = Path(__file__).resolve().parents[1]


class QualificationApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(QualificationService(self.connection))
        for user_id, role in (
            ("eng", "equipment_engineer"),
            ("test", "test_engineer"),
            ("appr", "approver"),
            ("ops", "operations"),
        ):
            response = self.app.handle(
                "POST", "/users",
                body=json.dumps(
                    {"user_id": user_id, "display_name": user_id, "role": role}
                ).encode(),
            )
            self.assertEqual(response.status, 201)

    def tearDown(self) -> None:
        self.connection.close()

    def _post(self, path: str, actor: str, payload: dict) -> tuple[int, dict]:
        response = self.app.handle(
            "POST", path, {"X-Actor-Id": actor}, json.dumps(payload).encode()
        )
        return response.status, response.body

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_json_error_shape(self) -> None:
        response = self.app.handle("POST", "/users", body=b"not-json")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_missing_actor(self) -> None:
        status, body = self._post("/equipment", "", {})
        self.assertEqual(status, 422)
        self.assertIn("X-Actor-Id", body["error"]["message"])

    def test_unknown_route(self) -> None:
        response = self.app.handle("GET", "/nothing")
        self.assertEqual(response.status, 404)
        self.assertEqual(response.body["error"]["code"], "route_not_found")

    def test_qualification_flow_over_http(self) -> None:
        status, _ = self._post("/design_versions", "eng", {
            "design_version_id": "hj-dv-1", "family": "海经", "version": "1.0.0",
            "content_sha256": "a" * 64,
        })
        self.assertEqual(status, 201)
        status, _ = self._post("/software_versions", "eng", {
            "software_version_id": "hj-sw-1", "family": "海经", "version": "1.1.0",
            "content_sha256": "b" * 64,
        })
        self.assertEqual(status, 201)
        status, _ = self._post("/component_batches", "eng", {
            "component_batch_id": "cb-1", "component_type": "采集站",
            "batch_no": "2026-A", "manufacturer": "中海油服",
        })
        self.assertEqual(status, 201)
        status, _ = self._post("/equipment", "eng", {
            "equipment_id": "haijing-01", "equipment_name": "海经海底地震采集系统",
            "family": "海经", "serial_no": "HJ-2026-01",
        })
        self.assertEqual(status, 201)
        status, _ = self._post("/equipment/haijing-01/configuration", "eng", {
            "design_version_id": "hj-dv-1", "software_version_id": "hj-sw-1",
            "expected_config_revision": 0,
        })
        self.assertEqual(status, 200)
        status, _ = self._post("/equipment/haijing-01/components", "eng", {
            "component_batch_id": "cb-1",
        })
        self.assertEqual(status, 201)
        protocol = load_json(ROOT / "fixtures" / "qualification_protocol.json")
        status, _ = self._post("/protocols", "test", protocol)
        self.assertEqual(status, 201)
        status, body = self._post("/calibrations", "test", {
            "equipment_id": "haijing-01", "instrument": "罗经-G-2",
            "certificate_no": "CAL-1", "calibrated_at": "2026-05-01T00:00:00Z",
            "valid_until": "2027-05-01T00:00:00Z",
        })
        self.assertEqual(status, 201)
        calibration_id = body["calibration_id"]
        evidence = load_json(ROOT / "fixtures" / "qualification_evidence.json")
        evidence["design_version_id"] = "hj-dv-1"
        status, body = self._post("/evidence", "test", evidence)
        self.assertEqual(status, 201)
        evidence_id = body["evidence_id"]
        status, body = self._post("/grants", "appr", {
            "equipment_id": "haijing-01", "capability": "water_depth",
            "envelope": {"max_water_depth_m": "3000"},
            "evidence_ids": [evidence_id], "calibration_ids": [calibration_id],
        })
        self.assertEqual(status, 201)
        status, body = self._post("/grants", "appr", {
            "equipment_id": "haijing-01", "capability": "water_depth",
            "envelope": {"max_water_depth_m": "3000"},
            "evidence_ids": [evidence_id], "calibration_ids": [calibration_id],
        })
        self.assertEqual(status, 201)
        self.assertTrue(body["replayed"])
        status, _ = self._post("/operations", "ops", {
            "operation_id": "op-1", "well_id": "LH29-1-A1", "phase": "drilling",
            "environment": "hthp", "water_depth_m": "2800", "temperature_c": "145",
            "pressure_mpa": "130", "planned_start": "2026-11-01T00:00:00Z",
        })
        self.assertEqual(status, 201)
        status, body = self._post("/releases", "ops", {
            "operation_id": "op-1", "equipment_id": "haijing-01",
        })
        self.assertEqual(status, 409)
        self.assertEqual(body["error"]["code"], "invalid_state")
        self.assertIn("压力", body["error"]["message"])
        response = self.app.handle(
            "GET", "/equipment/haijing-01/explain", {"X-Actor-Id": "ops"}
        )
        self.assertEqual(response.status, 200)
        self.assertTrue(response.body["capabilities"]["water_depth"]["qualified"])
        self.assertFalse(response.body["capabilities"]["pressure"]["qualified"])
        self.assertEqual(
            response.body["capabilities"]["pressure"]["missing"], ["证据已具备，待资格批准"]
        )

    def test_explain_rejects_bad_as_of(self) -> None:
        self._post("/equipment", "eng", {
            "equipment_id": "haijing-01", "equipment_name": "海经",
            "family": "海经", "serial_no": "HJ-1",
        })
        response = self.app.handle(
            "GET", "/equipment/haijing-01/explain?as_of=not-a-time", {"X-Actor-Id": "eng"}
        )
        self.assertEqual(response.status, 422)


if __name__ == "__main__":
    unittest.main()

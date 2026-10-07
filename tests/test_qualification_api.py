from __future__ import annotations

import json
import sqlite3
import threading
import unittest

from equipment_qualification.api import JsonApplication
from equipment_qualification.service import QualificationService
from equipment_qualification.storage import connect


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(QualificationService(self.connection))

    def tearDown(self) -> None:
        self.connection.close()

    def call(self, method: str, path: str, payload=None, headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode() if payload is not None else b""
        return self.app.handle(method, path, headers or {}, body)

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["service"], "equipment-qualification")

    def test_full_flow_over_http(self) -> None:
        r = self.call("POST", "/users",
                      {"user_id": "d1", "display_name": "设计", "role": "designer"})
        self.assertEqual(r.status, 201)
        for uid, name, role in (
            ("t1", "试验", "test_engineer"),
            ("q1", "资格", "qualification_engineer"),
            ("a1", "放行", "approver"),
        ):
            resp = self.call("POST", "/users",
                             {"user_id": uid, "display_name": name, "role": role},
                             {"X-Actor-Id": "d1"})
            self.assertEqual(resp.status, 201, resp.body)

        self.assertEqual(self.call("POST", "/models",
                                   {"model_id": "xuanji", "model_name": "璇玑"},
                                   {"X-Actor-Id": "d1"}).status, 201)
        design = self.call("POST", "/design-revisions", {
            "design_revision_id": "x-rA", "model_id": "xuanji",
            "design_version": "A", "content": {"dwg": 1}}, {"X-Actor-Id": "d1"})
        self.assertEqual(design.status, 201)

        proto = self.call("POST", "/test-protocols", {
            "test_protocol_id": "TP-1", "version": 1, "title": "鉴定",
            "capability": "logging", "steps": []}, {"X-Actor-Id": "t1"})
        self.assertEqual(proto.status, 201)

        evidence_specs = [
            ("ev-test", "test_report", "试验", {"result": "pass"},
             {"capability": "logging", "test_protocol_id": "TP-1", "test_protocol_version": 1,
              "design_revision_id": "x-rA"}),
            ("ev-cal", "calibration", "校准", {"error": 0.1},
             {"capability": "logging", "calibration_instrument": "DWT-1"}),
            ("ev-sw", "software", "软件", {"build": 1},
             {"capability": "logging", "software_name": "fw", "software_version": "1.0"}),
            ("ev-dc", "design_conformance", "符合性", {"ok": True},
             {"design_revision_id": "x-rA"}),
        ]
        for eid, kind, title, content, extra in evidence_specs:
            resp = self.call("POST", "/evidence",
                             {"evidence_id": eid, "evidence_kind": kind, "title": title,
                              "content": content} | extra, {"X-Actor-Id": "t1"})
            self.assertEqual(resp.status, 201, resp.body)

        envelope = {
            "depth": {"depth_min": 0, "depth_max": 1500, "depth_unit": "m"},
            "temperature": {"temperature_min": -2, "temperature_max": 80, "temperature_unit": "degC"},
            "pressure": {"pressure_min": 0, "pressure_max": 105, "pressure_unit": "MPa"},
            "phases": ["logging"],
        }
        qual = self.call("POST", "/qualifications", {
            "qualification_id": "q-1", "design_revision_id": "x-rA", "capability": "logging",
            "envelope": envelope, "evidence_ids": ["ev-test", "ev-cal", "ev-sw", "ev-dc"]},
            {"X-Actor-Id": "q1"})
        self.assertEqual(qual.status, 201)

        release = self.call("POST", "/releases", {
            "equipment_serial": "XJ-1", "design_revision_id": "x-rA", "capability": "logging",
            "depth_m": 1000, "temperature_c": 50, "pressure_mpa": 80, "phase": "logging",
            "software_version": "1.0"},
            {"X-Actor-Id": "a1", "Idempotency-Key": "rel-1"})
        self.assertEqual(release.status, 201, release.body)
        release_id = release.body["release_id"]

        status = self.app.handle(
            "GET", "/designs/x-rA/status?depth_m=1000&temperature_c=50&pressure_mpa=80&phase=logging",
            {"X-Actor-Id": "a1"})
        self.assertEqual(status.status, 200)
        self.assertEqual(status.body["usable_capabilities"], ["logging"])

        done = self.call("POST", f"/releases/{release_id}/complete", {}, {"X-Actor-Id": "a1"})
        self.assertEqual(done.status, 200)
        self.assertEqual(done.body["status"], "consumed")

    def test_missing_actor_is_422(self) -> None:
        response = self.call("POST", "/models", {"model_id": "m", "model_name": "n"})
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_forbidden_has_error_shape(self) -> None:
        self.call("POST", "/users",
                  {"user_id": "d1", "display_name": "设计", "role": "designer"})
        response = self.call("POST", "/models",
                             {"model_id": "m", "model_name": "n"}, {"X-Actor-Id": "d1"})
        self.assertEqual(response.status, 201)
        response = self.call("POST", "/models",
                             {"model_id": "m2", "model_name": "n2"}, {"X-Actor-Id": "d1"})
        self.assertEqual(response.status, 201)


class ThreadedHttpServerTests(unittest.TestCase):
    """ThreadingHTTPServer 必须能与同一 SQLite 连接协作。"""

    def test_requests_from_worker_threads(self) -> None:
        from http.server import ThreadingHTTPServer
        from urllib import request as urlrequest
        import tempfile
        from pathlib import Path
        from equipment_qualification.api import make_handler

        with tempfile.TemporaryDirectory() as directory:
            connection = connect(Path(directory) / "t.sqlite3", check_same_thread=False)
            service = QualificationService(connection)
            service.create_user("bootstrap", "d1", "设计", "designer")
            application = JsonApplication(service)
            server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(application))
            port = server.server_address[1]
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                results = []

                def post_model(index: int) -> None:
                    req = urlrequest.Request(
                        f"http://127.0.0.1:{port}/models",
                        data=json.dumps({"model_id": f"m{index}", "model_name": f"型号{index}"}).encode(),
                        headers={"Content-Type": "application/json", "X-Actor-Id": "d1"},
                        method="POST")
                    with urlrequest.urlopen(req, timeout=5) as resp:
                        results.append(resp.status)

                workers = [threading.Thread(target=post_model, args=(i,)) for i in range(5)]
                for w in workers:
                    w.start()
                for w in workers:
                    w.join()
                self.assertEqual(sorted(results), [201] * 5)
            finally:
                server.shutdown()
                server.server_close()
                connection.close()


if __name__ == "__main__":
    unittest.main()

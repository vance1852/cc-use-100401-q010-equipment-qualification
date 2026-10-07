from __future__ import annotations

import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from equipment_qualification.clock import FrozenClock
from equipment_qualification.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from equipment_qualification.jsonio import load_json
from equipment_qualification.service import QualificationService
from equipment_qualification.storage import connect


ROOT = Path(__file__).resolve().parents[1]

ENVELOPES = {
    "water_depth": {"max_water_depth_m": "3000"},
    "temperature": {"min_temperature_c": "-20", "max_temperature_c": "150"},
    "pressure": {"max_pressure_mpa": "140"},
    "phase": {"phases": ["drilling", "logging"]},
    "environment": {"environments": ["normal", "hthp"]},
}


class QualificationServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
        self.service = QualificationService(self.connection, self.clock)
        for user_id, role in (
            ("eng", "equipment_engineer"),
            ("test", "test_engineer"),
            ("appr", "approver"),
            ("ops", "operations"),
            ("aud", "auditor"),
        ):
            self.service.create_user(user_id, user_id, role)
        self.protocol = load_json(ROOT / "fixtures" / "qualification_protocol.json")
        self.evidence = load_json(ROOT / "fixtures" / "qualification_evidence.json")
        self.service.register_design_version("eng", "xuanji-dv-4", "璇玑", "4.2.0", "d" * 64)
        self.service.register_software_version("eng", "xuanji-sw-2", "璇玑", "2.5.1", "5" * 64)
        self.service.register_component_batch("eng", "cb-seal", "高压密封组件", "2026-A", "宝鸡石油机械")
        self.service.register_component_batch("eng", "cb-tel", "遥测模块", "2026-B", "中海油服电子")
        self.service.register_equipment("eng", "xuanji-001", "璇玑旋转导向系统", "璇玑", "XJ-2026-001")
        self.service.configure_equipment("eng", "xuanji-001", "xuanji-dv-4", "xuanji-sw-2", 0)
        self.service.install_component("eng", "xuanji-001", "cb-seal")
        self.service.install_component("eng", "xuanji-001", "cb-tel")
        self.service.publish_protocol("test", self.protocol)
        self.calibration = self.service.submit_calibration("test", {
            "equipment_id": "xuanji-001",
            "instrument": "石英压力计-QG-11",
            "certificate_no": "CAL-2026-0188",
            "calibrated_at": "2026-05-01T00:00:00Z",
            "valid_until": "2027-05-01T00:00:00Z",
        })
        self.evidence_id = self.service.submit_evidence("test", self.evidence)["evidence_id"]

    def tearDown(self) -> None:
        self.connection.close()

    def _grant(self, capability: str, envelope: dict | None = None) -> dict:
        return self.service.grant_qualification(
            "appr", "xuanji-001", capability, envelope or ENVELOPES[capability],
            [self.evidence_id], [self.calibration["calibration_id"]],
        )

    def _grant_all(self) -> None:
        for capability in ENVELOPES:
            self._grant(capability)

    def _plan(self, operation_id: str, depth: str = "2800", temperature: str = "145",
              pressure: str = "130", phase: str = "drilling", environment: str = "hthp") -> None:
        self.service.plan_operation(
            "ops", operation_id, "LH29-1-A1", phase, environment,
            depth, temperature, pressure, "2026-11-01T00:00:00Z",
        )

    # ------------------------------------------------------------------
    # 完整链路与解释
    # ------------------------------------------------------------------

    def test_full_chain_and_explain(self) -> None:
        self._grant_all()
        self._plan("op-1")
        release = self.service.request_release("ops", "op-1", "xuanji-001")
        self.assertEqual(release["state"], "active")
        explanation = self.service.explain_equipment("aud", "xuanji-001")
        self.assertEqual(explanation["configuration"]["design_version_id"], "xuanji-dv-4")
        self.assertEqual(
            explanation["configuration"]["component_batch_ids"], ["cb-seal", "cb-tel"]
        )
        for capability, view in explanation["capabilities"].items():
            self.assertTrue(view["qualified"], capability)
            self.assertEqual(view["missing"], [], capability)
        self.assertEqual(
            explanation["capabilities"]["water_depth"]["envelope"], {"max_water_depth_m": "3000"}
        )
        self.assertEqual(
            explanation["capabilities"]["phase"]["envelope"], {"phases": ["drilling", "logging"]}
        )
        retained = self.service.get_release("aud", release["release_id"])
        self.assertEqual(len(retained["basis"]["grants"]), 5)
        self.assertEqual(retained["basis"]["evidence"][0]["evidence_id"], self.evidence_id)
        self.assertEqual(
            retained["basis"]["calibrations"][0]["calibration_id"],
            self.calibration["calibration_id"],
        )

    def test_explain_reports_missing_evidence_before_qualification(self) -> None:
        explanation = self.service.explain_equipment("eng", "xuanji-001")
        self.assertFalse(explanation["capabilities"]["pressure"]["qualified"])
        self.assertEqual(
            explanation["capabilities"]["pressure"]["missing"], ["证据已具备，待资格批准"]
        )
        fresh = "xuanji-002"
        self.service.register_equipment("eng", fresh, "璇玑旋转导向系统", "璇玑", "XJ-2026-002")
        explanation = self.service.explain_equipment("eng", fresh)
        self.assertEqual(
            explanation["capabilities"]["water_depth"]["missing"],
            ["装备尚未配置设计版本与软件版本"],
        )
        early = self.service.explain_equipment("eng", "xuanji-001", as_of="2026-09-01T00:00:00Z")
        self.assertEqual(
            early["capabilities"]["water_depth"]["missing"], ["装备尚未配置设计版本与软件版本"]
        )

    def test_explain_target_shortfall(self) -> None:
        self._grant_all()
        covered = self.service.explain_equipment(
            "ops", "xuanji-001", target={"water_depth_m": "2500", "phase": "drilling"}
        )
        self.assertTrue(covered["target"]["covered"])
        short = self.service.explain_equipment(
            "ops", "xuanji-001",
            target={"water_depth_m": "3500", "environment": "polar"},
        )
        self.assertFalse(short["target"]["covered"])
        self.assertEqual(sorted(short["target"]["shortfalls"]), ["environment", "water_depth"])
        self.assertTrue(short["target"]["missing"])

    # ------------------------------------------------------------------
    # 资格可按能力部分通过
    # ------------------------------------------------------------------

    def test_partial_qualification_by_capability(self) -> None:
        partial = dict(self.evidence)
        partial["capability_results"] = [
            dict(item) for item in self.evidence["capability_results"]
        ]
        partial["capability_results"][2] = {
            "capability": "pressure",
            "outcome": "fail",
            "demonstrated": {"max_pressure_mpa": "120"},
        }
        evidence_id = self.service.submit_evidence("test", partial)["evidence_id"]
        granted = self.service.grant_qualification(
            "appr", "xuanji-001", "water_depth", ENVELOPES["water_depth"],
            [evidence_id], [self.calibration["calibration_id"]],
        )
        self.assertFalse(granted["replayed"])
        with self.assertRaises(ValidationFailed):
            self.service.grant_qualification(
                "appr", "xuanji-001", "pressure", ENVELOPES["pressure"],
                [evidence_id], [self.calibration["calibration_id"]],
            )
        explanation = self.service.explain_equipment("eng", "xuanji-001")
        self.assertTrue(explanation["capabilities"]["water_depth"]["qualified"])
        self.assertFalse(explanation["capabilities"]["pressure"]["qualified"])

    def test_grant_envelope_must_stay_within_demonstrated(self) -> None:
        with self.assertRaises(ValidationFailed):
            self._grant("water_depth", {"max_water_depth_m": "3500"})
        with self.assertRaises(ValidationFailed):
            self._grant("temperature", {"min_temperature_c": "-30", "max_temperature_c": "150"})
        with self.assertRaises(ValidationFailed):
            self._grant("phase", {"phases": ["drilling", "production"]})

    def test_evidence_must_meet_protocol_minimum(self) -> None:
        weak = dict(self.evidence)
        weak["capability_results"] = [
            {"capability": "water_depth", "outcome": "pass", "demonstrated": {"max_water_depth_m": "2500"}},
        ]
        with self.assertRaises(ValidationFailed):
            self.service.submit_evidence("test", weak)

    def test_envelope_validation(self) -> None:
        with self.assertRaises(ValidationFailed):
            self._grant("temperature", {"min_temperature_c": "160", "max_temperature_c": "150"})
        with self.assertRaises(ValidationFailed):
            self._grant("environment", {"environments": ["tropical"]})
        with self.assertRaises(ValidationFailed):
            self._grant("water_depth", {"max_water_depth_m": "3000", "extra": "1"})

    # ------------------------------------------------------------------
    # 相同证据重放不重复批准
    # ------------------------------------------------------------------

    def test_evidence_replay_does_not_duplicate(self) -> None:
        first = self.service.submit_evidence("test", self.evidence)
        self.assertTrue(first["replayed"])
        self.assertEqual(first["evidence_id"], self.evidence_id)
        count = self.connection.execute("SELECT count(*) FROM test_evidence").fetchone()[0]
        self.assertEqual(count, 1)
        shuffled = dict(self.evidence)
        shuffled["capability_results"] = list(reversed(self.evidence["capability_results"]))
        again = self.service.submit_evidence("test", shuffled)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["evidence_id"], self.evidence_id)

    def test_grant_replay_does_not_duplicate(self) -> None:
        first = self._grant("water_depth")
        second = self._grant("water_depth")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["grant_id"], second["grant_id"])
        count = self.connection.execute("SELECT count(*) FROM qualification_grants").fetchone()[0]
        self.assertEqual(count, 1)
        events = self.connection.execute(
            "SELECT count(*) FROM audit_events WHERE event_type='qualification.granted'"
        ).fetchone()[0]
        self.assertEqual(events, 1)

    def test_calibration_replay_does_not_duplicate(self) -> None:
        replay = self.service.submit_calibration("test", {
            "equipment_id": "xuanji-001",
            "instrument": "石英压力计-QG-11",
            "certificate_no": "CAL-2026-0188",
            "calibrated_at": "2026-05-01T00:00:00Z",
            "valid_until": "2027-05-01T00:00:00Z",
        })
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["calibration_id"], self.calibration["calibration_id"])

    # ------------------------------------------------------------------
    # 并发放行只能成功一次
    # ------------------------------------------------------------------

    def test_release_only_succeeds_once(self) -> None:
        self._grant_all()
        self._plan("op-1")
        first = self.service.request_release("ops", "op-1", "xuanji-001")
        with self.assertRaises(Conflict):
            self.service.request_release("ops", "op-1", "xuanji-001")
        count = self.connection.execute("SELECT count(*) FROM releases").fetchone()[0]
        self.assertEqual(count, 1)
        retained = self.service.get_release("aud", first["release_id"])
        self.assertEqual(retained["state"], "active")

    def test_release_requires_qualification_coverage(self) -> None:
        self._grant("water_depth")
        self._plan("op-1")
        with self.assertRaises(InvalidState) as caught:
            self.service.request_release("ops", "op-1", "xuanji-001")
        self.assertIn("压力", str(caught.exception))
        self.assertIn("作业阶段", str(caught.exception))

    # ------------------------------------------------------------------
    # 试验撤回与部件召回只影响尚未执行的作业
    # ------------------------------------------------------------------

    def test_withdrawal_only_affects_pending_operations(self) -> None:
        self._grant_all()
        self._plan("op-done")
        executed = self.service.request_release("ops", "op-done", "xuanji-001")
        self.service.complete_operation("ops", "op-done")
        self.clock.advance(hours=1)
        self._plan("op-pending")
        pending = self.service.request_release("ops", "op-pending", "xuanji-001")
        before = self.service.explain_equipment("aud", "xuanji-001")["as_of"]
        self.clock.advance(hours=1)
        result = self.service.withdraw_evidence("test", self.evidence_id, "试验曲线复核发现超差")
        self.assertEqual(result["invalidated_releases"], [pending["release_id"]])
        retained = self.service.get_release("aud", executed["release_id"])
        self.assertEqual(retained["state"], "executed")
        self.assertEqual(retained["basis"]["evidence"][0]["evidence_id"], self.evidence_id)
        invalidated = self.service.get_release("aud", pending["release_id"])
        self.assertEqual(invalidated["state"], "invalidated")
        self.assertIn("撤回", invalidated["invalidate_reason"])
        historical = self.service.explain_equipment("aud", "xuanji-001", as_of=before)
        self.assertTrue(historical["capabilities"]["pressure"]["qualified"])
        current = self.service.explain_equipment("aud", "xuanji-001")
        self.assertFalse(current["capabilities"]["pressure"]["qualified"])
        self.assertIn("已撤回", current["capabilities"]["pressure"]["missing"][0])
        self._plan("op-new")
        with self.assertRaises(InvalidState):
            self.service.request_release("ops", "op-new", "xuanji-001")
        replay = self.service.submit_evidence("test", self.evidence)
        self.assertTrue(replay["replayed"])
        self.assertEqual(replay["status"], "withdrawn")

    def test_recall_only_affects_pending_operations(self) -> None:
        self._grant_all()
        self._plan("op-done")
        executed = self.service.request_release("ops", "op-done", "xuanji-001")
        self.service.complete_operation("ops", "op-done")
        self._plan("op-pending")
        pending = self.service.request_release("ops", "op-pending", "xuanji-001")
        result = self.service.recall_component("eng", "cb-seal", "密封件批次发现材质缺陷")
        self.assertEqual(result["invalidated_releases"], [pending["release_id"]])
        self.assertEqual(self.service.get_release("aud", executed["release_id"])["state"], "executed")
        current = self.service.explain_equipment("eng", "xuanji-001")
        self.assertFalse(current["capabilities"]["water_depth"]["qualified"])
        self.assertIn("已召回", current["capabilities"]["water_depth"]["missing"][0])
        with self.assertRaises(InvalidState):
            self.service.install_component("eng", "xuanji-001", "cb-seal")

    def test_cancel_operation_invalidates_pending_releases(self) -> None:
        self._grant_all()
        self._plan("op-1")
        release = self.service.request_release("ops", "op-1", "xuanji-001")
        self.service.cancel_operation("ops", "op-1", "平台计划调整")
        self.assertEqual(
            self.service.get_release("aud", release["release_id"])["state"], "invalidated"
        )

    # ------------------------------------------------------------------
    # 偏差处置
    # ------------------------------------------------------------------

    def test_deviation_covers_gap_once_approved(self) -> None:
        self._grant_all()
        self._plan("op-deep", depth="3500")
        with self.assertRaises(InvalidState):
            self.service.request_release("ops", "op-deep", "xuanji-001")
        deviation = self.service.request_deviation(
            "ops", "op-deep", "xuanji-001", "water_depth", "邻井同型装备已完成 3500m 验证，本井限时使用"
        )
        reviewed = self.service.review_deviation(
            "appr", deviation["deviation_id"], True, "同意本井使用", "2026-12-31T00:00:00Z"
        )
        self.assertEqual(reviewed["status"], "approved")
        release = self.service.request_release("ops", "op-deep", "xuanji-001")
        basis = self.service.get_release("aud", release["release_id"])["basis"]
        self.assertEqual(basis["deviations"][0]["deviation_id"], deviation["deviation_id"])
        status = self.connection.execute(
            "SELECT status FROM deviations WHERE deviation_id=?", (deviation["deviation_id"],)
        ).fetchone()[0]
        self.assertEqual(status, "used")

    def test_rejected_deviation_does_not_release(self) -> None:
        self._grant_all()
        self._plan("op-deep", depth="3500")
        deviation = self.service.request_deviation(
            "ops", "op-deep", "xuanji-001", "water_depth", "尝试超包络使用"
        )
        reviewed = self.service.review_deviation("appr", deviation["deviation_id"], False, "证据不足")
        self.assertEqual(reviewed["status"], "rejected")
        with self.assertRaises(InvalidState):
            self.service.request_release("ops", "op-deep", "xuanji-001")
        follow_up = self.service.request_deviation(
            "ops", "op-deep", "xuanji-001", "water_depth", "补充邻井数据后再次申请"
        )
        self.assertEqual(follow_up["status"], "requested")

    def test_expired_deviation_does_not_release(self) -> None:
        self._grant_all()
        self._plan("op-deep", depth="3500")
        deviation = self.service.request_deviation(
            "ops", "op-deep", "xuanji-001", "water_depth", "限时偏差"
        )
        self.service.review_deviation(
            "appr", deviation["deviation_id"], True, "限期两周", "2026-10-10T00:00:00Z"
        )
        self.clock.advance(days=20)
        with self.assertRaises(InvalidState):
            self.service.request_release("ops", "op-deep", "xuanji-001")

    # ------------------------------------------------------------------
    # 时间维度：校准过期、配置变更、批准有效期
    # ------------------------------------------------------------------

    def test_calibration_expiry_blocks_qualification(self) -> None:
        expiring = self.service.submit_calibration("test", {
            "equipment_id": "xuanji-001",
            "instrument": "温度传感器-T-3",
            "certificate_no": "CAL-2026-0201",
            "calibrated_at": "2026-04-01T00:00:00Z",
            "valid_until": "2026-10-10T00:00:00Z",
        })
        self.service.grant_qualification(
            "appr", "xuanji-001", "water_depth", ENVELOPES["water_depth"],
            [self.evidence_id], [expiring["calibration_id"]],
        )
        self.clock.advance(days=15)
        explanation = self.service.explain_equipment("eng", "xuanji-001")
        self.assertFalse(explanation["capabilities"]["water_depth"]["qualified"])
        self.assertIn("已过期", explanation["capabilities"]["water_depth"]["missing"][0])
        historical = self.service.explain_equipment("eng", "xuanji-001", as_of="2026-10-05T00:00:00Z")
        self.assertTrue(historical["capabilities"]["water_depth"]["qualified"])

    def test_configuration_change_requires_reapproval(self) -> None:
        self._grant_all()
        self.service.register_software_version("eng", "xuanji-sw-3", "璇玑", "2.6.0", "6" * 64)
        self.clock.advance(hours=1)
        self.service.configure_equipment("eng", "xuanji-001", "xuanji-dv-4", "xuanji-sw-3", 1)
        explanation = self.service.explain_equipment("eng", "xuanji-001")
        self.assertFalse(explanation["capabilities"]["water_depth"]["qualified"])
        self.assertIn("软件版本已变更", explanation["capabilities"]["water_depth"]["missing"][0])
        historical = self.service.explain_equipment(
            "eng", "xuanji-001", as_of="2026-10-01T08:30:00Z"
        )
        self.assertTrue(historical["capabilities"]["water_depth"]["qualified"])
        self._plan("op-1")
        with self.assertRaises(InvalidState):
            self.service.request_release("ops", "op-1", "xuanji-001")

    def test_stale_config_revision_rejected(self) -> None:
        with self.assertRaises(InvalidState):
            self.service.configure_equipment("eng", "xuanji-001", "xuanji-dv-4", "xuanji-sw-2", 0)

    def test_grant_valid_until(self) -> None:
        self.service.grant_qualification(
            "appr", "xuanji-001", "water_depth", ENVELOPES["water_depth"],
            [self.evidence_id], [self.calibration["calibration_id"]],
            valid_until="2026-10-20T00:00:00Z",
        )
        self.clock.advance(days=30)
        explanation = self.service.explain_equipment("eng", "xuanji-001")
        self.assertFalse(explanation["capabilities"]["water_depth"]["qualified"])
        self.assertIn("批准已过期", explanation["capabilities"]["water_depth"]["missing"])

    # ------------------------------------------------------------------
    # 权限与审计
    # ------------------------------------------------------------------

    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.publish_protocol("ops", self.protocol)
        with self.assertRaises(Forbidden):
            self.service.grant_qualification(
                "test", "xuanji-001", "water_depth", ENVELOPES["water_depth"],
                [self.evidence_id], [self.calibration["calibration_id"]],
            )
        with self.assertRaises(Forbidden):
            self.service.request_release("eng", "op-1", "xuanji-001")
        with self.assertRaises(Forbidden):
            self.service.plan_operation(
                "aud", "op-x", "W-1", "drilling", "normal", "100", "20", "10",
                "2026-11-01T00:00:00Z",
            )
        with self.assertRaises(Forbidden):
            self.service.audit_trail("ops", "equipment", "xuanji-001")

    def test_audit_trail_is_hash_chained(self) -> None:
        self._grant_all()
        events = self.service.audit_trail("aud", "equipment", "xuanji-001")
        self.assertEqual(
            [event["event_type"] for event in events],
            ["equipment.registered", "equipment.configured", "component.installed", "component.installed"],
        )
        chained = self.connection.execute(
            "SELECT previous_hash,event_hash FROM audit_events ORDER BY event_id"
        ).fetchall()
        self.assertEqual(chained[0]["previous_hash"], "0" * 64)
        for previous, current in zip(chained, chained[1:]):
            self.assertEqual(current["previous_hash"], previous["event_hash"])

    def test_unknown_entities(self) -> None:
        with self.assertRaises(NotFound):
            self.service.get_equipment("aud", "ghost")
        with self.assertRaises(NotFound):
            self.service.withdraw_evidence("test", 999, "不存在")
        with self.assertRaises(NotFound):
            self.service.request_release("ops", "ghost", "xuanji-001")


class ConcurrentReleaseTests(unittest.TestCase):
    """两个服务实例在同一数据库上并发放行，只有一方能成功。"""

    def test_concurrent_release_succeeds_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = str(Path(temporary) / "qualification.sqlite3")
            for index in range(2):
                connection = connect(database)
                service = QualificationService(
                    connection, FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
                )
                if index == 0:
                    service.create_user("eng", "装备工程师", "equipment_engineer")
                    service.create_user("test", "试验工程师", "test_engineer")
                    service.create_user("appr", "资格审批人", "approver")
                    service.create_user("ops", "作业计划员", "operations")
                    service.register_design_version("eng", "dv-1", "璇玑", "4.2.0", "d" * 64)
                    service.register_software_version("eng", "sw-1", "璇玑", "2.5.1", "5" * 64)
                    service.register_component_batch("eng", "cb-1", "密封组件", "2026-A", "制造商")
                    service.register_equipment("eng", "eq-1", "璇玑系统", "璇玑", "XJ-1")
                    service.configure_equipment("eng", "eq-1", "dv-1", "sw-1", 0)
                    service.install_component("eng", "eq-1", "cb-1")
                    protocol = load_json(ROOT / "fixtures" / "qualification_protocol.json")
                    evidence = load_json(ROOT / "fixtures" / "qualification_evidence.json")
                    evidence["design_version_id"] = "dv-1"
                    service.publish_protocol("test", protocol)
                    calibration = service.submit_calibration("test", {
                        "equipment_id": "eq-1", "instrument": "压力计",
                        "certificate_no": "CAL-1", "calibrated_at": "2026-05-01T00:00:00Z",
                        "valid_until": "2027-05-01T00:00:00Z",
                    })
                    evidence_id = service.submit_evidence("test", evidence)["evidence_id"]
                    for capability, envelope in ENVELOPES.items():
                        service.grant_qualification(
                            "appr", "eq-1", capability, envelope,
                            [evidence_id], [calibration["calibration_id"]],
                        )
                    service.plan_operation(
                        "ops", "op-race", "W-1", "drilling", "hthp",
                        "2800", "145", "130", "2026-11-01T00:00:00Z",
                    )
                connection.close()
            outcomes: list[object] = []

            def attempt() -> None:
                connection = connect(database)
                try:
                    service = QualificationService(connection)
                    outcomes.append(service.request_release("ops", "op-race", "eq-1"))
                except (Conflict, InvalidState) as exc:
                    outcomes.append(exc)
                finally:
                    connection.close()

            threads = [threading.Thread(target=attempt) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            successes = [item for item in outcomes if isinstance(item, dict)]
            failures = [item for item in outcomes if isinstance(item, Conflict)]
            self.assertEqual(len(successes), 1)
            self.assertEqual(len(failures), 1)
            connection = connect(database)
            try:
                count = connection.execute("SELECT count(*) FROM releases").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(count, 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timedelta, timezone

from equipment_qualification.clock import FrozenClock, isoformat
from equipment_qualification.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from equipment_qualification.service import QualificationService


class QualificationFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 6, 1, 8, 0, tzinfo=timezone.utc))
        self.s = QualificationService(self.connection, self.clock)
        self.s.create_user("bootstrap", "designer", "设计负责人", "designer")
        for uid, name, role in (
            ("tester", "试验工程师", "test_engineer"),
            ("qe", "资格工程师", "qualification_engineer"),
            ("approver", "放行人", "approver"),
            ("auditor", "审计人员", "auditor"),
        ):
            self.s.create_user("designer", uid, name, role)
        self.s.register_model("designer", "haijing", "海经海底采集装备")
        self.s.register_design_revision(
            "designer", "hj-r1", "haijing", "rev-1", {"drawing": "HJ-1", "rating_mpa": 105})
        self.s.register_component_batch(
            "designer", "cb-1", "CONN-7", "v1", {"lot": "2026-04"})
        self.s.attach_component("designer", "hj-r1", "cb-1")
        self.s.publish_test_protocol("tester", {
            "test_protocol_id": "TP-1", "version": 1, "title": "鉴定程序",
            "capability": "acquisition", "steps": [{"name": "pressure"}]})
        self.envelope = {
            "depth": {"depth_min": 0, "depth_max": 1500, "depth_unit": "m"},
            "temperature": {"temperature_min": -2, "temperature_max": 80, "temperature_unit": "degC"},
            "pressure": {"pressure_min": 0, "pressure_max": 105, "pressure_unit": "MPa"},
            "phases": ["descent", "logging", "ascent"],
        }

    def tearDown(self) -> None:
        self.connection.close()

    def submit_full_evidence(self, prefix: str = "e", capability: str = "acquisition",
                             design_id: str = "hj-r1") -> list[str]:
        ids = [f"{prefix}-test", f"{prefix}-cal", f"{prefix}-sw", f"{prefix}-dc"]
        self.s.submit_evidence("tester", ids[0], "test_report", "试验报告",
                               {"result": "pass", "report": prefix},
                               capability=capability, test_protocol_id="TP-1",
                               test_protocol_version=1, design_revision_id=design_id,
                               component_batch_id="cb-1")
        self.s.submit_evidence("tester", ids[1], "calibration", "校准证书",
                               {"error": 0.2, "certificate": prefix},
                               capability=capability, design_revision_id=design_id,
                               calibration_instrument="DWT-1")
        self.s.submit_evidence("tester", ids[2], "software", "软件认证",
                               {"build": "100", "family": prefix},
                               capability=capability, software_name="hj-fw",
                               software_version="1.0.0")
        self.s.submit_evidence("qe", ids[3], "design_conformance", "符合性声明",
                               {"ok": True, "conformance": prefix},
                               design_revision_id=design_id)
        return ids

    def grant(self, qualification_id: str = "q-1", evidence_ids=None,
              envelope=None, capability: str = "acquisition") -> dict:
        return self.s.grant_qualification(
            "qe", qualification_id, "hj-r1", capability, envelope or self.envelope,
            evidence_ids if evidence_ids is not None else self.submit_full_evidence())


class QualificationLifecycleTests(QualificationFixture):
    def test_full_qualification_is_not_partial(self) -> None:
        result = self.grant()
        self.assertFalse(result["partial"])
        self.assertEqual(result["missing_evidence_kinds"], [])

    def test_partial_qualification_reports_missing_kinds(self) -> None:
        self.s.submit_evidence("tester", "e-test", "test_report", "试验报告", {"result": "pass"},
                               capability="acquisition", test_protocol_id="TP-1",
                               test_protocol_version=1)
        result = self.grant("q-partial", ["e-test"])
        self.assertTrue(result["partial"])
        self.assertEqual(result["missing_evidence_kinds"], ["calibration", "software", "design_conformance"])

    def test_withdrawn_evidence_cannot_be_basis(self) -> None:
        self.submit_full_evidence()
        self.s.withdraw_evidence("qe", "e-cal", "超差")
        with self.assertRaises(InvalidState):
            self.grant("q-2", ["e-test", "e-cal", "e-sw", "e-dc"])

    def test_supersede_requires_same_capability_other_design(self) -> None:
        self.grant("q-1")
        self.s.register_design_revision("designer", "hj-r2", "haijing", "rev-2", {"drawing": "HJ-2"})
        ids2 = self.submit_full_evidence("e2", design_id="hj-r2")
        self.s.grant_qualification("qe", "q-2", "hj-r2", "acquisition", self.envelope, ids2)
        result = self.s.supersede_qualification("qe", "q-1", "q-2")
        self.assertEqual(result["status"], "superseded")
        # 反向取代：q-1 已是 superseded，不能再作为继任资格
        with self.assertRaises(InvalidState):
            self.s.supersede_qualification("qe", "q-2", "q-1")

    def test_conformance_evidence_must_target_granted_design(self) -> None:
        self.s.register_design_revision("designer", "hj-r2", "haijing", "rev-2", {"drawing": "HJ-2"})
        # 试验/校准/软件证据不绑死设计版本，唯独符合性证据指向 hj-r1
        self.s.submit_evidence("tester", "x-test", "test_report", "试验报告",
                               {"result": "pass", "report": "x"},
                               capability="acquisition", test_protocol_id="TP-1",
                               test_protocol_version=1, component_batch_id="cb-1")
        self.s.submit_evidence("tester", "x-cal", "calibration", "校准证书",
                               {"error": 0.2, "certificate": "x"},
                               capability="acquisition", calibration_instrument="DWT-1")
        self.s.submit_evidence("tester", "x-sw", "software", "软件认证",
                               {"build": "100", "family": "x"},
                               capability="acquisition", software_name="hj-fw",
                               software_version="1.0.0")
        self.s.submit_evidence("qe", "x-dc", "design_conformance", "符合性声明",
                               {"ok": True, "conformance": "x"}, design_revision_id="hj-r1")
        with self.assertRaises(ValidationFailed) as ctx:
            self.s.grant_qualification("qe", "q-other", "hj-r2", "acquisition",
                                       self.envelope,
                                       ["x-test", "x-cal", "x-sw", "x-dc"])
        self.assertIn("设计符合性证据", str(ctx.exception))


class EvidenceReplayTests(QualificationFixture):
    def test_same_content_returns_same_evidence_without_new_approval(self) -> None:
        content = {"result": "pass", "samples": 3}
        first = self.s.submit_evidence("tester", "ev-1", "test_report", "报告", content,
                                      capability="acquisition", test_protocol_id="TP-1",
                                      test_protocol_version=1)
        second = self.s.submit_evidence("tester", "ev-other-id", "test_report", "另一份报告",
                                        {"samples": 3, "result": "pass"},
                                        capability="acquisition", test_protocol_id="TP-1",
                                        test_protocol_version=1)
        self.assertTrue(second.get("replayed"))
        self.assertEqual(second["evidence_id"], "ev-1")
        self.assertEqual(second["content_sha256"], first["content_sha256"])
        events = self.s.audit_trail("auditor", "evidence", "ev-1")
        self.assertEqual([e["event_type"] for e in events], ["evidence.submitted"])

    def test_grant_is_idempotent_per_qualification_id_only(self) -> None:
        ids = self.submit_full_evidence()
        self.grant("q-1", ids)
        with self.assertRaises(Conflict):
            self.grant("q-1", ids)


class ReleaseTests(QualificationFixture):
    def request(self, key: str = "k-1", **overrides):
        params = dict(equipment_serial="HJ-SN-9", design_revision_id="hj-r1",
                      capability="acquisition", depth_m=1000, temperature_c=50,
                      pressure_mpa=80, phase="logging", software_version="1.0.0")
        params.update(overrides)
        return self.s.release_operation("approver", key, **params)

    def test_release_succeeds_with_full_basis(self) -> None:
        self.grant()
        release = self.request()
        self.assertEqual(release["status"], "released")
        self.assertEqual(len(self.s.get_release(release["release_id"])["basis"]["evidence"]), 4)

    def test_release_key_succeeds_only_once(self) -> None:
        self.grant()
        first = self.request("once")
        replay = self.request("once")
        self.assertEqual(replay["release_id"], first["release_id"])
        self.assertTrue(replay.get("replayed"))
        with self.assertRaises(Conflict):
            self.request("once", equipment_serial="HJ-SN-OTHER")

    def test_outside_envelope_blocks_release(self) -> None:
        self.grant()
        with self.assertRaises(InvalidState):
            self.request("bad-depth", depth_m=2000)
        with self.assertRaises(InvalidState):
            self.request("bad-phase", phase="cementing")

    def test_unapproved_software_version_blocks_release(self) -> None:
        self.grant()
        with self.assertRaises(InvalidState) as ctx:
            self.request("bad-sw", software_version="2.0.0")
        self.assertIn("软件版本不在批准范围", str(ctx.exception))

    def test_partial_qualification_blocked_then_deviation_allows(self) -> None:
        self.s.submit_evidence("tester", "e-test", "test_report", "试验报告", {"result": "pass"},
                               capability="acquisition", test_protocol_id="TP-1",
                               test_protocol_version=1)
        self.s.submit_evidence("tester", "e-cal", "calibration", "校准证书", {"error": 0.2},
                               capability="acquisition", calibration_instrument="DWT-1")
        self.grant("q-partial", ["e-test", "e-cal"])
        with self.assertRaises(InvalidState) as ctx:
            self.request("blocked")
        self.assertIn("证据不完整", str(ctx.exception))

        self.s.record_deviation(
            "qe", "dev-1", "hj-r1", "accepted", "软件认证在途，限本井次",
            isoformat(self.clock.current + timedelta(days=7)), capability="acquisition")
        release = self.request("allowed")
        self.assertEqual(release["deviation_id"], "dev-1")

        # 偏差到期后不能再放行
        self.clock.advance(days=8)
        with self.assertRaises(InvalidState):
            self.request("expired")

    def test_rejected_deviation_does_not_help(self) -> None:
        self.s.submit_evidence("tester", "e-test", "test_report", "试验", {"result": "pass"},
                               capability="acquisition", test_protocol_id="TP-1",
                               test_protocol_version=1)
        self.grant("q-p", ["e-test"])
        self.s.record_deviation(
            "qe", "dev-no", "hj-r1", "rejected", "风险不可接受",
            isoformat(self.clock.current + timedelta(days=7)), capability="acquisition")
        with self.assertRaises(InvalidState):
            self.request("nope")

    def test_complete_is_once_only_and_preserved(self) -> None:
        self.grant()
        release = self.request()
        done = self.s.complete_operation("approver", release["release_id"])
        self.assertEqual(done["status"], "consumed")
        with self.assertRaises(InvalidState):
            self.s.complete_operation("approver", release["release_id"])


class WithdrawalAndRecallTests(QualificationFixture):
    def prepare_two_releases(self):
        self.grant()
        r1 = self.s.release_operation(
            "approver", "k-1", "HJ-1", "hj-r1", "acquisition", 1000, 50, 80,
            "logging", software_version="1.0.0")
        r2 = self.s.release_operation(
            "approver", "k-2", "HJ-2", "hj-r1", "acquisition", 1100, 55, 90,
            "logging", software_version="1.0.0")
        self.s.complete_operation("approver", r1["release_id"])
        return r1, r2

    def test_evidence_withdrawal_only_voids_open_releases(self) -> None:
        r1, r2 = self.prepare_two_releases()
        result = self.s.withdraw_evidence("qe", "e-cal", "复测超差")
        self.assertEqual(result["voided_releases"], [r2["release_id"]])
        self.assertEqual(self.s.get_release(r1["release_id"])["status"], "consumed")
        self.assertEqual(self.s.get_release(r2["release_id"])["status"], "voided")
        self.assertIn("复测超差", self.s.get_release(r2["release_id"])["void_reason"])
        # 已完成放行的依据快照仍保留四条证据
        self.assertEqual(len(self.s.get_release(r1["release_id"])["basis"]["evidence"]), 4)

    def test_recall_only_voids_open_releases_and_blocks_new(self) -> None:
        r1, r2 = self.prepare_two_releases()
        result = self.s.recall_component_batch("qe", "cb-1", "老化不合格")
        self.assertEqual(result["voided_releases"], [r2["release_id"]])
        self.assertEqual(self.s.get_release(r1["release_id"])["status"], "consumed")
        with self.assertRaises(InvalidState) as ctx:
            self.s.release_operation(
                "approver", "k-3", "HJ-3", "hj-r1", "acquisition", 900, 40, 60,
                "logging", software_version="1.0.0")
        self.assertIn("部件批次已召回", str(ctx.exception))

    def test_cannot_complete_voided_release(self) -> None:
        _r1, r2 = self.prepare_two_releases()
        self.s.withdraw_evidence("qe", "e-cal", "x")
        with self.assertRaises(InvalidState):
            self.s.complete_operation("approver", r2["release_id"])

    def test_qualification_withdrawal_also_cascades_to_open_releases(self) -> None:
        _r1, r2 = self.prepare_two_releases()
        result = self.s.withdraw_qualification("qe", "q-1", "资格撤销")
        self.assertEqual(result["voided_releases"], [r2["release_id"]])
        self.assertEqual(self.s.get_release(r2["release_id"])["status"], "voided")

    def test_replay_after_void_returns_voided_release_without_regranting(self) -> None:
        self.grant()
        first = self.s.release_operation(
            "approver", "k-x", "HJ-x", "hj-r1", "acquisition", 1000, 50, 80,
            "logging", software_version="1.0.0")
        self.s.recall_component_batch("qe", "cb-1", "x")
        self.assertEqual(self.s.get_release(first["release_id"])["status"], "voided")
        # 同一放行键重放：返回原（已作废）放行，不会重新成功签发
        replay = self.s.release_operation(
            "approver", "k-x", "HJ-x", "hj-r1", "acquisition", 1000, 50, 80,
            "logging", software_version="1.0.0")
        self.assertEqual(replay["release_id"], first["release_id"])
        self.assertEqual(replay["status"], "voided")
        self.assertTrue(replay.get("replayed"))


class StatusAtTests(QualificationFixture):
    def test_time_travel_explains_boundary_and_gaps(self) -> None:
        ids = self.submit_full_evidence()
        self.grant("q-1", ids)
        t0 = isoformat(self.clock.current)

        # 部分证据：当时完整可用于包络内
        status = self.s.status_at("auditor", "hj-r1", t0,
                                  depth_m=1000, temperature_c=50, pressure_mpa=80, phase="logging")
        cap = status["capabilities"][0]
        self.assertEqual(cap["state"], "qualified")
        self.assertTrue(cap["usable_at_point"])
        self.assertFalse(cap["missing_evidence"])

        # 撤回后：状态与缺口变化，但同一时间点的历史解释仍然有效
        self.clock.advance(days=3)
        self.s.withdraw_evidence("qe", "e-cal", "超差")
        t1 = isoformat(self.clock.current)
        after = self.s.status_at("auditor", "hj-r1", t1,
                                 depth_m=1000, temperature_c=50, pressure_mpa=80, phase="logging")
        cap_after = after["capabilities"][0]
        self.assertEqual(cap_after["state"], "evidence_withdrawn")
        self.assertFalse(cap_after["usable_at_point"])
        self.assertEqual([m["kind"] for m in cap_after["missing_evidence"]], ["calibration"])
        self.assertEqual(cap_after["withdrawn_basis"][0]["evidence_id"], "e-cal")

        before = self.s.status_at("auditor", "hj-r1", t0)
        self.assertEqual(before["capabilities"][0]["state"], "qualified")

    def test_status_filters_by_serial_and_shows_voided_history(self) -> None:
        self.grant()
        r = self.s.release_operation(
            "approver", "k-serial", "HJ-77", "hj-r1", "acquisition", 1000, 50, 80,
            "logging", software_version="1.0.0")
        self.s.withdraw_evidence("qe", "e-cal", "超差")
        status = self.s.status_at("auditor", "hj-r1", equipment_serial="HJ-77")
        self.assertEqual(len(status["releases"]), 1)
        self.assertEqual(status["releases"][0]["status_as_of"], "voided")
        self.assertEqual(status["releases"][0]["release_id"], r["release_id"])

    def test_recalled_batches_listed(self) -> None:
        self.grant()
        self.s.recall_component_batch("qe", "cb-1", "x")
        status = self.s.status_at("auditor", "hj-r1")
        self.assertEqual(status["recalled_component_batches"][0]["component_batch_id"], "cb-1")
        self.assertEqual(status["capabilities"][0]["state"], "component_recalled")


class PermissionTests(QualificationFixture):
    def test_role_separation(self) -> None:
        with self.assertRaises(Forbidden):
            self.s.register_model("tester", "x", "x")
        with self.assertRaises(Forbidden):
            self.s.publish_test_protocol("qe", {
                "test_protocol_id": "TP-x", "version": 1, "title": "t", "capability": "c"})
        with self.assertRaises(Forbidden):
            self.s.grant_qualification("tester", "q", "hj-r1", "c", self.envelope, [])
        with self.assertRaises(Forbidden):
            self.s.release_operation("qe", "k", "S", "hj-r1", "c", 1, 1, 1, "logging")
        with self.assertRaises(Forbidden):
            self.s.audit_trail("approver")
        with self.assertRaises(NotFound):
            self.s.status_at("ghost", "hj-r1")

    def test_unknown_user_bootstrap_rules(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.s.create_user("designer", "u", "u", "wizard")


if __name__ == "__main__":
    unittest.main()

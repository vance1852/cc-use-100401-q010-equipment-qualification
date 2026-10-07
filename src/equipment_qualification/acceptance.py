"""装备资格与适用边界管理的离线验收入口。

故事线：璇玑测井系统设计版本 A，取得深水测井完整资格；高温高压能力
仅有部分证据，凭偏差处置临时放行；证据重放不重复批准；同一放行键
只能成功一次；试验撤回与部件召回只作废未执行作业；最后按多个时间点
解释适用边界与证据缺口。
"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .clock import FrozenClock, isoformat
from .errors import Conflict, InvalidState
from .service import QualificationService
from .storage import connect, inspect_schema


def run(workspace: Path) -> dict[str, object]:
    start = datetime(2026, 5, 1, 8, 0, tzinfo=timezone.utc)
    clock = FrozenClock(start)
    with tempfile.TemporaryDirectory(prefix="equipment-qualification-") as temporary:
        database = Path(temporary) / "qualification.sqlite3"
        connection = connect(database)
        try:
            s = QualificationService(connection, clock)
            # 用户体系
            s.create_user("bootstrap", "designer-1", "设计负责人", "designer")
            s.create_user("designer-1", "tester-1", "试验工程师", "test_engineer")
            s.create_user("designer-1", "qe-1", "资格工程师", "qualification_engineer")
            s.create_user("designer-1", "approver-1", "放行人", "approver")
            s.create_user("designer-1", "auditor-1", "审计人员", "auditor")

            # 装备型号与设计版本
            s.register_model("designer-1", "xuanji", "璇玑旋转导向测井系统")
            design_a = {
                "drawing": "XJ-DWG-2026-A", "sections": ["电子仓", "液压仓", "探头总成"],
                "pressure_rating_mpa": 140,
            }
            s.register_design_revision("designer-1", "xuanji-rA", "xuanji", "rev-A", design_a)

            # 部件批次与装机构成
            seal_content = {"material": "HNBR-90", "cure_cycle": "2026-03", "rated_mpa": 140}
            s.register_component_batch("designer-1", "seal-batch-1", "SEAL-9001", "b2026.03", seal_content)
            s.attach_component("designer-1", "xuanji-rA", "seal-batch-1")

            # 试验协议
            protocol = {
                "test_protocol_id": "TP-DEEPWATER-LOGGING", "version": 3,
                "title": "深水测井环境鉴定试验程序", "capability": "deepwater_logging",
                "steps": [
                    {"name": "hydrostatic", "hold_mpa": 105, "hold_minutes": 120},
                    {"name": "thermal_cycle", "low_c": -2, "high_c": 80, "cycles": 12},
                ],
            }
            s.publish_test_protocol("tester-1", protocol)

            # 四类证据（深水测井能力完整链）
            ev_test = {
                "evidence_id": "ev-test-dw", "evidence_kind": "test_report",
                "title": "深水鉴定试验报告 rev-A", "content": {"samples": 3, "result": "pass"},
                "capability": "deepwater_logging",
                "test_protocol_id": "TP-DEEPWATER-LOGGING", "test_protocol_version": 3,
                "design_revision_id": "xuanji-rA", "component_batch_id": "seal-batch-1",
            }
            s.submit_evidence("tester-1", **ev_test)
            # 相同证据重放：返回 replayed，不新增审计/依据
            replay = s.submit_evidence("tester-1", evidence_id="ev-test-dw-dup-id",
                                       evidence_kind="test_report", title="重复提交",
                                       content={"samples": 3, "result": "pass"},
                                       capability="deepwater_logging",
                                       test_protocol_id="TP-DEEPWATER-LOGGING",
                                       test_protocol_version=3)
            assert replay["replayed"] is True and replay["evidence_id"] == "ev-test-dw"

            s.submit_evidence("tester-1", "ev-cal-dw", "calibration", "压力探头校准证书",
                              {"points": 11, "max_error_pct": 0.3},
                              capability="deepwater_logging",
                              design_revision_id="xuanji-rA",
                              calibration_instrument="dead-weight-tester-DWT-7")
            s.submit_evidence("tester-1", "ev-sw-dw", "software", "随钻测井固件认证",
                              {"build": "4107", "checksum": "deadbeef"},
                              capability="deepwater_logging",
                              software_name="xuanji-fw", software_version="4.1.7")
            s.submit_evidence("qe-1", "ev-dc-rA", "design_conformance", "rev-A 设计符合性声明",
                              {"drawing": "XJ-DWG-2026-A", "conforms": True},
                              design_revision_id="xuanji-rA")

            # 深水完整资格：0-1500m / -2~80℃ / 0-105MPa，测井+取样阶段
            envelope_dw = {
                "depth": {"depth_min": 0, "depth_max": 1500, "depth_unit": "m"},
                "temperature": {"temperature_min": -2, "temperature_max": 80, "temperature_unit": "degC"},
                "pressure": {"pressure_min": 0, "pressure_max": 105, "pressure_unit": "MPa"},
                "phases": ["descent", "logging", "sampling", "ascent"],
            }
            qual_dw = s.grant_qualification(
                "qe-1", "qual-dw-1", "xuanji-rA", "deepwater_logging", envelope_dw,
                ["ev-test-dw", "ev-cal-dw", "ev-sw-dw", "ev-dc-rA"])
            assert not qual_dw["partial"]

            # 高温高压能力：只有试验+校准 → 部分通过
            s.submit_evidence("tester-1", "ev-test-hp", "test_report", "高温高压摸底试验报告",
                              {"samples": 1, "result": "pass"}, capability="hpht_logging",
                              test_protocol_id="TP-DEEPWATER-LOGGING", test_protocol_version=3)
            envelope_hp = {
                "depth": {"depth_min": 0, "depth_max": 3000, "depth_unit": "m"},
                "temperature": {"temperature_min": 80, "temperature_max": 175, "temperature_unit": "degC"},
                "pressure": {"pressure_min": 0, "pressure_max": 140, "pressure_unit": "MPa"},
                "phases": ["logging"],
            }
            qual_hp = s.grant_qualification(
                "qe-1", "qual-hp-1", "xuanji-rA", "hpht_logging", envelope_hp,
                ["ev-test-hp", "ev-cal-dw"])
            assert qual_hp["partial"]
            assert qual_hp["missing_evidence_kinds"] == ["software", "design_conformance"]

            clock.advance(days=10)
            # 部分通过直接放行应被拒绝
            blocked = None
            try:
                s.release_operation(
                    "approver-1", "key-hp-blocked", "XJ-SN-0007", "xuanji-rA", "hpht_logging",
                    2200, 150, 120, "logging", software_version="4.1.7")
            except InvalidState as exc:
                blocked = str(exc)
            assert blocked and "证据不完整" in blocked

            # 偏差处置：限定井次接受 → 放行成功
            clock.advance(days=1)
            s.record_deviation(
                "qe-1", "dev-hp-1", "xuanji-rA", "accepted",
                "高温高压软件认证在途，限本井次使用并加密地面回放校验",
                isoformat(clock.current + timedelta(days=30)),
                capability="hpht_logging", restriction={"well": "LH-HPHT-9", "ground_replay": True})
            release_hp = s.release_operation(
                "approver-1", "key-hp-1", "XJ-SN-0007", "xuanji-rA", "hpht_logging",
                2200, 150, 120, "logging", software_version="4.1.7")

            # 深水：同一放行键重放返回同一放行；换井况复用键冲突
            release_dw = s.release_operation(
                "approver-1", "key-dw-1", "XJ-SN-0001", "xuanji-rA", "deepwater_logging",
                1200, 60, 88, "logging", software_version="4.1.7")
            replay_release = s.release_operation(
                "approver-1", "key-dw-1", "XJ-SN-0001", "xuanji-rA", "deepwater_logging",
                1200, 60, 88, "logging", software_version="4.1.7")
            assert replay_release["release_id"] == release_dw["release_id"]
            assert replay_release.get("replayed") is True
            try:
                s.release_operation(
                    "approver-1", "key-dw-1", "XJ-SN-0002", "xuanji-rA", "deepwater_logging",
                    1300, 60, 88, "logging", software_version="4.1.7")
            except Conflict:
                pass
            else:
                raise AssertionError("同一放行键不同请求必须冲突")

            # 软件版本不在批准范围 → 拒绝
            try:
                s.release_operation(
                    "approver-1", "key-dw-badsw", "XJ-SN-0001", "xuanji-rA", "deepwater_logging",
                    1200, 60, 88, "logging", software_version="9.9.9")
            except InvalidState as exc:
                assert "软件版本不在批准范围" in str(exc)
            else:
                raise AssertionError("未批准软件版本必须被拒绝")

            # 第二口深水井的放行，之后撤回试验 → 应被作废；第一口已完成 → 保留
            release_dw_2 = s.release_operation(
                "approver-1", "key-dw-2", "XJ-SN-0002", "xuanji-rA", "deepwater_logging",
                1450, 70, 100, "sampling", software_version="4.1.7")
            clock.advance(days=2)
            s.complete_operation("approver-1", release_dw["release_id"])

            clock.advance(days=1)
            # 部件召回：seal-batch-1 召回，未执行的 key-dw-2 作废
            recall = s.recall_component_batch("qe-1", "seal-batch-1", "批次密封件老化加速试验不合格")
            assert release_dw_2["release_id"] in recall["voided_releases"]

            # 已完成放行仍可读取，依据快照保留
            kept = s.get_release(release_dw["release_id"])
            assert kept["status"] == "consumed"
            assert len(kept["basis"]["evidence"]) == 4
            assert kept["basis"]["qualification_id"] == "qual-dw-1"

            # 召回后新的深水放行必须被拒绝
            try:
                s.release_operation(
                    "approver-1", "key-dw-3", "XJ-SN-0003", "xuanji-rA", "deepwater_logging",
                    900, 40, 60, "logging", software_version="4.1.7")
            except InvalidState as exc:
                assert "部件批次已召回" in str(exc)
            else:
                raise AssertionError("召回后放行必须被拒绝")

            # 时间旅行解释
            before_recall = isoformat(start + timedelta(days=13))
            status_before = s.status_at(
                "auditor-1", "xuanji-rA", before_recall,
                depth_m=1450, temperature_c=70,
                pressure_mpa=100, phase="sampling")
            by_cap = {c["capability"]: c for c in status_before["capabilities"]}
            assert by_cap["deepwater_logging"]["state"] == "qualified"
            assert by_cap["deepwater_logging"]["usable_at_point"] is True
            assert by_cap["hpht_logging"]["state"] == "partially_qualified"
            assert {m["kind"] for m in by_cap["hpht_logging"]["missing_evidence"]} == {"software", "design_conformance"}

            after_recall = isoformat(clock.current)
            status_after = s.status_at(
                "auditor-1", "xuanji-rA", after_recall,
                depth_m=1450, temperature_c=70,
                pressure_mpa=100, phase="sampling")
            by_cap_after = {c["capability"]: c for c in status_after["capabilities"]}
            assert by_cap_after["deepwater_logging"]["state"] == "component_recalled"
            assert by_cap_after["deepwater_logging"]["usable_at_point"] is False
            # 召回当时：已完成作业保持 consumed，未执行的 key-dw-2 已 voided
            release_states = {r["release_id"]: r["status_as_of"] for r in status_after["releases"]}
            assert release_states[release_dw["release_id"]] == "consumed"
            assert release_states[release_dw_2["release_id"]] == "voided"

            # 撤回深水校准证据（召回已作废 key-dw-2；撤回同时影响仍未执行的高温高压放行）
            clock.advance(days=1)
            withdrawal = s.withdraw_evidence("qe-1", "ev-cal-dw", "复测发现超差，原证书撤回")
            assert release_hp["release_id"] in withdrawal["voided_releases"]
            status_final = s.status_at("auditor-1", "xuanji-rA", isoformat(clock.current))
            states_final = {c["capability"]: c for c in status_final["capabilities"]}
            dw_final = states_final["deepwater_logging"]
            assert dw_final["state"] == "evidence_withdrawn"
            assert [m["kind"] for m in dw_final["missing_evidence"]] == ["calibration"]
            assert dw_final["withdrawn_basis"][0]["evidence_id"] == "ev-cal-dw"
            # 高温高压资格的校准依据同样失效：状态升级为证据撤回，缺口扩大
            hp_final = states_final["hpht_logging"]
            assert hp_final["state"] == "evidence_withdrawn"
            assert {m["kind"] for m in hp_final["missing_evidence"]} == {
                "calibration", "software", "design_conformance"}

            events = s.audit_trail("auditor-1")
            schema = inspect_schema(connection)
        finally:
            connection.close()

    if schema["missing_tables"]:
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "equipment": "xuanji rev-A",
        "qualifications": {"deepwater_logging": "qualified", "hpht_logging": "partially_qualified"},
        "evidence_replay": replay["evidence_id"],
        "releases": {
            "deepwater_1": "consumed",
            "deepwater_2": "voided_by_recall",
            "hpht_deviation": "voided_by_withdrawal",
        },
        "recall_voided": recall["voided_releases"],
        "withdrawal_voided": withdrawal["voided_releases"],
        "final_state": {c["capability"]: c["state"] for c in status_final["capabilities"]},
        "missing_evidence_final": {
            c["capability"]: [m["kind"] for m in c["missing_evidence"]]
            for c in status_final["capabilities"]
        },
        "event_count": len(events),
        "schema": schema,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="执行装备资格与适用边界管理的离线自检")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    result = run(args.workspace.resolve())
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

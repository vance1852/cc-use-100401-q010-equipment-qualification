"""装备资格与适用边界管理的离线验收入口。"""

from __future__ import annotations

import argparse
import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .jsonio import load_json
from .service import QualificationService
from .storage import connect, inspect_schema


CAPABILITY_ENVELOPES = {
    "water_depth": {"max_water_depth_m": "3000"},
    "temperature": {"min_temperature_c": "-20", "max_temperature_c": "150"},
    "pressure": {"max_pressure_mpa": "140"},
    "phase": {"phases": ["drilling", "logging"]},
    "environment": {"environments": ["normal", "hthp"]},
}


def run(workspace: Path) -> dict[str, object]:
    fixtures = workspace / "fixtures"
    protocol = load_json(fixtures / "qualification_protocol.json")
    evidence = load_json(fixtures / "qualification_evidence.json")
    clock = FrozenClock(datetime(2026, 10, 1, 8, 0, tzinfo=timezone.utc))
    with tempfile.TemporaryDirectory(prefix="equipment-qualification-") as temporary:
        database = Path(temporary) / "qualification.sqlite3"
        connection = connect(database)
        try:
            service = QualificationService(connection, clock)
            service.create_user("eng-1", "装备工程师", "equipment_engineer")
            service.create_user("test-1", "试验工程师", "test_engineer")
            service.create_user("appr-1", "资格审批人", "approver")
            service.create_user("ops-1", "作业计划员", "operations")
            service.create_user("aud-1", "审计人员", "auditor")
            service.register_design_version("eng-1", "xuanji-dv-4", "璇玑", "4.2.0", "d" * 64)
            service.register_software_version("eng-1", "xuanji-sw-2", "璇玑", "2.5.1", "5" * 64)
            service.register_component_batch("eng-1", "cb-seal-2026a", "高压密封组件", "2026-A", "宝鸡石油机械")
            service.register_component_batch("eng-1", "cb-telemetry-2026b", "遥测模块", "2026-B", "中海油服电子")
            service.register_equipment("eng-1", "xuanji-001", "璇玑旋转导向系统", "璇玑", "XJ-2026-001")
            service.configure_equipment("eng-1", "xuanji-001", "xuanji-dv-4", "xuanji-sw-2", 0)
            service.install_component("eng-1", "xuanji-001", "cb-seal-2026a")
            service.install_component("eng-1", "xuanji-001", "cb-telemetry-2026b")
            service.publish_protocol("test-1", protocol)
            calibration = service.submit_calibration("test-1", {
                "equipment_id": "xuanji-001",
                "instrument": "石英压力计-QG-11",
                "certificate_no": "CAL-2026-0188",
                "calibrated_at": "2026-05-01T00:00:00Z",
                "valid_until": "2027-05-01T00:00:00Z",
            })
            submitted = service.submit_evidence("test-1", evidence)
            replayed = service.submit_evidence("test-1", evidence)
            if not replayed["replayed"] or replayed["evidence_id"] != submitted["evidence_id"]:
                raise RuntimeError("相同证据重放产生了重复记录")
            grants = {}
            for capability, envelope in CAPABILITY_ENVELOPES.items():
                grants[capability] = service.grant_qualification(
                    "appr-1", "xuanji-001", capability, envelope,
                    [submitted["evidence_id"]], [calibration["calibration_id"]],
                )
            service.plan_operation(
                "ops-1", "op-lh29-a1", "LH29-1-A1", "drilling", "hthp",
                "2800", "145", "130", "2026-11-01T00:00:00Z",
            )
            clock.advance(hours=1)
            first_release = service.request_release("ops-1", "op-lh29-a1", "xuanji-001")
            service.complete_operation("ops-1", "op-lh29-a1")
            clock.advance(hours=1)
            service.plan_operation(
                "ops-1", "op-lh29-a2", "LH29-1-A2", "drilling", "hthp",
                "2900", "148", "135", "2026-12-01T00:00:00Z",
            )
            second_release = service.request_release("ops-1", "op-lh29-a2", "xuanji-001")
            qualified_before = service.explain_equipment("aud-1", "xuanji-001")
            clock.advance(hours=1)
            withdrawn = service.withdraw_evidence("test-1", submitted["evidence_id"], "试验曲线复核发现超差")
            if withdrawn["invalidated_releases"] != [second_release["release_id"]]:
                raise RuntimeError("试验撤回没有精确作用于尚未执行的作业")
            retained = service.get_release("aud-1", first_release["release_id"])
            if retained["state"] != "executed" or not retained["basis"]["evidence"]:
                raise RuntimeError("已完成作业没有保留当时依据")
            before_withdrawal = service.explain_equipment(
                "aud-1", "xuanji-001", as_of=qualified_before["as_of"]
            )
            if not before_withdrawal["capabilities"]["pressure"]["qualified"]:
                raise RuntimeError("按撤回前时间解释应仍然合格")
            after_withdrawal = service.explain_equipment("aud-1", "xuanji-001")
            missing = after_withdrawal["capabilities"]["pressure"]["missing"]
            if after_withdrawal["capabilities"]["pressure"]["qualified"] or not missing:
                raise RuntimeError("撤回后应报告缺口")
            schema = inspect_schema(connection)
        finally:
            connection.close()
    if schema["missing_tables"] or schema["schema_version"] != "1":
        raise RuntimeError("SQLite 基础结构检查失败")
    return {
        "status": "ok",
        "equipment": "xuanji-001",
        "protocol": f"{protocol['protocol_id']}@{protocol['version']}",
        "evidence_id": submitted["evidence_id"],
        "grant_count": len(grants),
        "executed_release": first_release["release_id"],
        "invalidated_release": second_release["release_id"],
        "missing_after_withdrawal": missing,
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

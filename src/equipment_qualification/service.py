"""装备资格与适用边界管理的领域用例。

核心规则：

* 资格按 *设计版本 × 能力* 授予，携带环境包络与依据证据，可部分通过；
* 证据内容寻址，相同内容重放直接返回原证据，不产生新的批准链；
* 作业放行携带 release_key 幂等键，且一份放行只能成功签发一次；
* 试验撤回与部件召回只作废尚未执行（status=released）的放行，
  已完成作业的放行行及其依据快照原样保留；
* status_at(as_of) 支持按任意时间解释装备当时的适用边界与证据缺口。
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from typing import Any, Mapping, Sequence

from .clock import SystemClock, isoformat, parse_at
from .envelope import OPERATION_PHASES, Envelope
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest, digest_value
from .storage import initialize, transaction


ROLE_PERMISSIONS: dict[str, set[str]] = {
    "designer": {"catalog.write"},
    "test_engineer": {"protocol.publish", "evidence.submit"},
    "qualification_engineer": {"evidence.submit", "qualification.write", "deviation.write"},
    "approver": {"release.write"},
    "auditor": {"status.read", "audit.read"},
}

# 每个能力完整资格所需的证据种类；缺项时资格属于“部分通过”。
REQUIRED_EVIDENCE_KINDS = ("test_report", "calibration", "software", "design_conformance")

EVIDENCE_KIND_LABELS = {
    "test_report": "试验报告",
    "calibration": "校准证据",
    "software": "软件版本认证",
    "design_conformance": "设计符合性声明",
}


class QualificationService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    # ------------------------------------------------------------------ 用户

    def _now(self) -> str:
        return isoformat(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT user_id, display_name, role, active FROM users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在: {user_id}")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self, entity_type: str, entity_id: str, event_type: str, actor_id: str, payload: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (entity_type, entity_id, event_type, actor_id, canonical_json(payload), self._now()),
        )

    def create_user(self, actor_id: str, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        # 首个用户允许自举；之后只有既有的资格管理员体系隐含 admin 语义时拒绝。
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        existing = self.connection.execute("SELECT count(*) FROM users").fetchone()[0]
        if existing:
            self._require(actor_id, "catalog.write")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
                if existing:
                    self._audit("user", user_id, "user.created", actor_id, {"role": role})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------ 基础目录

    def register_model(self, actor_id: str, model_id: str, model_name: str) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if not model_id.strip() or not model_name.strip():
            raise ValidationFailed("装备型号编号与名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO equipment_models(model_id,model_name,created_at) VALUES(?,?,?)",
                    (model_id, model_name, self._now()),
                )
                self._audit("model", model_id, "model.registered", actor_id, {"model_name": model_name})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"装备型号已存在: {model_id}") from exc
        return {"model_id": model_id, "model_name": model_name}

    def register_design_revision(
        self, actor_id: str, design_revision_id: str, model_id: str, design_version: str, content: Mapping[str, Any]
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        self._require_model(model_id)
        sha = digest_value(content)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO design_revisions(design_revision_id,model_id,design_version,content_sha256,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (design_revision_id, model_id, design_version, sha, actor_id, self._now()),
                )
                self._audit("design_revision", design_revision_id, "design.registered", actor_id,
                            {"model_id": model_id, "design_version": design_version, "content_sha256": sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("设计版本编号、型号内版本号或内容摘要冲突") from exc
        return {"design_revision_id": design_revision_id, "model_id": model_id,
                "design_version": design_version, "content_sha256": sha}

    def register_component_batch(
        self,
        actor_id: str,
        component_batch_id: str,
        part_number: str,
        batch_version: str,
        content: Mapping[str, Any],
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        sha = digest_value(content)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO component_batches(component_batch_id,part_number,batch_version,content_sha256,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?)",
                    (component_batch_id, part_number, batch_version, sha, actor_id, self._now()),
                )
                self._audit("component_batch", component_batch_id, "component.registered", actor_id,
                            {"part_number": part_number, "batch_version": batch_version, "content_sha256": sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("部件批次编号或件号-版本组合冲突") from exc
        return {"component_batch_id": component_batch_id, "part_number": part_number,
                "batch_version": batch_version, "status": "active", "content_sha256": sha}

    def attach_component(
        self, actor_id: str, design_revision_id: str, component_batch_id: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        self._require_design(design_revision_id)
        self._require_component(component_batch_id)
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO design_components(design_revision_id,component_batch_id,created_at) VALUES(?,?,?)",
                    (design_revision_id, component_batch_id, self._now()),
                )
                self._audit("design_revision", design_revision_id, "component.attached", actor_id,
                            {"component_batch_id": component_batch_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("该部件批次已挂接到此设计版本") from exc
        return {"design_revision_id": design_revision_id, "component_batch_id": component_batch_id}

    def publish_test_protocol(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "protocol.publish")
        protocol_id = str(raw.get("test_protocol_id", "")).strip()
        title = str(raw.get("title", "")).strip()
        capability = str(raw.get("capability", "")).strip()
        try:
            version = int(raw["version"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValidationFailed("协议版本必须是正整数") from exc
        if not protocol_id or not title or not capability or version <= 0:
            raise ValidationFailed("协议编号、标题、能力与正版本号均不能为空")
        sha = content_digest([raw])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO test_protocols(test_protocol_id,version,title,capability,canonical_json,"
                    "content_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (protocol_id, version, title, capability, canonical_json(raw), sha, self._now()),
                )
                self._audit("test_protocol", f"{protocol_id}@{version}", "protocol.published", actor_id,
                            {"capability": capability, "content_sha256": sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("协议版本或内容摘要已经存在") from exc
        return {"test_protocol_id": protocol_id, "version": version, "capability": capability,
                "content_sha256": sha}

    # ---------------------------------------------------------------- 证据

    def submit_evidence(
        self,
        actor_id: str,
        evidence_id: str,
        evidence_kind: str,
        title: str,
        content: Mapping[str, Any],
        *,
        capability: str | None = None,
        issued_at: str | None = None,
        test_protocol_id: str | None = None,
        test_protocol_version: int | None = None,
        design_revision_id: str | None = None,
        component_batch_id: str | None = None,
        software_name: str | None = None,
        software_version: str | None = None,
        calibration_instrument: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "evidence.submit")
        if evidence_kind not in EVIDENCE_KIND_LABELS:
            raise ValidationFailed(f"未知证据种类: {evidence_kind}")
        if not evidence_id.strip() or not title.strip() or not isinstance(content, Mapping):
            raise ValidationFailed("证据编号、标题与内容体不能为空")
        sha = digest_value(content)
        # 相同证据重放：直接返回既有记录，不再写批准链或审计。
        duplicate = self.connection.execute(
            "SELECT * FROM qualification_evidence WHERE content_sha256=?", (sha,)
        ).fetchone()
        if duplicate is not None:
            return dict(duplicate) | {"replayed": True}

        issued = isoformat(parse_at(issued_at, self.clock.now()))
        if test_protocol_id is not None:
            protocol = self.connection.execute(
                "SELECT capability FROM test_protocols WHERE test_protocol_id=? AND version=?",
                (test_protocol_id, test_protocol_version),
            ).fetchone()
            if protocol is None:
                raise NotFound("试验协议版本不存在")
            capability = capability or protocol["capability"]
        if design_revision_id is not None:
            self._require_design(design_revision_id)
        if component_batch_id is not None:
            self._require_component(component_batch_id)
        if not capability and evidence_kind != "design_conformance":
            raise ValidationFailed("试验/校准/软件证据必须声明能力")
        if evidence_kind == "test_report" and (test_protocol_id is None or test_protocol_version is None):
            raise ValidationFailed("试验报告必须引用已发布的试验协议版本")
        if evidence_kind == "calibration" and not calibration_instrument:
            raise ValidationFailed("校准证据必须记录校准仪器")
        if evidence_kind == "software" and (not software_name or not software_version):
            raise ValidationFailed("软件证据必须记录软件名称与版本")
        if evidence_kind == "design_conformance" and not design_revision_id:
            raise ValidationFailed("设计符合性证据必须指向设计版本")

        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO qualification_evidence(evidence_id,evidence_kind,title,content_sha256,"
                    "canonical_json,capability,test_protocol_id,test_protocol_version,design_revision_id,"
                    "component_batch_id,software_name,software_version,calibration_instrument,status,"
                    "issued_at,submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (evidence_id, evidence_kind, title, sha, canonical_json(content), capability,
                     test_protocol_id, test_protocol_version, design_revision_id, component_batch_id,
                     software_name, software_version, calibration_instrument, "active",
                     issued, actor_id, self._now()),
                )
                self._audit("evidence", evidence_id, "evidence.submitted", actor_id,
                            {"evidence_kind": evidence_kind, "capability": capability,
                             "content_sha256": sha})
        except sqlite3.IntegrityError as exc:
            raise Conflict("证据编号或内容摘要冲突（并发重放）") from exc
        row = self.connection.execute(
            "SELECT * FROM qualification_evidence WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        return dict(row)

    def withdraw_evidence(self, actor_id: str, evidence_id: str, reason: str) -> dict[str, Any]:
        """试验撤回：证据失效，作废所有尚未执行的相关放行；历史保留。"""

        self._require(actor_id, "qualification.write")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        evidence = self.connection.execute(
            "SELECT * FROM qualification_evidence WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        if evidence is None:
            raise NotFound("证据不存在")
        if evidence["status"] != "active":
            raise InvalidState("证据已经撤回")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE qualification_evidence SET status='withdrawn',withdrawn_at=?,withdraw_reason=? "
                "WHERE evidence_id=? AND status='active'",
                (now, reason, evidence_id),
            )
            voided = self._void_open_releases(
                now,
                reason,
                "SELECT release_id FROM operation_releases WHERE status='released' AND qualification_id IN "
                "(SELECT qualification_id FROM qualification_basis WHERE evidence_id=?)",
                (evidence_id,),
                actor_id,
                "evidence",
                evidence_id,
            )
            self._audit("evidence", evidence_id, "evidence.withdrawn", actor_id,
                        {"reason": reason, "voided_releases": voided})
        return {"evidence_id": evidence_id, "status": "withdrawn", "voided_releases": voided}

    # ---------------------------------------------------------------- 资格

    def grant_qualification(
        self,
        actor_id: str,
        qualification_id: str,
        design_revision_id: str,
        capability: str,
        envelope: Mapping[str, Any],
        evidence_ids: Sequence[str],
        *,
        valid_from: str | None = None,
        valid_to: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "qualification.write")
        self._require_design(design_revision_id)
        if not capability.strip():
            raise ValidationFailed("能力不能为空")
        try:
            parsed_envelope = Envelope.from_dict(envelope)
        except (KeyError, ValueError) as exc:
            raise ValidationFailed(f"环境包络无效: {exc}") from exc
        if not evidence_ids:
            raise ValidationFailed("资格至少需要一条依据证据")
        start = isoformat(parse_at(valid_from, self.clock.now()))
        end = isoformat(parse_at(valid_to)) if valid_to else None
        if end is not None and end <= start:
            raise ValidationFailed("资格失效时间必须晚于生效时间")

        evidence_rows = []
        for evidence_id in dict.fromkeys(evidence_ids):  # 去重但保序
            row = self.connection.execute(
                "SELECT * FROM qualification_evidence WHERE evidence_id=?", (evidence_id,)
            ).fetchone()
            if row is None:
                raise NotFound(f"证据不存在: {evidence_id}")
            if row["status"] != "active":
                raise InvalidState(f"证据已撤回，不能作为批准依据: {evidence_id}")
            if row["evidence_kind"] == "design_conformance" and row["design_revision_id"] != design_revision_id:
                raise ValidationFailed(
                    f"设计符合性证据 {evidence_id} 属于其他设计版本，不能作为本资格依据")
            linked_design = row["design_revision_id"]
            if row["evidence_kind"] in {"test_report", "calibration"} and linked_design is not None \
                    and linked_design != design_revision_id:
                raise ValidationFailed(
                    f"证据 {evidence_id} 指向其他设计版本，不能作为本资格依据")
            evidence_rows.append(row)

        missing_kinds = [k for k in REQUIRED_EVIDENCE_KINDS
                         if not any(r["evidence_kind"] == k for r in evidence_rows)]
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO qualifications(qualification_id,design_revision_id,capability,envelope_json,"
                    "status,valid_from,valid_to,created_by,created_at) VALUES(?,?,?,?, 'qualified',?,?,?,?)",
                    (qualification_id, design_revision_id, capability,
                     canonical_json(parsed_envelope.as_dict()), start, end, actor_id, self._now()),
                )
                for row in evidence_rows:
                    self.connection.execute(
                        "INSERT INTO qualification_basis(qualification_id,evidence_id,evidence_sha256) "
                        "VALUES(?,?,?)",
                        (qualification_id, row["evidence_id"], row["content_sha256"]),
                    )
                self._audit("qualification", qualification_id, "qualification.granted", actor_id,
                            {"design_revision_id": design_revision_id, "capability": capability,
                             "envelope": parsed_envelope.as_dict(),
                             "evidence_ids": [r["evidence_id"] for r in evidence_rows],
                             "missing_evidence_kinds": missing_kinds})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"资格编号冲突: {qualification_id}") from exc
        result = self.get_qualification(qualification_id)
        result["missing_evidence_kinds"] = missing_kinds
        result["partial"] = bool(missing_kinds)
        return result

    def withdraw_qualification(self, actor_id: str, qualification_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "qualification.write")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        row = self.connection.execute(
            "SELECT * FROM qualifications WHERE qualification_id=?", (qualification_id,)
        ).fetchone()
        if row is None:
            raise NotFound("资格不存在")
        if row["status"] != "qualified":
            raise InvalidState("资格不是有效状态")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE qualifications SET status='withdrawn',withdrawn_at=?,withdraw_reason=? "
                "WHERE qualification_id=? AND status='qualified'",
                (now, reason, qualification_id),
            )
            voided = self._void_open_releases(
                now, reason,
                "SELECT release_id FROM operation_releases WHERE status='released' AND qualification_id=?",
                (qualification_id,), actor_id, "qualification", qualification_id,
            )
            self._audit("qualification", qualification_id, "qualification.withdrawn", actor_id,
                        {"reason": reason, "voided_releases": voided})
        return {"qualification_id": qualification_id, "status": "withdrawn", "voided_releases": voided}

    def supersede_qualification(
        self, actor_id: str, qualification_id: str, successor_qualification_id: str
    ) -> dict[str, Any]:
        self._require(actor_id, "qualification.write")
        old = self._require_qualification_row(qualification_id)
        successor = self._require_qualification_row(successor_qualification_id)
        if old["capability"] != successor["capability"]:
            raise ValidationFailed("只能由同一能力的新资格取代")
        if old["design_revision_id"] == successor["design_revision_id"]:
            raise ValidationFailed("取代资格必须属于另一个设计版本")
        if old["status"] != "qualified":
            raise InvalidState("只有有效资格可以被取代")
        if successor["status"] != "qualified":
            raise InvalidState("继任资格不是有效状态，不能取代其他资格")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE qualifications SET status='superseded',superseded_by=? WHERE qualification_id=?",
                (successor_qualification_id, qualification_id),
            )
            self._audit("qualification", qualification_id, "qualification.superseded", actor_id,
                        {"successor_qualification_id": successor_qualification_id})
        return {"qualification_id": qualification_id, "status": "superseded",
                "successor_qualification_id": successor_qualification_id}

    def get_qualification(self, qualification_id: str) -> dict[str, Any]:
        row = self._require_qualification_row(qualification_id)
        result = dict(row)
        result["envelope"] = json.loads(row["envelope_json"])
        basis = self.connection.execute(
            "SELECT b.evidence_id,b.evidence_sha256,e.evidence_kind,e.status,e.capability,"
            "e.software_name,e.software_version,e.component_batch_id,e.test_protocol_id,e.test_protocol_version "
            "FROM qualification_basis b JOIN qualification_evidence e ON e.evidence_id=b.evidence_id "
            "WHERE b.qualification_id=? ORDER BY b.evidence_id",
            (qualification_id,),
        ).fetchall()
        result["basis"] = [dict(r) for r in basis]
        return result

    # ---------------------------------------------------------------- 偏差

    def record_deviation(
        self,
        actor_id: str,
        deviation_id: str,
        design_revision_id: str,
        decision: str,
        justification: str,
        valid_to: str,
        *,
        capability: str | None = None,
        restriction: Mapping[str, Any] | None = None,
        valid_from: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "deviation.write")
        self._require_design(design_revision_id)
        if decision not in {"accepted", "rejected"} or not justification.strip():
            raise ValidationFailed("偏差决定（accepted/rejected）与理由不能为空")
        start = isoformat(parse_at(valid_from, self.clock.now()))
        end = isoformat(parse_at(valid_to))
        if end <= start:
            raise ValidationFailed("偏差有效期结束时间必须晚于开始时间")
        if restriction is not None and not isinstance(restriction, Mapping):
            raise ValidationFailed("偏差限制必须是对象")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO deviations(deviation_id,design_revision_id,capability,decision,"
                    "restriction_json,justification,valid_from,valid_to,status,requested_by,reviewed_by,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?, 'open',?,?,?)",
                    (deviation_id, design_revision_id, capability, decision,
                     canonical_json(restriction) if restriction is not None else None,
                     justification, start, end, actor_id, actor_id, self._now()),
                )
                self._audit("deviation", deviation_id, "deviation.recorded", actor_id,
                            {"design_revision_id": design_revision_id, "capability": capability,
                             "decision": decision, "valid_to": end})
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"偏差编号冲突: {deviation_id}") from exc
        return self.get_deviation(deviation_id)

    def close_deviation(self, actor_id: str, deviation_id: str) -> dict[str, Any]:
        self._require(actor_id, "deviation.write")
        row = self.connection.execute(
            "SELECT * FROM deviations WHERE deviation_id=?", (deviation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("偏差不存在")
        if row["status"] != "open":
            raise InvalidState("偏差已经关闭")
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE deviations SET status='closed',closed_at=? WHERE deviation_id=? AND status='open'",
                (self._now(), deviation_id),
            )
            self._audit("deviation", deviation_id, "deviation.closed", actor_id, {})
        return self.get_deviation(deviation_id)

    def get_deviation(self, deviation_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM deviations WHERE deviation_id=?", (deviation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("偏差不存在")
        result = dict(row)
        result["restriction"] = json.loads(row["restriction_json"]) if row["restriction_json"] else None
        return result

    # ---------------------------------------------------------------- 召回

    def recall_component_batch(self, actor_id: str, component_batch_id: str, reason: str) -> dict[str, Any]:
        """部件召回：批次失效并作废料未执行的放行；已完成数据保留。"""

        self._require(actor_id, "qualification.write")
        if not reason.strip():
            raise ValidationFailed("召回原因不能为空")
        batch = self._require_component(component_batch_id)
        if batch["status"] != "active":
            raise InvalidState("部件批次已经召回")
        now = self._now()
        with transaction(self.connection, immediate=True):
            self.connection.execute(
                "UPDATE component_batches SET status='recalled',recalled_at=?,recall_reason=? "
                "WHERE component_batch_id=? AND status='active'",
                (now, reason, component_batch_id),
            )
            select_sql = (
                "SELECT r.release_id FROM operation_releases r WHERE r.status='released' AND ("
                "r.component_batch_id=? OR r.qualification_id IN ("
                "SELECT b.qualification_id FROM qualification_basis b "
                "JOIN qualification_evidence e ON e.evidence_id=b.evidence_id "
                "WHERE e.component_batch_id=?))"
            )
            voided = self._void_open_releases(
                now, reason, select_sql, (component_batch_id, component_batch_id),
                actor_id, "component_batch", component_batch_id,
            )
            self._audit("component_batch", component_batch_id, "component.recalled", actor_id,
                        {"reason": reason, "voided_releases": voided})
        return {"component_batch_id": component_batch_id, "status": "recalled",
                "voided_releases": voided}

    # ---------------------------------------------------------------- 放行

    def release_operation(
        self,
        actor_id: str,
        release_key: str,
        equipment_serial: str,
        design_revision_id: str,
        capability: str,
        depth_m: float,
        temperature_c: float,
        pressure_mpa: float,
        phase: str,
        *,
        component_batch_id: str | None = None,
        software_version: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "release.write")
        if not release_key.strip() or not equipment_serial.strip():
            raise ValidationFailed("放行键与装备序列号不能为空")
        for name, value in (("depth_m", depth_m), ("temperature_c", temperature_c),
                            ("pressure_mpa", pressure_mpa)):
            if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
                raise ValidationFailed(f"{name} 必须是有限数值")
        if phase not in OPERATION_PHASES:
            raise ValidationFailed(f"未知作业阶段: {phase}")
        self._require_design(design_revision_id)
        if component_batch_id is not None:
            batch = self._require_component(component_batch_id)
            if batch["status"] != "active":
                raise InvalidState(f"部件批次已召回: {component_batch_id}")

        request_digest = digest_value({
            "equipment_serial": equipment_serial,
            "design_revision_id": design_revision_id,
            "capability": capability,
            "depth_m": float(depth_m),
            "temperature_c": float(temperature_c),
            "pressure_mpa": float(pressure_mpa),
            "phase": phase,
            "component_batch_id": component_batch_id,
            "software_version": software_version,
        })

        with transaction(self.connection, immediate=True):
            # 幂等：同一放行键只能成功一次；重放原样返回，不同请求冲突。
            replay = self.connection.execute(
                "SELECT release_id FROM release_idempotency WHERE release_key=?", (release_key,)
            ).fetchone()
            if replay is not None:
                stored = self.connection.execute(
                    "SELECT request_sha256 FROM release_idempotency WHERE release_key=?", (release_key,)
                ).fetchone()
                if stored["request_sha256"] != request_digest:
                    raise Conflict("同一放行键对应了不同的作业请求")
                row = self.connection.execute(
                    "SELECT * FROM operation_releases WHERE release_id=?", (replay["release_id"],)
                ).fetchone()
                return dict(row) | {"replayed": True}

            now = self._now()
            qualification = self.connection.execute(
                "SELECT * FROM qualifications WHERE design_revision_id=? AND capability=? "
                "AND status='qualified' AND valid_from<=? "
                "AND (valid_to IS NULL OR valid_to>?) "
                "ORDER BY valid_from DESC, qualification_id DESC LIMIT 1",
                (design_revision_id, capability, now, now),
            ).fetchone()
            blockers: list[str] = []
            missing_kinds: list[str] = []
            accepted_deviation: sqlite3.Row | None = None
            if qualification is None:
                blockers.append("该设计版本在此能力上没有当前有效的资格")
                envelope_dict = None
                basis_rows = []
            else:
                envelope = Envelope.from_dict(json.loads(qualification["envelope_json"]))
                envelope_dict = envelope.as_dict()
                if not envelope.covers_point(float(depth_m), float(temperature_c),
                                            float(pressure_mpa), phase):
                    blockers.append("作业井况（水深/温压/阶段）超出资格环境包络")
                basis_rows = self.connection.execute(
                    "SELECT e.* FROM qualification_basis b "
                    "JOIN qualification_evidence e ON e.evidence_id=b.evidence_id "
                    "WHERE b.qualification_id=?",
                    (qualification["qualification_id"],),
                ).fetchall()
                for row in basis_rows:
                    if row["status"] != "active":
                        blockers.append(f"依据证据已撤回: {row['evidence_id']}（{EVIDENCE_KIND_LABELS[row['evidence_kind']]}）")
                    if row["component_batch_id"] and self._component_status(row["component_batch_id"]) != "active":
                        blockers.append(f"依据涉及的部件批次已召回: {row['component_batch_id']}")
                present_kinds = {r["evidence_kind"] for r in basis_rows if r["status"] == "active"}
                missing_kinds = [k for k in REQUIRED_EVIDENCE_KINDS if k not in present_kinds]
                software_rows = [r for r in basis_rows
                                 if r["evidence_kind"] == "software" and r["status"] == "active"]
                if software_rows:
                    approved_versions = {r["software_version"] for r in software_rows}
                    if software_version not in approved_versions:
                        blockers.append(
                            f"软件版本不在批准范围: 需要 {sorted(v for v in approved_versions if v)}，"
                            f"收到 {software_version!r}")
                accepted_deviation = self.connection.execute(
                    "SELECT * FROM deviations WHERE design_revision_id=? AND status='open' "
                    "AND decision='accepted' AND (capability IS NULL OR capability=?) "
                    "AND valid_from<=? AND valid_to>? "
                    "ORDER BY valid_from DESC, deviation_id DESC LIMIT 1",
                    (design_revision_id, capability, now, now),
                ).fetchone()

            # 证据缺项属于偏差处置范围时，凭有效 accepted 偏差放行；否则阻断。
            unresolved_missing = list(missing_kinds)
            if missing_kinds and accepted_deviation is not None:
                unresolved_missing = []
            if unresolved_missing:
                blockers.append("证据不完整，缺少: "
                                + "、".join(EVIDENCE_KIND_LABELS[k] for k in unresolved_missing))
            if blockers:
                raise InvalidState("放行被拒绝: " + "；".join(blockers))

            basis_snapshot = {
                "qualification_id": qualification["qualification_id"],
                "capability": capability,
                "envelope": envelope_dict,
                "evidence": [
                    {"evidence_id": r["evidence_id"], "evidence_kind": r["evidence_kind"],
                     "content_sha256": r["content_sha256"], "status": r["status"]}
                    for r in basis_rows
                ],
                "deviation_id": None if accepted_deviation is None else accepted_deviation["deviation_id"],
            }
            qualification_sha = digest_value(basis_snapshot)
            release_id = uuid.uuid4().hex
            self.connection.execute(
                "INSERT INTO operation_releases(release_id,release_key,equipment_serial,design_revision_id,"
                "component_batch_id,capability,depth_m,temperature_c,pressure_mpa,phase,software_version,"
                "qualification_id,qualification_sha256,basis_json,status,requested_by,released_at,"
                "deviation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?, 'released',?,?,?)",
                (release_id, release_key, equipment_serial, design_revision_id, component_batch_id,
                 capability, float(depth_m), float(temperature_c), float(pressure_mpa), phase,
                 software_version, qualification["qualification_id"], qualification_sha,
                 canonical_json(basis_snapshot), actor_id, now,
                 None if accepted_deviation is None else accepted_deviation["deviation_id"]),
            )
            self.connection.execute(
                "INSERT INTO release_idempotency(release_key,request_sha256,release_id,created_at) "
                "VALUES(?,?,?,?)",
                (release_key, request_digest, release_id, now),
            )
            self._audit("release", release_id, "release.granted", actor_id,
                        {"release_key": release_key, "equipment_serial": equipment_serial,
                         "qualification_id": qualification["qualification_id"],
                         "capability": capability, "phase": phase,
                         "deviation_id": basis_snapshot["deviation_id"],
                         "qualification_sha256": qualification_sha})
        row = self.connection.execute(
            "SELECT * FROM operation_releases WHERE release_id=?", (release_id,)
        ).fetchone()
        return dict(row)

    def complete_operation(self, actor_id: str, release_id: str) -> dict[str, Any]:
        """登记作业完成；放行一旦 consumed，后续撤回/召回不能再改变它。"""

        self._require(actor_id, "release.write")
        row = self.connection.execute(
            "SELECT * FROM operation_releases WHERE release_id=?", (release_id,)
        ).fetchone()
        if row is None:
            raise NotFound("放行不存在")
        if row["status"] == "consumed":
            raise InvalidState("作业已经完成，放行不能重复登记")
        if row["status"] == "voided":
            raise InvalidState("放行已作废，不能登记完成")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE operation_releases SET status='consumed',consumed_at=? "
                "WHERE release_id=? AND status='released'",
                (now, release_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("放行状态已变化，完成登记失败")
            self._audit("release", release_id, "operation.completed", actor_id,
                        {"equipment_serial": row["equipment_serial"],
                         "qualification_id": row["qualification_id"],
                         "qualification_sha256": row["qualification_sha256"]})
        return dict(self.connection.execute(
            "SELECT * FROM operation_releases WHERE release_id=?", (release_id,)
        ).fetchone())

    def get_release(self, release_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM operation_releases WHERE release_id=?", (release_id,)
        ).fetchone()
        if row is None:
            raise NotFound("放行不存在")
        result = dict(row)
        result["basis"] = json.loads(row["basis_json"])
        return result

    # ------------------------------------------------------- 任意时间解释

    def status_at(
        self,
        actor_id: str,
        design_revision_id: str,
        at: str | None = None,
        *,
        equipment_serial: str | None = None,
        depth_m: float | None = None,
        temperature_c: float | None = None,
        pressure_mpa: float | None = None,
        phase: str | None = None,
    ) -> dict[str, Any]:
        """按任意时间点解释某设计版本（可限定某台装备）的适用边界与证据缺口。"""

        self._user(actor_id)  # 任何在岗角色都可以解释
        self._require_design(design_revision_id)
        if phase is not None and phase not in OPERATION_PHASES:
            raise ValidationFailed(f"未知作业阶段: {phase}")
        point_supplied = all(v is not None for v in (depth_m, temperature_c, pressure_mpa))
        if (depth_m, temperature_c, pressure_mpa).count(None) not in (0, 3):
            raise ValidationFailed("水深、温度、压力必须同时提供或同时省略")
        moment = isoformat(parse_at(at, self.clock.now()))

        rows = self.connection.execute(
            "SELECT * FROM qualifications WHERE design_revision_id=? AND valid_from<=? "
            "ORDER BY capability, valid_from",
            (design_revision_id, moment),
        ).fetchall()

        capabilities: list[dict[str, Any]] = []
        seen: set[str] = set()
        for row in rows:
            if row["capability"] in seen:
                continue
            # 取该能力在 moment 时最新版本（按授权时间倒序遍历）
            latest = next(
                (r for r in reversed(rows)
                 if r["capability"] == row["capability"]
                 and r["valid_from"] <= moment
                 and (r["valid_to"] is None or r["valid_to"] > moment)),
                None,
            )
            if latest is None:
                continue
            seen.add(row["capability"])
            capabilities.append(self._capability_status(latest, moment, point_supplied,
                                                        depth_m, temperature_c, pressure_mpa, phase))

        # 已挂接部件召回状态也影响整机解释
        recalled_batches = [
            dict(r) for r in self.connection.execute(
                "SELECT c.component_batch_id,c.part_number,c.batch_version,c.recalled_at,c.recall_reason "
                "FROM design_components dc JOIN component_batches c ON c.component_batch_id=dc.component_batch_id "
                "WHERE dc.design_revision_id=? AND c.status='recalled' AND c.recalled_at<=?",
                (design_revision_id, moment),
            ).fetchall()
        ]

        releases = self.connection.execute(
            "SELECT * FROM operation_releases WHERE design_revision_id=? "
            + ("AND equipment_serial=? " if equipment_serial else "")
            + "AND released_at<=? ORDER BY released_at, release_id",
            ((design_revision_id, equipment_serial, moment) if equipment_serial
             else (design_revision_id, moment)),
        ).fetchall()
        release_views = []
        for r in releases:
            status = "released"
            if r["voided_at"] and r["voided_at"] <= moment:
                status = "voided"
            elif r["consumed_at"] and r["consumed_at"] <= moment:
                status = "consumed"
            release_views.append({
                "release_id": r["release_id"],
                "equipment_serial": r["equipment_serial"],
                "capability": r["capability"],
                "phase": r["phase"],
                "well_conditions": {
                    "depth_m": r["depth_m"], "temperature_c": r["temperature_c"],
                    "pressure_mpa": r["pressure_mpa"],
                },
                "software_version": r["software_version"],
                "status_as_of": status,
                "qualification_id": r["qualification_id"],
                "qualification_sha256": r["qualification_sha256"],
                "released_at": r["released_at"],
                "consumed_at": r["consumed_at"],
                "voided_at": r["voided_at"],
                "void_reason": r["void_reason"],
                "basis": json.loads(r["basis_json"]),
            })

        deviations = [
            dict(d) for d in self.connection.execute(
                "SELECT * FROM deviations WHERE design_revision_id=? AND valid_from<=? AND valid_to>? "
                "AND status='open' ORDER BY deviation_id",
                (design_revision_id, moment, moment),
            ).fetchall()
        ]
        for d in deviations:
            d["restriction"] = json.loads(d["restriction_json"]) if d["restriction_json"] else None
            d.pop("restriction_json", None)

        usable = [c["capability"] for c in capabilities if c["usable_at_point"]]
        return {
            "design_revision_id": design_revision_id,
            "equipment_serial": equipment_serial,
            "as_of": moment,
            "well_conditions": None if not point_supplied else {
                "depth_m": float(depth_m), "temperature_c": float(temperature_c),
                "pressure_mpa": float(pressure_mpa), "phase": phase,
            },
            "capabilities": capabilities,
            "usable_capabilities": usable,
            "recalled_component_batches": recalled_batches,
            "open_deviations": deviations,
            "releases": release_views,
        }

    def _capability_status(
        self, qualification: sqlite3.Row, moment: str, point_supplied: bool,
        depth_m: float | None, temperature_c: float | None, pressure_mpa: float | None,
        phase: str | None,
    ) -> dict[str, Any]:
        envelope = Envelope.from_dict(json.loads(qualification["envelope_json"]))
        basis = self.connection.execute(
            "SELECT e.* FROM qualification_basis b JOIN qualification_evidence e "
            "ON e.evidence_id=b.evidence_id WHERE b.qualification_id=? ORDER BY e.evidence_id",
            (qualification["qualification_id"],),
        ).fetchall()

        def active_at(row: sqlite3.Row) -> bool:
            return row["created_at"] <= moment and (
                row["withdrawn_at"] is None or row["withdrawn_at"] > moment)

        active_basis = [r for r in basis if active_at(r)]
        withdrawn_basis = [
            {"evidence_id": r["evidence_id"], "evidence_kind": r["evidence_kind"],
             "withdrawn_at": r["withdrawn_at"], "withdraw_reason": r["withdraw_reason"]}
            for r in basis
            if r["created_at"] <= moment and r["withdrawn_at"] is not None
            and r["withdrawn_at"] <= moment
        ]
        missing_kinds = [k for k in REQUIRED_EVIDENCE_KINDS
                         if not any(r["evidence_kind"] == k for r in active_basis)]
        recalled = sorted({
            r["component_batch_id"] for r in active_basis
            if r["component_batch_id"] and self._component_recalled_at(r["component_batch_id"], moment)
        })

        withdrawn_now = (
            qualification["status"] == "withdrawn"
            and qualification["withdrawn_at"] is not None
            and qualification["withdrawn_at"] <= moment
        )
        if withdrawn_now:
            state = "withdrawn"
        elif withdrawn_basis:
            state = "evidence_withdrawn"
        elif recalled:
            state = "component_recalled"
        elif missing_kinds:
            state = "partially_qualified"
        else:
            state = "qualified"

        within_envelope = None
        if point_supplied:
            within_envelope = envelope.covers_point(
                float(depth_m), float(temperature_c), float(pressure_mpa), phase)
        usable = state in {"qualified", "partially_qualified"} and (
            within_envelope in (None, True))
        # 部分通过只有在存在覆盖该能力的有效 accepted 偏差时才算可用
        if state == "partially_qualified":
            waiver = self.connection.execute(
                "SELECT 1 FROM deviations WHERE design_revision_id=? AND status='open' "
                "AND decision='accepted' AND (capability IS NULL OR capability=?) "
                "AND valid_from<=? AND valid_to>? LIMIT 1",
                (qualification["design_revision_id"], qualification["capability"], moment, moment),
            ).fetchone()
            usable = bool(waiver) and within_envelope in (None, True)

        return {
            "capability": qualification["capability"],
            "qualification_id": qualification["qualification_id"],
            "state": state,
            "qualified_at": qualification["valid_from"],
            "valid_to": qualification["valid_to"],
            "envelope": envelope.as_dict(),
            "basis": [
                {"evidence_id": r["evidence_id"], "evidence_kind": r["evidence_kind"],
                 "content_sha256": r["content_sha256"],
                 "software_name": r["software_name"], "software_version": r["software_version"],
                 "component_batch_id": r["component_batch_id"],
                 "test_protocol_id": r["test_protocol_id"],
                 "test_protocol_version": r["test_protocol_version"]}
                for r in active_basis
            ],
            "withdrawn_basis": withdrawn_basis,
            "missing_evidence": [
                {"kind": kind, "label": EVIDENCE_KIND_LABELS[kind]} for kind in missing_kinds
            ],
            "recalled_component_batches": recalled,
            "within_envelope": within_envelope,
            "usable_at_point": usable,
        }

    def audit_trail(self, actor_id: str, entity_type: str | None = None,
                    entity_id: str | None = None) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        sql = "SELECT * FROM audit_events"
        params: tuple[Any, ...] = ()
        if entity_type and entity_id:
            sql += " WHERE entity_type=? AND entity_id=?"
            params = (entity_type, entity_id)
        elif entity_type:
            sql += " WHERE entity_type=?"
            params = (entity_type,)
        sql += " ORDER BY event_id"
        return [dict(r) | {"payload": json.loads(r["payload_json"])} for r in
                self.connection.execute(sql, params).fetchall()]

    # ------------------------------------------------------------- 内部辅助

    def _void_open_releases(
        self, now: str, reason: str, select_sql: str, params: tuple[Any, ...],
        actor_id: str, entity_type: str, entity_id: str,
    ) -> list[str]:
        """作废查询选出的全部未执行放行，并逐条审计；返回作废的放行编号。"""

        release_ids = [r[0] for r in self.connection.execute(select_sql, params).fetchall()]
        for release_id in release_ids:
            self.connection.execute(
                "UPDATE operation_releases SET status='voided',voided_at=?,void_reason=? "
                "WHERE release_id=? AND status='released'",
                (now, reason, release_id),
            )
            self._audit("release", release_id, "release.voided", actor_id,
                        {"cause_type": entity_type, "cause_id": entity_id, "reason": reason})
        return release_ids

    def _require_model(self, model_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM equipment_models WHERE model_id=?", (model_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"装备型号不存在: {model_id}")
        return row

    def _require_design(self, design_revision_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM design_revisions WHERE design_revision_id=?", (design_revision_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"设计版本不存在: {design_revision_id}")
        return row

    def _require_component(self, component_batch_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM component_batches WHERE component_batch_id=?", (component_batch_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"部件批次不存在: {component_batch_id}")
        return row

    def _component_status(self, component_batch_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT status FROM component_batches WHERE component_batch_id=?", (component_batch_id,)
        ).fetchone()
        return None if row is None else row["status"]

    def _component_recalled_at(self, component_batch_id: str, moment: str) -> bool:
        row = self.connection.execute(
            "SELECT recalled_at FROM component_batches WHERE component_batch_id=?",
            (component_batch_id,),
        ).fetchone()
        return row is not None and row["recalled_at"] is not None and row["recalled_at"] <= moment

    def _require_qualification_row(self, qualification_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM qualifications WHERE qualification_id=?", (qualification_id,)
        ).fetchone()
        if row is None:
            raise NotFound("资格不存在")
        return row

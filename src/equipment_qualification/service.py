"""装备资格与适用边界管理的领域用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from typing import Any, Mapping, Sequence

from .clock import SystemClock, isoformat, parse_utc
from .contracts import (
    CAPABILITIES,
    CAPABILITY_LABELS,
    ENVIRONMENTS,
    PHASES,
    EvidenceSubmission,
    OperationDemand,
    Protocol,
    ValidationError,
    envelope_fragment,
    fragment_contains,
    merge_fragments,
    require_capability,
)
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .jsonio import canonical_json, content_digest
from .storage import initialize, transaction


ROLE_PERMISSIONS = {
    "equipment_engineer": {
        "catalog.write", "configuration.write", "component.recall", "qualification.read",
    },
    "test_engineer": {
        "protocol.publish", "evidence.submit", "evidence.withdraw",
        "calibration.submit", "calibration.withdraw", "qualification.read",
    },
    "approver": {"qualification.grant", "deviation.review", "qualification.read"},
    "operations": {
        "operation.plan", "operation.complete", "deviation.request",
        "release.request", "qualification.read",
    },
    "auditor": {"qualification.read", "audit.read"},
}


def _jsonable(value: object) -> Any:
    """把含 Decimal/元组的结构转成可回读的标准 JSON 结构。"""

    return json.loads(canonical_json(value))


class QualificationService:
    """在单个 SQLite 连接上提供装备资格全部业务操作。"""

    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return isoformat(self.clock.now())

    @staticmethod
    def _normalize_time(value: str, field: str) -> str:
        try:
            return isoformat(parse_utc(value, field))
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc

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
        previous = self.connection.execute(
            "SELECT event_hash FROM audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type, entity_id, event_type, actor_id,
                canonical_json(payload), previous_hash, event_hash, body["created_at"],
            ),
        )

    def create_user(self, user_id: str, display_name: str, role: str) -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed(f"未知角色: {role}")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO users(user_id,display_name,role) VALUES(?,?,?)",
                    (user_id.strip(), display_name.strip(), role),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"用户已存在: {user_id}") from exc
        return {"user_id": user_id.strip(), "role": role}

    # ------------------------------------------------------------------
    # 装备目录与配置
    # ------------------------------------------------------------------

    def register_design_version(
        self, actor_id: str, design_version_id: str, family: str, version: str, content_sha256: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if len(content_sha256) != 64:
            raise ValidationFailed("设计版本摘要必须是 64 位 SHA-256")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO design_versions(design_version_id,family,version,content_sha256,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (design_version_id, family, version, content_sha256.lower(), self._now()),
                )
                self._audit(
                    "design_version", design_version_id, "design_version.registered", actor_id,
                    {"family": family, "version": version},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("设计版本编号、版本或摘要冲突") from exc
        return {"design_version_id": design_version_id, "family": family, "version": version}

    def register_software_version(
        self, actor_id: str, software_version_id: str, family: str, version: str, content_sha256: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        if len(content_sha256) != 64:
            raise ValidationFailed("软件版本摘要必须是 64 位 SHA-256")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO software_versions(software_version_id,family,version,content_sha256,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (software_version_id, family, version, content_sha256.lower(), self._now()),
                )
                self._audit(
                    "software_version", software_version_id, "software_version.registered", actor_id,
                    {"family": family, "version": version},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("软件版本编号、版本或摘要冲突") from exc
        return {"software_version_id": software_version_id, "family": family, "version": version}

    def register_component_batch(
        self, actor_id: str, component_batch_id: str, component_type: str, batch_no: str, manufacturer: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        for value, field in (
            (component_batch_id, "部件批次编号"), (component_type, "部件类型"),
            (batch_no, "批次号"), (manufacturer, "制造商"),
        ):
            if not value.strip():
                raise ValidationFailed(f"{field}不能为空")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO component_batches(component_batch_id,component_type,batch_no,manufacturer,created_at) "
                    "VALUES(?,?,?,?,?)",
                    (component_batch_id, component_type, batch_no, manufacturer, self._now()),
                )
                self._audit(
                    "component_batch", component_batch_id, "component_batch.registered", actor_id,
                    {"component_type": component_type, "batch_no": batch_no},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("部件批次编号或批次号冲突") from exc
        return {"component_batch_id": component_batch_id, "status": "active"}

    def register_equipment(
        self, actor_id: str, equipment_id: str, equipment_name: str, family: str, serial_no: str
    ) -> dict[str, Any]:
        self._require(actor_id, "catalog.write")
        for value, field in ((equipment_id, "装备编号"), (equipment_name, "装备名称"), (serial_no, "序列号")):
            if not value.strip():
                raise ValidationFailed(f"{field}不能为空")
        try:
            with transaction(self.connection, immediate=True):
                now = self._now()
                self.connection.execute(
                    "INSERT INTO equipment_units(equipment_id,equipment_name,family,serial_no,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (equipment_id, equipment_name.strip(), family.strip(), serial_no.strip(), now, now),
                )
                self._audit(
                    "equipment", equipment_id, "equipment.registered", actor_id,
                    {"equipment_name": equipment_name, "family": family, "serial_no": serial_no},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("装备编号或序列号冲突") from exc
        return {"equipment_id": equipment_id, "family": family.strip()}

    def _equipment(self, equipment_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM equipment_units WHERE equipment_id=?", (equipment_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"装备不存在: {equipment_id}")
        return row

    def configure_equipment(
        self,
        actor_id: str,
        equipment_id: str,
        design_version_id: str,
        software_version_id: str,
        expected_config_revision: int,
    ) -> dict[str, Any]:
        self._require(actor_id, "configuration.write")
        equipment = self._equipment(equipment_id)
        design = self.connection.execute(
            "SELECT * FROM design_versions WHERE design_version_id=?", (design_version_id,)
        ).fetchone()
        if design is None:
            raise NotFound("设计版本不存在")
        software = self.connection.execute(
            "SELECT * FROM software_versions WHERE software_version_id=?", (software_version_id,)
        ).fetchone()
        if software is None:
            raise NotFound("软件版本不存在")
        if design["family"] != equipment["family"] or software["family"] != equipment["family"]:
            raise ValidationFailed("设计版本或软件版本与装备系列不一致")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE equipment_units SET design_version_id=?,software_version_id=?,"
                "config_revision=config_revision+1,updated_at=? "
                "WHERE equipment_id=? AND config_revision=?",
                (design_version_id, software_version_id, now, equipment_id, expected_config_revision),
            )
            if cursor.rowcount != 1:
                raise InvalidState("装备配置版本已变化，请刷新后重试")
            self.connection.execute(
                "INSERT INTO equipment_config_history(equipment_id,config_revision,design_version_id,"
                "software_version_id,configured_by,configured_at) VALUES(?,?,?,?,?,?)",
                (equipment_id, expected_config_revision + 1, design_version_id, software_version_id, actor_id, now),
            )
            self._audit(
                "equipment", equipment_id, "equipment.configured", actor_id,
                {
                    "design_version_id": design_version_id,
                    "software_version_id": software_version_id,
                    "config_revision": expected_config_revision + 1,
                },
            )
        return self.get_equipment(actor_id, equipment_id)

    def get_equipment(self, actor_id: str, equipment_id: str) -> dict[str, Any]:
        self._require(actor_id, "qualification.read")
        equipment = dict(self._equipment(equipment_id))
        components = self.connection.execute(
            "SELECT component_batch_id,installed_at FROM equipment_components "
            "WHERE equipment_id=? AND removed_at IS NULL ORDER BY component_batch_id",
            (equipment_id,),
        ).fetchall()
        equipment["components"] = [dict(row) for row in components]
        return equipment

    def install_component(self, actor_id: str, equipment_id: str, component_batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "configuration.write")
        self._equipment(equipment_id)
        batch = self.connection.execute(
            "SELECT * FROM component_batches WHERE component_batch_id=?", (component_batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("部件批次不存在")
        if batch["status"] != "active":
            raise InvalidState("已召回的部件批次不能安装")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO equipment_components(equipment_id,component_batch_id,installed_by,installed_at) "
                    "VALUES(?,?,?,?)",
                    (equipment_id, component_batch_id, actor_id, self._now()),
                )
                self._audit(
                    "equipment", equipment_id, "component.installed", actor_id,
                    {"component_batch_id": component_batch_id},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该部件批次已安装在此装备上") from exc
        return {"equipment_id": equipment_id, "component_batch_id": component_batch_id}

    def remove_component(self, actor_id: str, equipment_id: str, component_batch_id: str) -> dict[str, Any]:
        self._require(actor_id, "configuration.write")
        self._equipment(equipment_id)
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE equipment_components SET removed_at=? "
                "WHERE equipment_id=? AND component_batch_id=? AND removed_at IS NULL",
                (self._now(), equipment_id, component_batch_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("该部件批次未安装在此装备上")
            self._audit(
                "equipment", equipment_id, "component.removed", actor_id,
                {"component_batch_id": component_batch_id},
            )
        return {"equipment_id": equipment_id, "component_batch_id": component_batch_id, "removed": True}

    def recall_component(self, actor_id: str, component_batch_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "component.recall")
        if not reason.strip():
            raise ValidationFailed("召回原因不能为空")
        batch = self.connection.execute(
            "SELECT * FROM component_batches WHERE component_batch_id=?", (component_batch_id,)
        ).fetchone()
        if batch is None:
            raise NotFound("部件批次不存在")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE component_batches SET status='recalled',recalled_at=?,recalled_by=?,recall_reason=? "
                "WHERE component_batch_id=? AND status='active'",
                (now, actor_id, reason, component_batch_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("部件批次已召回")
            self._audit(
                "component_batch", component_batch_id, "component_batch.recalled", actor_id,
                {"reason": reason},
            )
            invalidated = self._invalidate_pending_releases(
                actor_id,
                "SELECT release_id FROM release_components WHERE component_batch_id=?",
                (component_batch_id,),
                f"部件批次 {component_batch_id} 已召回: {reason}",
                now,
            )
        return {
            "component_batch_id": component_batch_id,
            "status": "recalled",
            "invalidated_releases": invalidated,
        }

    # ------------------------------------------------------------------
    # 试验协议、试验证据与校准证据
    # ------------------------------------------------------------------

    def publish_protocol(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "protocol.publish")
        try:
            protocol = Protocol.from_dict(raw)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        digest = content_digest([raw])
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO test_protocols(protocol_id,version,title,capabilities_json,canonical_json,"
                    "content_sha256,created_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        protocol.protocol_id, protocol.version, protocol.title,
                        canonical_json(list(protocol.capabilities)), canonical_json(raw), digest, self._now(),
                    ),
                )
                self._audit(
                    "protocol", f"{protocol.protocol_id}@{protocol.version}",
                    "protocol.published", actor_id, {"sha256": digest},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("协议版本或内容摘要已经存在") from exc
        return {"protocol_id": protocol.protocol_id, "version": protocol.version, "sha256": digest}

    def _protocol(self, protocol_id: str, version: int) -> tuple[Protocol, str]:
        row = self.connection.execute(
            "SELECT canonical_json,content_sha256 FROM test_protocols WHERE protocol_id=? AND version=?",
            (protocol_id, version),
        ).fetchone()
        if row is None:
            raise NotFound("试验协议版本不存在")
        return Protocol.from_dict(json.loads(row["canonical_json"])), row["content_sha256"]

    @staticmethod
    def _evidence_identity(submission: EvidenceSubmission) -> dict[str, Any]:
        results = sorted(
            (
                {
                    "capability": item.capability,
                    "outcome": item.outcome,
                    "demonstrated": _jsonable(item.demonstrated),
                }
                for item in submission.results
            ),
            key=lambda item: item["capability"],
        )
        return {
            "protocol_id": submission.protocol_id,
            "protocol_version": submission.protocol_version,
            "design_version_id": submission.design_version_id,
            "capability_results": results,
            "tested_at": submission.tested_at,
            "test_site": submission.test_site,
        }

    def submit_evidence(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记试验证据；相同内容重放返回原记录，不重复产生证据。"""

        self._require(actor_id, "evidence.submit")
        if not isinstance(raw, Mapping):
            raise ValidationFailed("证据必须是 JSON 对象")
        protocol_id = raw.get("protocol_id")
        protocol_version = raw.get("protocol_version")
        if not isinstance(protocol_id, str) or isinstance(protocol_version, bool) or not isinstance(protocol_version, int):
            raise ValidationFailed("证据必须引用协议编号与整数版本")
        protocol, _ = self._protocol(protocol_id, protocol_version)
        try:
            submission = EvidenceSubmission.from_dict(raw, protocol)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        design = self.connection.execute(
            "SELECT design_version_id FROM design_versions WHERE design_version_id=?",
            (submission.design_version_id,),
        ).fetchone()
        if design is None:
            raise NotFound("证据引用的设计版本不存在")
        tested_at = self._normalize_time(submission.tested_at, "evidence.tested_at")
        normalized = self._evidence_identity(
            EvidenceSubmission(
                protocol_id=submission.protocol_id,
                protocol_version=submission.protocol_version,
                design_version_id=submission.design_version_id,
                results=submission.results,
                tested_at=tested_at,
                test_site=submission.test_site,
            )
        )
        digest = content_digest([normalized])
        existing = self.connection.execute(
            "SELECT evidence_id,status FROM test_evidence WHERE content_sha256=?", (digest,)
        ).fetchone()
        if existing is not None:
            return {
                "evidence_id": existing["evidence_id"],
                "content_sha256": digest,
                "status": existing["status"],
                "replayed": True,
            }
        results_json = canonical_json(normalized["capability_results"])
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO test_evidence(protocol_id,protocol_version,design_version_id,results_json,"
                    "canonical_json,content_sha256,tested_at,test_site,submitted_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        submission.protocol_id, submission.protocol_version, submission.design_version_id,
                        results_json, canonical_json(normalized), digest, tested_at,
                        submission.test_site, actor_id, self._now(),
                    ),
                )
                evidence_id = cursor.lastrowid
                self._audit(
                    "test_evidence", str(evidence_id), "evidence.submitted", actor_id,
                    {"content_sha256": digest, "design_version_id": submission.design_version_id},
                )
        except sqlite3.IntegrityError:
            row = self.connection.execute(
                "SELECT evidence_id,status FROM test_evidence WHERE content_sha256=?", (digest,)
            ).fetchone()
            return {
                "evidence_id": row["evidence_id"],
                "content_sha256": digest,
                "status": row["status"],
                "replayed": True,
            }
        return {
            "evidence_id": evidence_id,
            "content_sha256": digest,
            "status": "valid",
            "replayed": False,
        }

    def _evidence_row(self, evidence_id: int) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM test_evidence WHERE evidence_id=?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"试验证据不存在: {evidence_id}")
        return row

    def _invalidate_pending_releases(
        self, actor_id: str, select_sql: str, params: tuple[Any, ...], reason: str, now: str
    ) -> list[int]:
        """把依赖已失效依据、但作业尚未执行的放行标记为无效；已完成作业保持原样。"""

        rows = self.connection.execute(
            f"SELECT r.release_id, r.operation_id FROM releases r "
            f"JOIN operations o ON o.operation_id = r.operation_id "
            f"WHERE r.state='active' AND o.state='planned' AND r.release_id IN ({select_sql})",
            params,
        ).fetchall()
        invalidated: list[int] = []
        for row in rows:
            self.connection.execute(
                "UPDATE releases SET state='invalidated',invalidated_at=?,invalidate_reason=? "
                "WHERE release_id=? AND state='active'",
                (now, reason, row["release_id"]),
            )
            self._audit(
                "release", str(row["release_id"]), "release.invalidated", actor_id,
                {"operation_id": row["operation_id"], "reason": reason},
            )
            invalidated.append(row["release_id"])
        return invalidated

    def withdraw_evidence(self, actor_id: str, evidence_id: int, reason: str) -> dict[str, Any]:
        """撤回试验证据；只影响尚未执行的作业，已完成作业保留当时依据。"""

        self._require(actor_id, "evidence.withdraw")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        self._evidence_row(evidence_id)
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE test_evidence SET status='withdrawn',withdrawn_at=?,withdrawn_by=?,withdraw_reason=? "
                "WHERE evidence_id=? AND status='valid'",
                (now, actor_id, reason, evidence_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("试验证据已撤回")
            self._audit("test_evidence", str(evidence_id), "evidence.withdrawn", actor_id, {"reason": reason})
            invalidated = self._invalidate_pending_releases(
                actor_id,
                "SELECT release_id FROM release_evidence WHERE evidence_id=?",
                (evidence_id,),
                f"试验证据 {evidence_id} 已撤回: {reason}",
                now,
            )
        return {"evidence_id": evidence_id, "status": "withdrawn", "invalidated_releases": invalidated}

    def submit_calibration(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        """登记校准证据；相同内容重放返回原记录。"""

        self._require(actor_id, "calibration.submit")
        if not isinstance(raw, Mapping):
            raise ValidationFailed("校准证据必须是 JSON 对象")
        equipment_id = raw.get("equipment_id")
        instrument = raw.get("instrument")
        certificate_no = raw.get("certificate_no")
        for value, field in (
            (equipment_id, "equipment_id"), (instrument, "instrument"), (certificate_no, "certificate_no"),
        ):
            if not isinstance(value, str) or not value.strip():
                raise ValidationFailed(f"校准证据字段 {field} 必须是非空字符串")
        self._equipment(equipment_id.strip())
        calibrated_at = self._normalize_time(raw.get("calibrated_at"), "calibration.calibrated_at")
        valid_until = self._normalize_time(raw.get("valid_until"), "calibration.valid_until")
        if valid_until <= calibrated_at:
            raise ValidationFailed("校准有效期必须晚于校准时间")
        normalized = {
            "equipment_id": equipment_id.strip(),
            "instrument": instrument.strip(),
            "certificate_no": certificate_no.strip(),
            "calibrated_at": calibrated_at,
            "valid_until": valid_until,
        }
        digest = content_digest([normalized])
        existing = self.connection.execute(
            "SELECT calibration_id,status FROM calibration_evidence WHERE content_sha256=?", (digest,)
        ).fetchone()
        if existing is not None:
            return {
                "calibration_id": existing["calibration_id"],
                "content_sha256": digest,
                "status": existing["status"],
                "replayed": True,
            }
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO calibration_evidence(equipment_id,instrument,calibrated_at,valid_until,"
                    "canonical_json,content_sha256,submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        normalized["equipment_id"], normalized["instrument"], calibrated_at, valid_until,
                        canonical_json(normalized), digest, actor_id, self._now(),
                    ),
                )
                calibration_id = cursor.lastrowid
                self._audit(
                    "calibration_evidence", str(calibration_id), "calibration.submitted", actor_id,
                    {"content_sha256": digest, "equipment_id": normalized["equipment_id"]},
                )
        except sqlite3.IntegrityError:
            row = self.connection.execute(
                "SELECT calibration_id,status FROM calibration_evidence WHERE content_sha256=?", (digest,)
            ).fetchone()
            return {
                "calibration_id": row["calibration_id"],
                "content_sha256": digest,
                "status": row["status"],
                "replayed": True,
            }
        return {
            "calibration_id": calibration_id,
            "content_sha256": digest,
            "status": "valid",
            "replayed": False,
        }

    def withdraw_calibration(self, actor_id: str, calibration_id: int, reason: str) -> dict[str, Any]:
        self._require(actor_id, "calibration.withdraw")
        if not reason.strip():
            raise ValidationFailed("撤回原因不能为空")
        row = self.connection.execute(
            "SELECT * FROM calibration_evidence WHERE calibration_id=?", (calibration_id,)
        ).fetchone()
        if row is None:
            raise NotFound("校准证据不存在")
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE calibration_evidence SET status='withdrawn',withdrawn_at=?,withdrawn_by=?,withdraw_reason=? "
                "WHERE calibration_id=? AND status='valid'",
                (now, actor_id, reason, calibration_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("校准证据已撤回")
            self._audit(
                "calibration_evidence", str(calibration_id), "calibration.withdrawn", actor_id,
                {"reason": reason},
            )
            invalidated = self._invalidate_pending_releases(
                actor_id,
                "SELECT rg.release_id FROM release_grants rg "
                "JOIN grant_calibrations gc ON gc.grant_id = rg.grant_id "
                "WHERE gc.calibration_id=?",
                (calibration_id,),
                f"校准证据 {calibration_id} 已撤回: {reason}",
                now,
            )
        return {"calibration_id": calibration_id, "status": "withdrawn", "invalidated_releases": invalidated}

    # ------------------------------------------------------------------
    # 资格批准（可按能力部分通过）
    # ------------------------------------------------------------------

    def _current_components(self, equipment_id: str) -> list[str]:
        rows = self.connection.execute(
            "SELECT component_batch_id FROM equipment_components "
            "WHERE equipment_id=? AND removed_at IS NULL ORDER BY component_batch_id",
            (equipment_id,),
        ).fetchall()
        return [row["component_batch_id"] for row in rows]

    def grant_qualification(
        self,
        actor_id: str,
        equipment_id: str,
        capability: str,
        envelope_raw: Mapping[str, Any],
        evidence_ids: Sequence[int],
        calibration_ids: Sequence[int],
        valid_until: str | None = None,
    ) -> dict[str, Any]:
        """按单项能力批准适用包络；相同依据链重放不重复批准。"""

        self._require(actor_id, "qualification.grant")
        try:
            capability = require_capability(capability)
            envelope = envelope_fragment(capability, envelope_raw, "grant.envelope")
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        equipment = self._equipment(equipment_id)
        if equipment["design_version_id"] is None or equipment["software_version_id"] is None:
            raise InvalidState("装备尚未配置设计版本与软件版本")
        if not evidence_ids:
            raise ValidationFailed("资格批准必须引用至少一份试验证据")
        if not calibration_ids:
            raise ValidationFailed("资格批准必须引用至少一份校准证据")
        now = self._now()
        valid_until_text = None
        if valid_until is not None:
            valid_until_text = self._normalize_time(valid_until, "grant.valid_until")
            if valid_until_text <= now:
                raise ValidationFailed("批准有效期必须晚于当前时间")
        demonstrated: list[Mapping[str, Any]] = []
        evidence_id_set = sorted({int(item) for item in evidence_ids})
        for evidence_id in evidence_id_set:
            evidence = self._evidence_row(evidence_id)
            if evidence["status"] != "valid":
                raise InvalidState(f"试验证据 {evidence_id} 已撤回，不能用于批准")
            if evidence["design_version_id"] != equipment["design_version_id"]:
                raise ValidationFailed(
                    f"试验证据 {evidence_id} 对应的设计版本与装备当前配置不一致"
                )
            for result in json.loads(evidence["results_json"]):
                if result["capability"] == capability and result["outcome"] == "pass":
                    demonstrated.append(result["demonstrated"])
        merged = merge_fragments(demonstrated)
        if merged is None:
            raise ValidationFailed(f"引用证据未包含能力 {capability} 的合格结论")
        if not fragment_contains(merged, envelope):
            raise ValidationFailed(f"批准包络超出能力 {capability} 的已验证范围")
        calibration_id_set = sorted({int(item) for item in calibration_ids})
        for calibration_id in calibration_id_set:
            calibration = self.connection.execute(
                "SELECT * FROM calibration_evidence WHERE calibration_id=?", (calibration_id,)
            ).fetchone()
            if calibration is None:
                raise NotFound(f"校准证据不存在: {calibration_id}")
            if calibration["equipment_id"] != equipment_id:
                raise ValidationFailed(f"校准证据 {calibration_id} 不属于该装备")
            if calibration["status"] != "valid":
                raise InvalidState(f"校准证据 {calibration_id} 已撤回，不能用于批准")
            if calibration["valid_until"] < now:
                raise InvalidState(f"校准证据 {calibration_id} 已过期，不能用于批准")
        components = self._current_components(equipment_id)
        if not components:
            raise InvalidState("装备未安装任何部件批次，资格链不完整")
        basis = {
            "equipment_id": equipment_id,
            "capability": capability,
            "envelope": _jsonable(envelope),
            "design_version_id": equipment["design_version_id"],
            "software_version_id": equipment["software_version_id"],
            "component_batch_ids": components,
            "evidence_ids": evidence_id_set,
            "calibration_ids": calibration_id_set,
        }
        basis_digest = content_digest([basis])
        existing = self.connection.execute(
            "SELECT grant_id FROM qualification_grants "
            "WHERE equipment_id=? AND capability=? AND basis_sha256=?",
            (equipment_id, capability, basis_digest),
        ).fetchone()
        if existing is not None:
            return {
                "grant_id": existing["grant_id"],
                "capability": capability,
                "basis_sha256": basis_digest,
                "replayed": True,
            }
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO qualification_grants(equipment_id,capability,envelope_json,design_version_id,"
                    "software_version_id,basis_json,basis_sha256,approved_by,approved_at,valid_from,valid_until) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        equipment_id, capability, canonical_json(basis["envelope"]),
                        equipment["design_version_id"], equipment["software_version_id"],
                        canonical_json(basis), basis_digest, actor_id, now, now, valid_until_text,
                    ),
                )
                grant_id = cursor.lastrowid
                for evidence_id in evidence_id_set:
                    self.connection.execute(
                        "INSERT INTO grant_evidence(grant_id,evidence_id) VALUES(?,?)",
                        (grant_id, evidence_id),
                    )
                for calibration_id in calibration_id_set:
                    self.connection.execute(
                        "INSERT INTO grant_calibrations(grant_id,calibration_id) VALUES(?,?)",
                        (grant_id, calibration_id),
                    )
                for component_batch_id in components:
                    self.connection.execute(
                        "INSERT INTO grant_components(grant_id,component_batch_id) VALUES(?,?)",
                        (grant_id, component_batch_id),
                    )
                self._audit(
                    "qualification_grant", str(grant_id), "qualification.granted", actor_id,
                    {
                        "equipment_id": equipment_id,
                        "capability": capability,
                        "basis_sha256": basis_digest,
                        "valid_until": valid_until_text,
                    },
                )
        except sqlite3.IntegrityError:
            row = self.connection.execute(
                "SELECT grant_id FROM qualification_grants "
                "WHERE equipment_id=? AND capability=? AND basis_sha256=?",
                (equipment_id, capability, basis_digest),
            ).fetchone()
            return {
                "grant_id": row["grant_id"],
                "capability": capability,
                "basis_sha256": basis_digest,
                "replayed": True,
            }
        return {
            "grant_id": grant_id,
            "capability": capability,
            "basis_sha256": basis_digest,
            "replayed": False,
        }

    # ------------------------------------------------------------------
    # 作业、偏差与放行
    # ------------------------------------------------------------------

    def plan_operation(
        self,
        actor_id: str,
        operation_id: str,
        well_id: str,
        phase: str,
        environment: str,
        water_depth_m: object,
        temperature_c: object,
        pressure_mpa: object,
        planned_start: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "operation.plan")
        try:
            demand = OperationDemand.from_values(water_depth_m, temperature_c, pressure_mpa, phase, environment)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        if not operation_id.strip() or not well_id.strip():
            raise ValidationFailed("作业编号和井号不能为空")
        planned = self._normalize_time(planned_start, "operation.planned_start")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO operations(operation_id,well_id,phase,environment,water_depth_m,temperature_c,"
                    "pressure_mpa,planned_start,state,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        operation_id.strip(), well_id.strip(), demand.phase, demand.environment,
                        format(demand.water_depth_m, "f"), format(demand.temperature_c, "f"),
                        format(demand.pressure_mpa, "f"), planned, "planned", actor_id, self._now(),
                    ),
                )
                self._audit(
                    "operation", operation_id.strip(), "operation.planned", actor_id,
                    {"well_id": well_id.strip(), "phase": demand.phase, "environment": demand.environment},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict(f"作业已存在: {operation_id}") from exc
        return {"operation_id": operation_id.strip(), "state": "planned"}

    def _operation(self, operation_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM operations WHERE operation_id=?", (operation_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"作业不存在: {operation_id}")
        return row

    def complete_operation(self, actor_id: str, operation_id: str) -> dict[str, Any]:
        self._require(actor_id, "operation.complete")
        self._operation(operation_id)
        now = self._now()
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE operations SET state='completed',completed_at=? WHERE operation_id=? AND state='planned'",
                (now, operation_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("作业不是计划中状态")
            executed = self.connection.execute(
                "UPDATE releases SET state='executed',executed_at=? WHERE operation_id=? AND state='active'",
                (now, operation_id),
            ).rowcount
            self._audit(
                "operation", operation_id, "operation.completed", actor_id,
                {"executed_releases": executed},
            )
        return {"operation_id": operation_id, "state": "completed", "executed_releases": executed}

    def cancel_operation(self, actor_id: str, operation_id: str, reason: str) -> dict[str, Any]:
        self._require(actor_id, "operation.complete")
        self._operation(operation_id)
        now = self._now()
        with transaction(self.connection, immediate=True):
            invalidated = self._invalidate_pending_releases(
                actor_id,
                "SELECT release_id FROM releases WHERE operation_id=?",
                (operation_id,),
                f"作业已取消: {reason}",
                now,
            )
            cursor = self.connection.execute(
                "UPDATE operations SET state='cancelled',cancelled_at=? WHERE operation_id=? AND state='planned'",
                (now, operation_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("作业不是计划中状态")
            self._audit(
                "operation", operation_id, "operation.cancelled", actor_id,
                {"reason": reason, "invalidated_releases": invalidated},
            )
        return {"operation_id": operation_id, "state": "cancelled", "invalidated_releases": invalidated}

    def request_deviation(
        self, actor_id: str, operation_id: str, equipment_id: str, capability: str, justification: str
    ) -> dict[str, Any]:
        self._require(actor_id, "deviation.request")
        try:
            capability = require_capability(capability)
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        operation = self._operation(operation_id)
        if operation["state"] != "planned":
            raise InvalidState("只有计划中的作业可以申请偏差")
        self._equipment(equipment_id)
        if not justification.strip():
            raise ValidationFailed("偏差理由不能为空")
        demand = OperationDemand.from_values(
            operation["water_depth_m"], operation["temperature_c"], operation["pressure_mpa"],
            operation["phase"], operation["environment"],
        )
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO deviations(operation_id,equipment_id,capability,demand_json,justification,"
                    "status,requested_by,requested_at) VALUES(?,?,?,?,?,?,?,?)",
                    (
                        operation_id, equipment_id, capability,
                        canonical_json(_jsonable(demand.requirement(capability))),
                        justification.strip(), "requested", actor_id, self._now(),
                    ),
                )
                deviation_id = cursor.lastrowid
                self._audit(
                    "deviation", str(deviation_id), "deviation.requested", actor_id,
                    {"operation_id": operation_id, "equipment_id": equipment_id, "capability": capability},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("该作业对此装备同一能力已有待处理或已批准的偏差") from exc
        return {"deviation_id": deviation_id, "status": "requested"}

    def review_deviation(
        self,
        actor_id: str,
        deviation_id: int,
        approve: bool,
        note: str,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        self._require(actor_id, "deviation.review")
        row = self.connection.execute(
            "SELECT * FROM deviations WHERE deviation_id=?", (deviation_id,)
        ).fetchone()
        if row is None:
            raise NotFound("偏差申请不存在")
        if row["status"] != "requested":
            raise InvalidState("偏差申请已经处理")
        if row["requested_by"] == actor_id:
            raise Forbidden("申请人不能复核自己的偏差申请")
        expires_text = None
        if approve and expires_at is not None:
            expires_text = self._normalize_time(expires_at, "deviation.expires_at")
            if expires_text <= self._now():
                raise ValidationFailed("偏差有效期必须晚于当前时间")
        status = "approved" if approve else "rejected"
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "UPDATE deviations SET status=?,reviewed_by=?,reviewed_at=?,review_note=?,expires_at=? "
                "WHERE deviation_id=? AND status='requested'",
                (status, actor_id, self._now(), note, expires_text, deviation_id),
            )
            if cursor.rowcount != 1:
                raise InvalidState("偏差申请已经处理")
            self._audit(
                "deviation", str(deviation_id), f"deviation.{status}", actor_id,
                {"note": note, "expires_at": expires_text},
            )
        return {"deviation_id": deviation_id, "status": status}

    # ------------------------------------------------------------------
    # 资格状态计算与放行门禁
    # ------------------------------------------------------------------

    def _configuration_at(self, equipment_id: str, as_of: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM equipment_config_history "
            "WHERE equipment_id=? AND configured_at<=? ORDER BY config_revision DESC LIMIT 1",
            (equipment_id, as_of),
        ).fetchone()

    def _components_at(self, equipment_id: str, as_of: str) -> set[str]:
        rows = self.connection.execute(
            "SELECT component_batch_id FROM equipment_components "
            "WHERE equipment_id=? AND installed_at<=? AND (removed_at IS NULL OR removed_at>?)",
            (equipment_id, as_of, as_of),
        ).fetchall()
        return {row["component_batch_id"] for row in rows}

    def _grant_blocking_reasons(
        self,
        grant: sqlite3.Row,
        config: sqlite3.Row | None,
        components_at: set[str],
        as_of: str,
    ) -> list[str]:
        reasons: list[str] = []
        if grant["valid_from"] > as_of:
            reasons.append("批准尚未生效")
        if grant["valid_until"] is not None and grant["valid_until"] < as_of:
            reasons.append("批准已过期")
        if config is None:
            reasons.append("装备当时尚未配置设计版本与软件版本")
        else:
            if config["design_version_id"] != grant["design_version_id"]:
                reasons.append("设计版本已变更，需重新批准")
            if config["software_version_id"] != grant["software_version_id"]:
                reasons.append("软件版本已变更，需重新批准")
        evidence_rows = self.connection.execute(
            "SELECT e.evidence_id,e.status,e.withdrawn_at FROM test_evidence e "
            "JOIN grant_evidence ge ON ge.evidence_id=e.evidence_id WHERE ge.grant_id=?",
            (grant["grant_id"],),
        ).fetchall()
        for evidence in evidence_rows:
            if evidence["status"] == "withdrawn" and evidence["withdrawn_at"] <= as_of:
                reasons.append(f"试验证据 {evidence['evidence_id']} 已撤回，需重新试验")
        calibration_rows = self.connection.execute(
            "SELECT c.calibration_id,c.status,c.withdrawn_at,c.calibrated_at,c.valid_until "
            "FROM calibration_evidence c "
            "JOIN grant_calibrations gc ON gc.calibration_id=c.calibration_id WHERE gc.grant_id=?",
            (grant["grant_id"],),
        ).fetchall()
        for calibration in calibration_rows:
            if calibration["status"] == "withdrawn" and calibration["withdrawn_at"] <= as_of:
                reasons.append(f"校准证据 {calibration['calibration_id']} 已撤回，需重新校准")
            elif calibration["calibrated_at"] > as_of:
                reasons.append(f"校准证据 {calibration['calibration_id']} 尚未生效")
            elif calibration["valid_until"] < as_of:
                reasons.append(f"校准证据 {calibration['calibration_id']} 已过期，需重新校准")
        component_rows = self.connection.execute(
            "SELECT c.component_batch_id,c.status,c.recalled_at FROM component_batches c "
            "JOIN grant_components gc ON gc.component_batch_id=c.component_batch_id WHERE gc.grant_id=?",
            (grant["grant_id"],),
        ).fetchall()
        for component in component_rows:
            if component["status"] == "recalled" and component["recalled_at"] <= as_of:
                reasons.append(f"部件批次 {component['component_batch_id']} 已召回，需更换部件并重新批准")
            if component["component_batch_id"] not in components_at:
                reasons.append(f"部件批次 {component['component_batch_id']} 已拆换，需重新批准")
        return reasons

    def _capability_status(
        self, equipment_id: str, capability: str, as_of: str
    ) -> dict[str, Any]:
        config = self._configuration_at(equipment_id, as_of)
        components_at = self._components_at(equipment_id, as_of)
        grants = self.connection.execute(
            "SELECT * FROM qualification_grants WHERE equipment_id=? AND capability=? ORDER BY grant_id",
            (equipment_id, capability),
        ).fetchall()
        grant_views: list[dict[str, Any]] = []
        effective_envelopes: list[Mapping[str, Any]] = []
        effective_grants: list[sqlite3.Row] = []
        for grant in grants:
            blocking = self._grant_blocking_reasons(grant, config, components_at, as_of)
            envelope = json.loads(grant["envelope_json"])
            grant_views.append({
                "grant_id": grant["grant_id"],
                "envelope": envelope,
                "basis_sha256": grant["basis_sha256"],
                "approved_by": grant["approved_by"],
                "approved_at": grant["approved_at"],
                "valid_until": grant["valid_until"],
                "effective": not blocking,
                "blocking": blocking,
            })
            if not blocking:
                effective_envelopes.append(envelope)
                effective_grants.append(grant)
        merged = merge_fragments(effective_envelopes)
        missing: list[str] = []
        if merged is None:
            missing = self._missing_evidence(equipment_id, capability, as_of, config, grant_views)
        return {
            "qualified": merged is not None,
            "envelope": _jsonable(merged) if merged is not None else None,
            "grants": grant_views,
            "effective_grants": effective_grants,
            "missing": missing,
        }

    def _missing_evidence(
        self,
        equipment_id: str,
        capability: str,
        as_of: str,
        config: sqlite3.Row | None,
        grant_views: Sequence[Mapping[str, Any]],
    ) -> list[str]:
        if grant_views:
            reasons: list[str] = []
            for view in grant_views:
                for reason in view["blocking"]:
                    if reason not in reasons:
                        reasons.append(reason)
            return reasons
        if config is None:
            return ["装备尚未配置设计版本与软件版本"]
        evidence_rows = self.connection.execute(
            "SELECT results_json,status,withdrawn_at FROM test_evidence WHERE design_version_id=?",
            (config["design_version_id"],),
        ).fetchall()
        has_passing_evidence = False
        for evidence in evidence_rows:
            if evidence["status"] == "withdrawn" and evidence["withdrawn_at"] <= as_of:
                continue
            for result in json.loads(evidence["results_json"]):
                if result["capability"] == capability and result["outcome"] == "pass":
                    has_passing_evidence = True
                    break
            if has_passing_evidence:
                break
        if not has_passing_evidence:
            return [f"缺少{CAPABILITY_LABELS[capability]}合格试验证据"]
        calibration = self.connection.execute(
            "SELECT 1 FROM calibration_evidence WHERE equipment_id=? AND calibrated_at<=? AND valid_until>=? "
            "AND (status='valid' OR withdrawn_at>?) LIMIT 1",
            (equipment_id, as_of, as_of, as_of),
        ).fetchone()
        if calibration is None:
            return ["缺少有效校准证据"]
        return ["证据已具备，待资格批准"]

    def request_release(self, actor_id: str, operation_id: str, equipment_id: str) -> dict[str, Any]:
        """放行门禁：资格覆盖或有已批准偏差才放行；同一作业同一装备只成功一次。"""

        self._require(actor_id, "release.request")
        operation = self._operation(operation_id)
        if operation["state"] != "planned":
            raise InvalidState("只有计划中的作业可以放行")
        self._equipment(equipment_id)
        now = self._now()
        demand = OperationDemand.from_values(
            operation["water_depth_m"], operation["temperature_c"], operation["pressure_mpa"],
            operation["phase"], operation["environment"],
        )
        used_grants: list[sqlite3.Row] = []
        used_deviations: list[sqlite3.Row] = []
        uncovered: list[str] = []
        for capability in CAPABILITIES:
            status = self._capability_status(equipment_id, capability, now)
            requirement = _jsonable(demand.requirement(capability))
            if status["envelope"] is not None and fragment_contains(status["envelope"], requirement):
                used_grants.extend(status["effective_grants"])
                continue
            deviation = self.connection.execute(
                "SELECT * FROM deviations WHERE operation_id=? AND equipment_id=? AND capability=? "
                "AND status='approved' AND (expires_at IS NULL OR expires_at>?) "
                "ORDER BY deviation_id DESC LIMIT 1",
                (operation_id, equipment_id, capability, now),
            ).fetchone()
            if deviation is not None:
                used_deviations.append(deviation)
                continue
            uncovered.append(capability)
        if uncovered:
            labels = "、".join(f"{CAPABILITY_LABELS[item]}({item})" for item in uncovered)
            raise InvalidState(f"资格不足且无可用的已批准偏差，不能放行: {labels}")
        config = self._configuration_at(equipment_id, now)
        components = sorted(self._components_at(equipment_id, now))
        grant_ids = sorted({grant["grant_id"] for grant in used_grants})
        evidence_rows = self.connection.execute(
            "SELECT DISTINCT e.evidence_id,e.content_sha256 FROM test_evidence e "
            "JOIN grant_evidence ge ON ge.evidence_id=e.evidence_id "
            "WHERE ge.grant_id IN (%s) ORDER BY e.evidence_id"
            % ",".join("?" for _ in grant_ids),
            tuple(grant_ids),
        ).fetchall() if grant_ids else []
        calibration_rows = self.connection.execute(
            "SELECT DISTINCT c.calibration_id,c.content_sha256 FROM calibration_evidence c "
            "JOIN grant_calibrations gc ON gc.calibration_id=c.calibration_id "
            "WHERE gc.grant_id IN (%s) ORDER BY c.calibration_id"
            % ",".join("?" for _ in grant_ids),
            tuple(grant_ids),
        ).fetchall() if grant_ids else []
        basis = {
            "as_of": now,
            "operation_id": operation_id,
            "equipment_id": equipment_id,
            "design_version_id": None if config is None else config["design_version_id"],
            "software_version_id": None if config is None else config["software_version_id"],
            "component_batch_ids": components,
            "grants": [
                {
                    "grant_id": grant["grant_id"],
                    "capability": grant["capability"],
                    "basis_sha256": grant["basis_sha256"],
                }
                for grant in sorted(used_grants, key=lambda row: row["grant_id"])
            ],
            "evidence": [
                {"evidence_id": row["evidence_id"], "content_sha256": row["content_sha256"]}
                for row in evidence_rows
            ],
            "calibrations": [
                {"calibration_id": row["calibration_id"], "content_sha256": row["content_sha256"]}
                for row in calibration_rows
            ],
            "deviations": [
                {"deviation_id": row["deviation_id"], "capability": row["capability"]}
                for row in used_deviations
            ],
        }
        basis_digest = content_digest([basis])
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO releases(operation_id,equipment_id,basis_json,basis_sha256,granted_by,granted_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (operation_id, equipment_id, canonical_json(basis), basis_digest, actor_id, now),
                )
                release_id = cursor.lastrowid
                for grant_id in grant_ids:
                    self.connection.execute(
                        "INSERT INTO release_grants(release_id,grant_id) VALUES(?,?)",
                        (release_id, grant_id),
                    )
                for row in evidence_rows:
                    self.connection.execute(
                        "INSERT INTO release_evidence(release_id,evidence_id) VALUES(?,?)",
                        (release_id, row["evidence_id"]),
                    )
                for component_batch_id in components:
                    self.connection.execute(
                        "INSERT INTO release_components(release_id,component_batch_id) VALUES(?,?)",
                        (release_id, component_batch_id),
                    )
                for deviation in used_deviations:
                    self.connection.execute(
                        "UPDATE deviations SET status='used' WHERE deviation_id=? AND status='approved'",
                        (deviation["deviation_id"],),
                    )
                self._audit(
                    "release", str(release_id), "release.granted", actor_id,
                    {
                        "operation_id": operation_id,
                        "equipment_id": equipment_id,
                        "basis_sha256": basis_digest,
                        "deviation_ids": [row["deviation_id"] for row in used_deviations],
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("同一作业对同一装备的放行只能成功一次") from exc
        return {
            "release_id": release_id,
            "operation_id": operation_id,
            "equipment_id": equipment_id,
            "basis_sha256": basis_digest,
            "state": "active",
        }

    # ------------------------------------------------------------------
    # 解释与追溯
    # ------------------------------------------------------------------

    def explain_equipment(
        self,
        actor_id: str,
        equipment_id: str,
        as_of: str | None = None,
        target: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """按任意时间解释装备可用的水深、温压与作业阶段，以及还缺少的证据。"""

        self._require(actor_id, "qualification.read")
        self._equipment(equipment_id)
        moment = self._normalize_time(as_of, "as_of") if as_of else self._now()
        config = self._configuration_at(equipment_id, moment)
        components = sorted(self._components_at(equipment_id, moment))
        capabilities: dict[str, Any] = {}
        for capability in CAPABILITIES:
            status = self._capability_status(equipment_id, capability, moment)
            capabilities[capability] = {
                "label": CAPABILITY_LABELS[capability],
                "qualified": status["qualified"],
                "envelope": status["envelope"],
                "grants": status["grants"],
                "missing": status["missing"],
            }
        target_report = None
        if target is not None:
            target_report = self._evaluate_target(capabilities, target)
        return {
            "equipment_id": equipment_id,
            "as_of": moment,
            "configuration": {
                "design_version_id": None if config is None else config["design_version_id"],
                "software_version_id": None if config is None else config["software_version_id"],
                "component_batch_ids": components,
            },
            "capabilities": capabilities,
            "target": target_report,
        }

    @staticmethod
    def _evaluate_target(
        capabilities: Mapping[str, Any], target: Mapping[str, Any]
    ) -> dict[str, Any]:
        checks: dict[str, Mapping[str, Any]] = {}
        try:
            if "water_depth_m" in target:
                depth = OperationDemand.from_values(
                    target["water_depth_m"], "0", "1", "drilling", "normal"
                ).water_depth_m
                checks["water_depth"] = {"max_water_depth_m": depth}
            if "temperature_c" in target:
                demand = OperationDemand.from_values(
                    "1", target["temperature_c"], "1", "drilling", "normal"
                )
                checks["temperature"] = demand.requirement("temperature")
            if "pressure_mpa" in target:
                demand = OperationDemand.from_values(
                    "1", "0", target["pressure_mpa"], "drilling", "normal"
                )
                checks["pressure"] = demand.requirement("pressure")
            if "phase" in target:
                phase = target["phase"]
                if phase not in PHASES:
                    raise ValidationError(f"target.phase 不受支持: {phase}")
                checks["phase"] = {"phases": (phase,)}
            if "environment" in target:
                environment = target["environment"]
                if environment not in ENVIRONMENTS:
                    raise ValidationError(f"target.environment 不受支持: {environment}")
                checks["environment"] = {"environments": (environment,)}
        except ValidationError as exc:
            raise ValidationFailed(str(exc)) from exc
        if not checks:
            raise ValidationFailed("目标包络至少需要一个维度")
        shortfalls: list[str] = []
        for capability, requirement in checks.items():
            envelope = capabilities[capability]["envelope"]
            if envelope is None or not fragment_contains(envelope, _jsonable(requirement)):
                shortfalls.append(capability)
        missing: set[str] = set()
        for capability in shortfalls:
            if capabilities[capability]["missing"]:
                missing.update(capabilities[capability]["missing"])
            else:
                missing.add(
                    f"已批准{CAPABILITY_LABELS[capability]}包络不覆盖目标，需补充试验证据并重新批准"
                )
        return {
            "covered": not shortfalls,
            "shortfalls": shortfalls,
            "missing": sorted(missing),
        }

    def get_release(self, actor_id: str, release_id: int) -> dict[str, Any]:
        self._require(actor_id, "qualification.read")
        row = self.connection.execute(
            "SELECT * FROM releases WHERE release_id=?", (release_id,)
        ).fetchone()
        if row is None:
            raise NotFound("放行记录不存在")
        result = dict(row)
        result["basis"] = json.loads(row["basis_json"])
        del result["basis_json"]
        return result

    def audit_trail(self, actor_id: str, entity_type: str, entity_id: str) -> list[dict[str, Any]]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT event_id,entity_type,entity_id,event_type,actor_id,payload_json,previous_hash,event_hash,"
            "created_at FROM audit_events WHERE entity_type=? AND entity_id=? ORDER BY event_id",
            (entity_type, entity_id),
        ).fetchall()
        return [dict(row) | {"payload": json.loads(row["payload_json"])} for row in rows]

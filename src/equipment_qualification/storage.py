"""装备资格与适用边界的 SQLite 模式与事务辅助。

时间模型采用双时态：
- 业务有效时间（valid_from/valid_to / revoked_at）：资格在现场实际生效区间；
- 系统记录时间（created_at 等）：数据进入系统的时间，永不修改。

撤回、召回只写新状态并关闭未执行的放行，不删除任何历史行，因此
"已完成作业保留当时依据" 由结构本身保证。
"""

from __future__ import annotations

import contextlib
import sqlite3
from collections.abc import Iterator
from pathlib import Path


SCHEMA_VERSION = 1

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('designer', 'test_engineer', 'qualification_engineer', 'approver', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

-- 装备型号与设计版本（璇玑/海经/海脉等自主装备的具体设计构型）
CREATE TABLE IF NOT EXISTS equipment_models (
    model_id TEXT PRIMARY KEY,
    model_name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS design_revisions (
    design_revision_id TEXT PRIMARY KEY,
    model_id TEXT NOT NULL REFERENCES equipment_models(model_id),
    design_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE (model_id, design_version),
    UNIQUE (content_sha256)
);

-- 部件批次（可能被召回）
CREATE TABLE IF NOT EXISTS component_batches (
    component_batch_id TEXT PRIMARY KEY,
    part_number TEXT NOT NULL,
    batch_version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'recalled')),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    recalled_at TEXT,
    recall_reason TEXT,
    UNIQUE (part_number, batch_version)
);

-- 试验协议（版本化目录，内容寻址）
CREATE TABLE IF NOT EXISTS test_protocols (
    test_protocol_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    capability TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (test_protocol_id, version),
    UNIQUE (content_sha256)
);

-- 资格证据（试验报告 / 校准证书 / 软件版本认证 / 设计符合性）。
-- evidence_kind 决定其指向哪类对象；相同内容重放返回同一行，不重复批准。
CREATE TABLE IF NOT EXISTS qualification_evidence (
    evidence_id TEXT PRIMARY KEY,
    evidence_kind TEXT NOT NULL CHECK (evidence_kind IN ('test_report', 'calibration', 'software', 'design_conformance')),
    title TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    canonical_json TEXT NOT NULL,
    capability TEXT,
    test_protocol_id TEXT,
    test_protocol_version INTEGER,
    design_revision_id TEXT REFERENCES design_revisions(design_revision_id),
    component_batch_id TEXT REFERENCES component_batches(component_batch_id),
    software_name TEXT,
    software_version TEXT,
    calibration_instrument TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'withdrawn')),
    issued_at TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdraw_reason TEXT,
    UNIQUE (content_sha256),
    FOREIGN KEY (test_protocol_id, test_protocol_version)
        REFERENCES test_protocols(test_protocol_id, version)
);

-- 设计版本对部件批次的装机构成（资格按部件批次举证时用）
CREATE TABLE IF NOT EXISTS design_components (
    design_revision_id TEXT NOT NULL REFERENCES design_revisions(design_revision_id),
    component_batch_id TEXT NOT NULL REFERENCES component_batches(component_batch_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY (design_revision_id, component_batch_id)
);

-- 资格记录：针对设计版本，按能力(capability)逐条授权，携带环境包络。
-- status: qualified（有效）/ withdrawn（试验撤回）/ superseded（被新版本取代）
CREATE TABLE IF NOT EXISTS qualifications (
    qualification_id TEXT PRIMARY KEY,
    design_revision_id TEXT NOT NULL REFERENCES design_revisions(design_revision_id),
    capability TEXT NOT NULL,
    envelope_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'qualified'
        CHECK (status IN ('qualified', 'withdrawn', 'superseded')),
    valid_from TEXT NOT NULL,
    valid_to TEXT,
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    withdrawn_at TEXT,
    withdraw_reason TEXT,
    superseded_by TEXT
);

-- 资格 -> 证据的多对多依据。批准时快照证据摘要；
-- 一条证据事后撤回，凭快照仍能解释当时的批准。
CREATE TABLE IF NOT EXISTS qualification_basis (
    qualification_id TEXT NOT NULL REFERENCES qualifications(qualification_id),
    evidence_id TEXT NOT NULL REFERENCES qualification_evidence(evidence_id),
    evidence_sha256 TEXT NOT NULL CHECK (length(evidence_sha256) = 64),
    PRIMARY KEY (qualification_id, evidence_id)
);

-- 偏差处置（临时豁免/限制），按能力收敛到资格上
CREATE TABLE IF NOT EXISTS deviations (
    deviation_id TEXT PRIMARY KEY,
    design_revision_id TEXT NOT NULL REFERENCES design_revisions(design_revision_id),
    capability TEXT,
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    restriction_json TEXT,
    justification TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_to TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    requested_by TEXT NOT NULL REFERENCES users(user_id),
    reviewed_by TEXT REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    closed_at TEXT
);

-- 作业放行门禁。release_key 使同一放行请求只能成功一次。
-- status: released（已放行，作业尚未执行）/ consumed（作业已完成）/ voided（依据失效，撤回或召回）
CREATE TABLE IF NOT EXISTS operation_releases (
    release_id TEXT PRIMARY KEY,
    release_key TEXT NOT NULL UNIQUE,
    equipment_serial TEXT NOT NULL,
    design_revision_id TEXT NOT NULL REFERENCES design_revisions(design_revision_id),
    component_batch_id TEXT REFERENCES component_batches(component_batch_id),
    capability TEXT NOT NULL,
    depth_m REAL NOT NULL,
    temperature_c REAL NOT NULL,
    pressure_mpa REAL NOT NULL,
    phase TEXT NOT NULL,
    software_version TEXT,
    qualification_id TEXT NOT NULL REFERENCES qualifications(qualification_id),
    qualification_sha256 TEXT NOT NULL CHECK (length(qualification_sha256) = 64),
    basis_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'released' CHECK (status IN ('released', 'consumed', 'voided')),
    requested_by TEXT NOT NULL REFERENCES users(user_id),
    released_at TEXT NOT NULL,
    consumed_at TEXT,
    voided_at TEXT,
    void_reason TEXT,
    deviation_id TEXT REFERENCES deviations(deviation_id)
);

-- 放行幂等：重放完全相同的放行请求直接返回原结果；
-- 不同请求复用同一键则冲突。
CREATE TABLE IF NOT EXISTS release_idempotency (
    release_key TEXT PRIMARY KEY,
    request_sha256 TEXT NOT NULL CHECK (length(request_sha256) = 64),
    release_id TEXT NOT NULL REFERENCES operation_releases(release_id),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_qualifications_design ON qualifications(design_revision_id);
CREATE INDEX IF NOT EXISTS idx_releases_design ON operation_releases(design_revision_id);
CREATE INDEX IF NOT EXISTS idx_evidence_kind ON qualification_evidence(evidence_kind);
CREATE INDEX IF NOT EXISTS idx_audit_entity ON audit_events(entity_type, entity_id);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "equipment_models", "design_revisions", "component_batches",
    "test_protocols", "qualification_evidence", "design_components", "qualifications",
    "qualification_basis", "deviations", "operation_releases", "release_idempotency",
    "audit_events",
})


def connect(path: str | Path = ":memory:", *, check_same_thread: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(
        str(path), isolation_level=None, check_same_thread=check_same_thread)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    return {
        "tables": tables,
        "missing_tables": sorted(REQUIRED_TABLES - set(tables)),
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(connection.execute("PRAGMA foreign_keys").fetchone()[0]),
    }

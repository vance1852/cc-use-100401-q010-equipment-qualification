"""装备资格服务的 SQLite 模式与事务辅助。"""

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
    role TEXT NOT NULL CHECK (role IN ('equipment_engineer', 'test_engineer', 'approver', 'operations', 'auditor')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE IF NOT EXISTS design_versions (
    design_version_id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (family, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS software_versions (
    software_version_id TEXT PRIMARY KEY,
    family TEXT NOT NULL,
    version TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    UNIQUE (family, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS component_batches (
    component_batch_id TEXT PRIMARY KEY,
    component_type TEXT NOT NULL,
    batch_no TEXT NOT NULL,
    manufacturer TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'recalled')),
    recalled_at TEXT,
    recalled_by TEXT,
    recall_reason TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (component_type, batch_no)
);

CREATE TABLE IF NOT EXISTS equipment_units (
    equipment_id TEXT PRIMARY KEY,
    equipment_name TEXT NOT NULL,
    family TEXT NOT NULL,
    serial_no TEXT NOT NULL,
    design_version_id TEXT REFERENCES design_versions(design_version_id),
    software_version_id TEXT REFERENCES software_versions(software_version_id),
    config_revision INTEGER NOT NULL DEFAULT 0 CHECK (config_revision >= 0),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (family, serial_no)
);

CREATE TABLE IF NOT EXISTS equipment_config_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id TEXT NOT NULL REFERENCES equipment_units(equipment_id),
    config_revision INTEGER NOT NULL,
    design_version_id TEXT NOT NULL REFERENCES design_versions(design_version_id),
    software_version_id TEXT NOT NULL REFERENCES software_versions(software_version_id),
    configured_by TEXT NOT NULL REFERENCES users(user_id),
    configured_at TEXT NOT NULL,
    UNIQUE (equipment_id, config_revision)
);

CREATE TABLE IF NOT EXISTS equipment_components (
    installation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id TEXT NOT NULL REFERENCES equipment_units(equipment_id),
    component_batch_id TEXT NOT NULL REFERENCES component_batches(component_batch_id),
    installed_by TEXT NOT NULL REFERENCES users(user_id),
    installed_at TEXT NOT NULL,
    removed_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_current_installation_per_batch
ON equipment_components(equipment_id, component_batch_id)
WHERE removed_at IS NULL;

CREATE TABLE IF NOT EXISTS test_protocols (
    protocol_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    title TEXT NOT NULL,
    capabilities_json TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    created_at TEXT NOT NULL,
    PRIMARY KEY (protocol_id, version),
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS test_evidence (
    evidence_id INTEGER PRIMARY KEY AUTOINCREMENT,
    protocol_id TEXT NOT NULL,
    protocol_version INTEGER NOT NULL,
    design_version_id TEXT NOT NULL REFERENCES design_versions(design_version_id),
    results_json TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    tested_at TEXT NOT NULL,
    test_site TEXT NOT NULL,
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'withdrawn')),
    withdrawn_at TEXT,
    withdrawn_by TEXT,
    withdraw_reason TEXT,
    UNIQUE (content_sha256),
    FOREIGN KEY (protocol_id, protocol_version) REFERENCES test_protocols(protocol_id, version)
);

CREATE TABLE IF NOT EXISTS calibration_evidence (
    calibration_id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id TEXT NOT NULL REFERENCES equipment_units(equipment_id),
    instrument TEXT NOT NULL,
    calibrated_at TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    canonical_json TEXT NOT NULL,
    content_sha256 TEXT NOT NULL CHECK (length(content_sha256) = 64),
    submitted_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'valid' CHECK (status IN ('valid', 'withdrawn')),
    withdrawn_at TEXT,
    withdrawn_by TEXT,
    withdraw_reason TEXT,
    UNIQUE (content_sha256)
);

CREATE TABLE IF NOT EXISTS qualification_grants (
    grant_id INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id TEXT NOT NULL REFERENCES equipment_units(equipment_id),
    capability TEXT NOT NULL CHECK (capability IN ('water_depth', 'temperature', 'pressure', 'phase', 'environment')),
    envelope_json TEXT NOT NULL,
    design_version_id TEXT NOT NULL REFERENCES design_versions(design_version_id),
    software_version_id TEXT NOT NULL REFERENCES software_versions(software_version_id),
    basis_json TEXT NOT NULL,
    basis_sha256 TEXT NOT NULL CHECK (length(basis_sha256) = 64),
    approved_by TEXT NOT NULL REFERENCES users(user_id),
    approved_at TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT,
    UNIQUE (equipment_id, capability, basis_sha256)
);

CREATE TABLE IF NOT EXISTS grant_evidence (
    grant_id INTEGER NOT NULL REFERENCES qualification_grants(grant_id),
    evidence_id INTEGER NOT NULL REFERENCES test_evidence(evidence_id),
    PRIMARY KEY (grant_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS grant_calibrations (
    grant_id INTEGER NOT NULL REFERENCES qualification_grants(grant_id),
    calibration_id INTEGER NOT NULL REFERENCES calibration_evidence(calibration_id),
    PRIMARY KEY (grant_id, calibration_id)
);

CREATE TABLE IF NOT EXISTS grant_components (
    grant_id INTEGER NOT NULL REFERENCES qualification_grants(grant_id),
    component_batch_id TEXT NOT NULL REFERENCES component_batches(component_batch_id),
    PRIMARY KEY (grant_id, component_batch_id)
);

CREATE TABLE IF NOT EXISTS operations (
    operation_id TEXT PRIMARY KEY,
    well_id TEXT NOT NULL,
    phase TEXT NOT NULL,
    environment TEXT NOT NULL,
    water_depth_m TEXT NOT NULL,
    temperature_c TEXT NOT NULL,
    pressure_mpa TEXT NOT NULL,
    planned_start TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'planned' CHECK (state IN ('planned', 'completed', 'cancelled')),
    created_by TEXT NOT NULL REFERENCES users(user_id),
    created_at TEXT NOT NULL,
    completed_at TEXT,
    cancelled_at TEXT
);

CREATE TABLE IF NOT EXISTS deviations (
    deviation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    equipment_id TEXT NOT NULL REFERENCES equipment_units(equipment_id),
    capability TEXT NOT NULL CHECK (capability IN ('water_depth', 'temperature', 'pressure', 'phase', 'environment')),
    demand_json TEXT NOT NULL,
    justification TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'requested' CHECK (status IN ('requested', 'approved', 'rejected', 'used')),
    requested_by TEXT NOT NULL REFERENCES users(user_id),
    requested_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES users(user_id),
    reviewed_at TEXT,
    review_note TEXT,
    expires_at TEXT
);

CREATE UNIQUE INDEX IF NOT EXISTS one_open_deviation_per_gap
ON deviations(operation_id, equipment_id, capability)
WHERE status IN ('requested', 'approved');

CREATE TABLE IF NOT EXISTS releases (
    release_id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation_id TEXT NOT NULL REFERENCES operations(operation_id),
    equipment_id TEXT NOT NULL REFERENCES equipment_units(equipment_id),
    basis_json TEXT NOT NULL,
    basis_sha256 TEXT NOT NULL CHECK (length(basis_sha256) = 64),
    granted_by TEXT NOT NULL REFERENCES users(user_id),
    granted_at TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'executed', 'invalidated')),
    executed_at TEXT,
    invalidated_at TEXT,
    invalidate_reason TEXT,
    UNIQUE (operation_id, equipment_id)
);

CREATE TABLE IF NOT EXISTS release_grants (
    release_id INTEGER NOT NULL REFERENCES releases(release_id),
    grant_id INTEGER NOT NULL REFERENCES qualification_grants(grant_id),
    PRIMARY KEY (release_id, grant_id)
);

CREATE TABLE IF NOT EXISTS release_evidence (
    release_id INTEGER NOT NULL REFERENCES releases(release_id),
    evidence_id INTEGER NOT NULL REFERENCES test_evidence(evidence_id),
    PRIMARY KEY (release_id, evidence_id)
);

CREATE TABLE IF NOT EXISTS release_components (
    release_id INTEGER NOT NULL REFERENCES releases(release_id),
    component_batch_id TEXT NOT NULL REFERENCES component_batches(component_batch_id),
    PRIMARY KEY (release_id, component_batch_id)
);

CREATE TABLE IF NOT EXISTS audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""

REQUIRED_TABLES = frozenset({
    "schema_meta", "users", "design_versions", "software_versions", "component_batches",
    "equipment_units", "equipment_config_history", "equipment_components", "test_protocols",
    "test_evidence", "calibration_evidence", "qualification_grants", "grant_evidence",
    "grant_calibrations", "grant_components", "operations", "deviations", "releases",
    "release_grants", "release_evidence", "release_components", "audit_events",
})


def connect(path: str | Path) -> sqlite3.Connection:
    """打开连接并启用严格的事务与外键设置。

    连接允许跨线程使用；HTTP 层用锁串行化处理，保持单写者事务语义。
    """

    connection = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


@contextlib.contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    """显式事务；异常时保证回滚。"""

    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> None:
    """初始化基础资料表，重复执行不改变已有数据。"""

    connection.executescript(SCHEMA_SQL)
    with transaction(connection, immediate=True):
        connection.execute(
            "INSERT INTO schema_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def inspect_schema(connection: sqlite3.Connection) -> dict[str, object]:
    """返回适合机器检查的数据库结构摘要。"""

    table_rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    tables = tuple(row["name"] for row in table_rows)
    version_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key='schema_version'"
    ).fetchone()
    missing = sorted(REQUIRED_TABLES - set(tables))
    foreign_keys = connection.execute("PRAGMA foreign_keys").fetchone()[0]
    return {
        "tables": tables,
        "missing_tables": missing,
        "schema_version": None if version_row is None else version_row["value"],
        "foreign_keys": bool(foreign_keys),
    }

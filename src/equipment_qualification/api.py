"""装备资格与适用边界管理的无依赖 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import sqlite3
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .errors import ServiceError, ValidationFailed
from .service import QualificationService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    """将 HTTP 路由映射到资格领域服务，便于无网络单元测试。"""

    def __init__(self, service: QualificationService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [p for p in path.split("/") if p]
        query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok", "service": "equipment-qualification"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            s = self.service

            if method == "POST" and path == "/users":
                return Response(201, s.create_user(
                    normalized.get("x-actor-id", "").strip() or payload.get("actor_id", ""),
                    payload["user_id"], payload["display_name"], payload["role"]))

            if method == "POST" and path == "/models":
                return Response(201, s.register_model(
                    self._actor(normalized), payload["model_id"], payload["model_name"]))

            if method == "POST" and path == "/design-revisions":
                return Response(201, s.register_design_revision(
                    self._actor(normalized), payload["design_revision_id"], payload["model_id"],
                    payload["design_version"], payload["content"]))

            if method == "POST" and path == "/component-batches":
                return Response(201, s.register_component_batch(
                    self._actor(normalized), payload["component_batch_id"], payload["part_number"],
                    payload["batch_version"], payload["content"]))

            if method == "POST" and len(parts) == 3 and parts[0] == "designs" and parts[2] == "components":
                return Response(201, s.attach_component(
                    self._actor(normalized), parts[1], payload["component_batch_id"]))

            if method == "POST" and path == "/test-protocols":
                return Response(201, s.publish_test_protocol(self._actor(normalized), payload))

            if method == "POST" and path == "/evidence":
                return Response(201, s.submit_evidence(
                    self._actor(normalized), payload["evidence_id"], payload["evidence_kind"],
                    payload["title"], payload["content"],
                    capability=payload.get("capability"),
                    issued_at=payload.get("issued_at"),
                    test_protocol_id=payload.get("test_protocol_id"),
                    test_protocol_version=payload.get("test_protocol_version"),
                    design_revision_id=payload.get("design_revision_id"),
                    component_batch_id=payload.get("component_batch_id"),
                    software_name=payload.get("software_name"),
                    software_version=payload.get("software_version"),
                    calibration_instrument=payload.get("calibration_instrument")))

            if method == "POST" and len(parts) == 3 and parts[0] == "evidence" and parts[2] == "withdraw":
                return Response(200, s.withdraw_evidence(
                    self._actor(normalized), parts[1], payload["reason"]))

            if method == "POST" and path == "/qualifications":
                return Response(201, s.grant_qualification(
                    self._actor(normalized), payload["qualification_id"], payload["design_revision_id"],
                    payload["capability"], payload["envelope"], payload["evidence_ids"],
                    valid_from=payload.get("valid_from"), valid_to=payload.get("valid_to")))

            if method == "GET" and len(parts) == 2 and parts[0] == "qualifications":
                return Response(200, s.get_qualification(parts[1]))

            if method == "POST" and len(parts) == 3 and parts[0] == "qualifications" and parts[2] == "withdraw":
                return Response(200, s.withdraw_qualification(
                    self._actor(normalized), parts[1], payload["reason"]))

            if method == "POST" and len(parts) == 3 and parts[0] == "qualifications" and parts[2] == "supersede":
                return Response(200, s.supersede_qualification(
                    self._actor(normalized), parts[1], payload["successor_qualification_id"]))

            if method == "POST" and path == "/deviations":
                return Response(201, s.record_deviation(
                    self._actor(normalized), payload["deviation_id"], payload["design_revision_id"],
                    payload["decision"], payload["justification"], payload["valid_to"],
                    capability=payload.get("capability"),
                    restriction=payload.get("restriction"),
                    valid_from=payload.get("valid_from")))

            if method == "POST" and len(parts) == 3 and parts[0] == "deviations" and parts[2] == "close":
                return Response(200, s.close_deviation(self._actor(normalized), parts[1]))

            if method == "POST" and len(parts) == 3 and parts[0] == "component-batches" and parts[2] == "recall":
                return Response(200, s.recall_component_batch(
                    self._actor(normalized), parts[1], payload["reason"]))

            if method == "POST" and path == "/releases":
                key = normalized.get("idempotency-key", "").strip()
                if not key:
                    raise ValidationFailed("缺少 Idempotency-Key")
                return Response(201, s.release_operation(
                    self._actor(normalized), key, payload["equipment_serial"],
                    payload["design_revision_id"], payload["capability"],
                    float(payload["depth_m"]), float(payload["temperature_c"]),
                    float(payload["pressure_mpa"]), payload["phase"],
                    component_batch_id=payload.get("component_batch_id"),
                    software_version=payload.get("software_version")))

            if method == "POST" and len(parts) == 3 and parts[0] == "releases" and parts[2] == "complete":
                return Response(200, s.complete_operation(self._actor(normalized), parts[1]))

            if method == "GET" and len(parts) == 2 and parts[0] == "releases":
                return Response(200, s.get_release(parts[1]))

            if method == "GET" and len(parts) == 3 and parts[0] == "designs" and parts[2] == "status":
                def number(name: str) -> float | None:
                    if name not in query:
                        return None
                    try:
                        return float(query[name])
                    except ValueError as exc:
                        raise ValidationFailed(f"{name} 必须是数值") from exc

                return Response(200, s.status_at(
                    self._actor(normalized), parts[1], query.get("at"),
                    equipment_serial=query.get("equipment_serial"),
                    depth_m=number("depth_m"), temperature_c=number("temperature_c"),
                    pressure_mpa=number("pressure_mpa"), phase=query.get("phase")))

            if method == "GET" and path == "/audit":
                return Response(200, {"events": s.audit_trail(
                    self._actor(normalized), query.get("entity_type"), query.get("entity_id"))})

            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    dispatch_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "EquipmentQualification/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            # 所有工作共享同一个 SQLite 连接；用锁串行化，避免并发 BEGIN 嵌套。
            with dispatch_lock:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动装备资格与适用边界管理 HTTP 服务")
    parser.add_argument("--database", type=Path, default=Path("equipment-qualification.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database, check_same_thread=False)
    application = JsonApplication(QualificationService(connection))
    server = ThreadingHTTPServer((args.host, args.port), make_handler(application))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

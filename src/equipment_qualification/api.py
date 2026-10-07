"""无第三方依赖的装备资格 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
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
    """将 HTTP 路由映射到领域服务，便于无网络单元测试。"""

    def __init__(self, service: QualificationService) -> None:
        self.service = service
        self._lock = threading.Lock()

    def handle(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        # 共享单个 SQLite 连接，串行化处理以保持单写者事务语义。
        with self._lock:
            return self._dispatch(method, target, headers, body)

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

    def _dispatch(
        self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b""
    ) -> Response:
        normalized_headers = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        query = parse_qs(parsed.query)
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            if method == "POST" and path == "/users":
                result = self.service.create_user(payload["user_id"], payload["display_name"], payload["role"])
                return Response(201, result)
            if method == "POST" and path == "/design_versions":
                result = self.service.register_design_version(
                    self._actor(normalized_headers), payload["design_version_id"], payload["family"],
                    payload["version"], payload["content_sha256"],
                )
                return Response(201, result)
            if method == "POST" and path == "/software_versions":
                result = self.service.register_software_version(
                    self._actor(normalized_headers), payload["software_version_id"], payload["family"],
                    payload["version"], payload["content_sha256"],
                )
                return Response(201, result)
            if method == "POST" and path == "/component_batches":
                result = self.service.register_component_batch(
                    self._actor(normalized_headers), payload["component_batch_id"], payload["component_type"],
                    payload["batch_no"], payload["manufacturer"],
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "component_batches" and parts[2] == "recall":
                result = self.service.recall_component(
                    self._actor(normalized_headers), parts[1], payload["reason"]
                )
                return Response(200, result)
            if method == "POST" and path == "/equipment":
                result = self.service.register_equipment(
                    self._actor(normalized_headers), payload["equipment_id"], payload["equipment_name"],
                    payload["family"], payload["serial_no"],
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "equipment":
                return Response(200, self.service.get_equipment(self._actor(normalized_headers), parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "equipment" and parts[2] == "configuration":
                result = self.service.configure_equipment(
                    self._actor(normalized_headers), parts[1], payload["design_version_id"],
                    payload["software_version_id"], int(payload["expected_config_revision"]),
                )
                return Response(200, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "equipment" and parts[2] == "components":
                result = self.service.install_component(
                    self._actor(normalized_headers), parts[1], payload["component_batch_id"]
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 4 and parts[0] == "equipment" and parts[2] == "components" and parts[3] == "remove":
                result = self.service.remove_component(
                    self._actor(normalized_headers), parts[1], payload["component_batch_id"]
                )
                return Response(200, result)
            if method == "GET" and len(parts) == 3 and parts[0] == "equipment" and parts[2] == "explain":
                target = payload.get("target") if payload else None
                if target is None and "target" in query:
                    target = json.loads(query["target"][0])
                result = self.service.explain_equipment(
                    self._actor(normalized_headers), parts[1],
                    query["as_of"][0] if "as_of" in query else None,
                    target,
                )
                return Response(200, result)
            if method == "POST" and path == "/protocols":
                return Response(201, self.service.publish_protocol(self._actor(normalized_headers), payload))
            if method == "POST" and path == "/evidence":
                return Response(201, self.service.submit_evidence(self._actor(normalized_headers), payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "evidence" and parts[2] == "withdraw":
                result = self.service.withdraw_evidence(
                    self._actor(normalized_headers), int(parts[1]), payload["reason"]
                )
                return Response(200, result)
            if method == "POST" and path == "/calibrations":
                return Response(201, self.service.submit_calibration(self._actor(normalized_headers), payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "calibrations" and parts[2] == "withdraw":
                result = self.service.withdraw_calibration(
                    self._actor(normalized_headers), int(parts[1]), payload["reason"]
                )
                return Response(200, result)
            if method == "POST" and path == "/grants":
                result = self.service.grant_qualification(
                    self._actor(normalized_headers), payload["equipment_id"], payload["capability"],
                    payload["envelope"], payload.get("evidence_ids", []),
                    payload.get("calibration_ids", []), payload.get("valid_until"),
                )
                return Response(201, result)
            if method == "POST" and path == "/operations":
                result = self.service.plan_operation(
                    self._actor(normalized_headers), payload["operation_id"], payload["well_id"],
                    payload["phase"], payload["environment"], payload["water_depth_m"],
                    payload["temperature_c"], payload["pressure_mpa"], payload["planned_start"],
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "operations" and parts[2] == "complete":
                return Response(200, self.service.complete_operation(self._actor(normalized_headers), parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "operations" and parts[2] == "cancel":
                result = self.service.cancel_operation(
                    self._actor(normalized_headers), parts[1], payload.get("reason", "")
                )
                return Response(200, result)
            if method == "POST" and path == "/deviations":
                result = self.service.request_deviation(
                    self._actor(normalized_headers), payload["operation_id"], payload["equipment_id"],
                    payload["capability"], payload["justification"],
                )
                return Response(201, result)
            if method == "POST" and len(parts) == 3 and parts[0] == "deviations" and parts[2] == "review":
                result = self.service.review_deviation(
                    self._actor(normalized_headers), int(parts[1]), bool(payload["approve"]),
                    payload.get("note", ""), payload.get("expires_at"),
                )
                return Response(200, result)
            if method == "POST" and path == "/releases":
                result = self.service.request_release(
                    self._actor(normalized_headers), payload["operation_id"], payload["equipment_id"]
                )
                return Response(201, result)
            if method == "GET" and len(parts) == 2 and parts[0] == "releases":
                return Response(200, self.service.get_release(self._actor(normalized_headers), int(parts[1])))
            if method == "GET" and len(parts) == 3 and parts[0] == "audit":
                return Response(200, {"events": self.service.audit_trail(
                    self._actor(normalized_headers), parts[1], parts[2]
                )})
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except ServiceError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "EquipmentQualification/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
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
    parser.add_argument("--database", type=Path, default=Path("equipment_qualification.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
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

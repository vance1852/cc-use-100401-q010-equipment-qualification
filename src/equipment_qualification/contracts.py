"""装备资格域的严格数据契约与包络运算。"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


class ValidationError(ValueError):
    """输入不能满足领域契约。"""


CAPABILITIES = ("water_depth", "temperature", "pressure", "phase", "environment")

CAPABILITY_LABELS = {
    "water_depth": "水深",
    "temperature": "温度",
    "pressure": "压力",
    "phase": "作业阶段",
    "environment": "环境类别",
}

PHASES = ("drilling", "completion", "logging", "testing", "production", "intervention")

ENVIRONMENTS = ("normal", "hthp", "polar")


def _require_mapping(value: object, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{path} 必须是对象")
    return value


def _require_sequence(value: object, path: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise ValidationError(f"{path} 必须是数组")
    return value


def _required_text(value: object, path: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{path} 必须是非空字符串")
    return value.strip()


def _optional_text(value: object, path: str) -> str | None:
    if value is None:
        return None
    return _required_text(value, path)


def _decimal(value: object, path: str) -> Decimal:
    if isinstance(value, bool):
        raise ValidationError(f"{path} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValidationError(f"{path} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationError(f"{path} 必须是有限数值")
    return result


def require_capability(value: object, path: str = "capability") -> str:
    capability = _required_text(value, path)
    if capability not in CAPABILITIES:
        raise ValidationError(f"{path} 不受支持: {capability}")
    return capability


def envelope_fragment(capability: str, raw: object, path: str) -> dict[str, Any]:
    """校验并规范化单项能力的包络片段，数值统一为十进制。"""

    data = _require_mapping(raw, path)
    if capability == "water_depth":
        allowed = {"max_water_depth_m"}
        depth = _decimal(data.get("max_water_depth_m"), f"{path}.max_water_depth_m")
        if depth <= 0:
            raise ValidationError(f"{path}.max_water_depth_m 必须大于零")
        fragment: dict[str, Any] = {"max_water_depth_m": depth}
    elif capability == "temperature":
        allowed = {"min_temperature_c", "max_temperature_c"}
        low = _decimal(data.get("min_temperature_c"), f"{path}.min_temperature_c")
        high = _decimal(data.get("max_temperature_c"), f"{path}.max_temperature_c")
        if low >= high:
            raise ValidationError(f"{path} 温度下限必须小于上限")
        fragment = {"min_temperature_c": low, "max_temperature_c": high}
    elif capability == "pressure":
        allowed = {"max_pressure_mpa"}
        pressure = _decimal(data.get("max_pressure_mpa"), f"{path}.max_pressure_mpa")
        if pressure <= 0:
            raise ValidationError(f"{path}.max_pressure_mpa 必须大于零")
        fragment = {"max_pressure_mpa": pressure}
    elif capability == "phase":
        allowed = {"phases"}
        phases = tuple(
            _required_text(item, f"{path}.phases[]")
            for item in _require_sequence(data.get("phases"), f"{path}.phases")
        )
        if not phases:
            raise ValidationError(f"{path}.phases 不能为空")
        unknown = sorted(set(phases) - set(PHASES))
        if unknown:
            raise ValidationError(f"{path}.phases 含未知作业阶段: {unknown}")
        if len(set(phases)) != len(phases):
            raise ValidationError(f"{path}.phases 不能重复")
        fragment = {"phases": tuple(sorted(set(phases), key=PHASES.index))}
    elif capability == "environment":
        allowed = {"environments"}
        environments = tuple(
            _required_text(item, f"{path}.environments[]")
            for item in _require_sequence(data.get("environments"), f"{path}.environments")
        )
        if not environments:
            raise ValidationError(f"{path}.environments 不能为空")
        unknown = sorted(set(environments) - set(ENVIRONMENTS))
        if unknown:
            raise ValidationError(f"{path}.environments 含未知环境类别: {unknown}")
        if len(set(environments)) != len(environments):
            raise ValidationError(f"{path}.environments 不能重复")
        fragment = {"environments": tuple(sorted(set(environments), key=ENVIRONMENTS.index))}
    else:
        raise ValidationError(f"未知能力: {capability}")
    extra = sorted(set(data) - allowed)
    if extra:
        raise ValidationError(f"{path} 含未声明字段: {extra}")
    return fragment


def fragment_contains(demonstrated: Mapping[str, Any], requested: Mapping[str, Any]) -> bool:
    """判断已验证包络是否完全覆盖申请包络（同一能力）。"""

    if "max_water_depth_m" in requested:
        if Decimal(str(requested["max_water_depth_m"])) > Decimal(str(demonstrated["max_water_depth_m"])):
            return False
    if "max_pressure_mpa" in requested:
        if Decimal(str(requested["max_pressure_mpa"])) > Decimal(str(demonstrated["max_pressure_mpa"])):
            return False
    if "min_temperature_c" in requested:
        if Decimal(str(requested["min_temperature_c"])) < Decimal(str(demonstrated["min_temperature_c"])):
            return False
        if Decimal(str(requested["max_temperature_c"])) > Decimal(str(demonstrated["max_temperature_c"])):
            return False
    if "phases" in requested:
        if not set(requested["phases"]) <= set(demonstrated["phases"]):
            return False
    if "environments" in requested:
        if not set(requested["environments"]) <= set(demonstrated["environments"]):
            return False
    return True


def merge_fragments(fragments: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """合并同一能力多份有效批准的包络，取并集。"""

    if not fragments:
        return None
    merged: dict[str, Any] = {}
    for fragment in fragments:
        if "max_water_depth_m" in fragment:
            key = "max_water_depth_m"
            value = Decimal(str(fragment[key]))
            merged[key] = max(value, merged.get(key, value))
        if "max_pressure_mpa" in fragment:
            key = "max_pressure_mpa"
            value = Decimal(str(fragment[key]))
            merged[key] = max(value, merged.get(key, value))
        if "min_temperature_c" in fragment:
            low = Decimal(str(fragment["min_temperature_c"]))
            high = Decimal(str(fragment["max_temperature_c"]))
            merged["min_temperature_c"] = min(low, merged.get("min_temperature_c", low))
            merged["max_temperature_c"] = max(high, merged.get("max_temperature_c", high))
        if "phases" in fragment:
            merged["phases"] = tuple(
                sorted(set(merged.get("phases", ())) | set(fragment["phases"]), key=PHASES.index)
            )
        if "environments" in fragment:
            merged["environments"] = tuple(
                sorted(
                    set(merged.get("environments", ())) | set(fragment["environments"]),
                    key=ENVIRONMENTS.index,
                )
            )
    return merged


@dataclass(frozen=True, slots=True)
class Protocol:
    """一份不可歧义的试验协议版本，声明可验证的能力与最低验证要求。"""

    protocol_id: str
    version: int
    title: str
    capabilities: tuple[str, ...]
    required_demonstration: Mapping[str, Mapping[str, Any]]

    @classmethod
    def from_dict(cls, raw: object) -> "Protocol":
        data = _require_mapping(raw, "protocol")
        version = data.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version <= 0:
            raise ValidationError("protocol.version 必须是正整数")
        capabilities = tuple(
            require_capability(item, "protocol.capabilities[]")
            for item in _require_sequence(data.get("capabilities"), "protocol.capabilities")
        )
        if not capabilities:
            raise ValidationError("protocol.capabilities 不能为空")
        if len(set(capabilities)) != len(capabilities):
            raise ValidationError("protocol.capabilities 不能重复")
        raw_demo = _require_mapping(data.get("required_demonstration"), "protocol.required_demonstration")
        if set(raw_demo) != set(capabilities):
            raise ValidationError("protocol.required_demonstration 必须覆盖且仅覆盖已声明能力")
        demonstration = {
            capability: envelope_fragment(
                capability, raw_demo[capability], f"protocol.required_demonstration.{capability}"
            )
            for capability in capabilities
        }
        return cls(
            protocol_id=_required_text(data.get("protocol_id"), "protocol.protocol_id"),
            version=version,
            title=_required_text(data.get("title"), "protocol.title"),
            capabilities=capabilities,
            required_demonstration=demonstration,
        )


@dataclass(frozen=True, slots=True)
class CapabilityResult:
    """试验证据中单项能力的结论与已验证包络。"""

    capability: str
    outcome: str
    demonstrated: Mapping[str, Any]

    @classmethod
    def from_dict(cls, raw: object, path: str) -> "CapabilityResult":
        data = _require_mapping(raw, path)
        capability = require_capability(data.get("capability"), f"{path}.capability")
        outcome = _required_text(data.get("outcome"), f"{path}.outcome")
        if outcome not in {"pass", "fail"}:
            raise ValidationError(f"{path}.outcome 必须是 pass 或 fail")
        demonstrated = envelope_fragment(capability, data.get("demonstrated"), f"{path}.demonstrated")
        return cls(capability=capability, outcome=outcome, demonstrated=demonstrated)


@dataclass(frozen=True, slots=True)
class EvidenceSubmission:
    """一次按协议版本完成的装备试验证据。"""

    protocol_id: str
    protocol_version: int
    design_version_id: str
    results: tuple[CapabilityResult, ...]
    tested_at: str
    test_site: str

    @classmethod
    def from_dict(cls, raw: object, protocol: Protocol) -> "EvidenceSubmission":
        data = _require_mapping(raw, "evidence")
        protocol_id = _required_text(data.get("protocol_id"), "evidence.protocol_id")
        protocol_version = data.get("protocol_version")
        if protocol_id != protocol.protocol_id or protocol_version != protocol.version:
            raise ValidationError("证据引用的协议版本与当前协议不一致")
        results = tuple(
            CapabilityResult.from_dict(item, f"evidence.capability_results[{index}]")
            for index, item in enumerate(
                _require_sequence(data.get("capability_results"), "evidence.capability_results")
            )
        )
        if not results:
            raise ValidationError("evidence.capability_results 不能为空")
        result_capabilities = [item.capability for item in results]
        if len(set(result_capabilities)) != len(result_capabilities):
            raise ValidationError("evidence.capability_results 能力不能重复")
        undeclared = sorted(set(result_capabilities) - set(protocol.capabilities))
        if undeclared:
            raise ValidationError(f"证据包含协议未声明的能力: {undeclared}")
        for item in results:
            if item.outcome != "pass":
                continue
            required = protocol.required_demonstration[item.capability]
            if not fragment_contains(item.demonstrated, required):
                raise ValidationError(
                    f"能力 {item.capability} 的已验证包络未达到协议最低要求"
                )
        return cls(
            protocol_id=protocol_id,
            protocol_version=protocol.version,
            design_version_id=_required_text(data.get("design_version_id"), "evidence.design_version_id"),
            results=results,
            tested_at=_required_text(data.get("tested_at"), "evidence.tested_at"),
            test_site=_required_text(data.get("test_site"), "evidence.test_site"),
        )


@dataclass(frozen=True, slots=True)
class OperationDemand:
    """一次作业对装备资格包络的完整需求。"""

    water_depth_m: Decimal
    temperature_c: Decimal
    pressure_mpa: Decimal
    phase: str
    environment: str

    @classmethod
    def from_values(
        cls,
        water_depth_m: object,
        temperature_c: object,
        pressure_mpa: object,
        phase: object,
        environment: object,
    ) -> "OperationDemand":
        depth = _decimal(water_depth_m, "operation.water_depth_m")
        if depth <= 0:
            raise ValidationError("operation.water_depth_m 必须大于零")
        pressure = _decimal(pressure_mpa, "operation.pressure_mpa")
        if pressure <= 0:
            raise ValidationError("operation.pressure_mpa 必须大于零")
        phase_text = _required_text(phase, "operation.phase")
        if phase_text not in PHASES:
            raise ValidationError(f"operation.phase 不受支持: {phase_text}")
        environment_text = _required_text(environment, "operation.environment")
        if environment_text not in ENVIRONMENTS:
            raise ValidationError(f"operation.environment 不受支持: {environment_text}")
        return cls(
            water_depth_m=depth,
            temperature_c=_decimal(temperature_c, "operation.temperature_c"),
            pressure_mpa=pressure,
            phase=phase_text,
            environment=environment_text,
        )

    def requirement(self, capability: str) -> dict[str, Any]:
        """该作业对单项能力的包络需求片段。"""

        if capability == "water_depth":
            return {"max_water_depth_m": self.water_depth_m}
        if capability == "temperature":
            return {"min_temperature_c": self.temperature_c, "max_temperature_c": self.temperature_c}
        if capability == "pressure":
            return {"max_pressure_mpa": self.pressure_mpa}
        if capability == "phase":
            return {"phases": (self.phase,)}
        if capability == "environment":
            return {"environments": (self.environment,)}
        raise ValidationError(f"未知能力: {capability}")

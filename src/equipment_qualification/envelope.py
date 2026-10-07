"""环境包络与作业阶段的纯领域逻辑。

装备在某一井况下能否使用，本质上是一个点（或一个需求区间）是否落在
已批准环境包络内。这里只做可复算的数学判定，不触碰数据库。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

# 统一作业阶段顺序；资格按能力部分通过时即按阶段粒度授权。
OPERATION_PHASES = (
    "descent",        # 下入
    "logging",        # 测井/随钻测量
    "sampling",       # 取样
    "testing",        # 试井测试
    "stimulation",    # 压裂/酸化等增产
    "cementing",      # 固井
    "completion",     # 完井
    "ascent",         # 起出
)


@dataclass(frozen=True, slots=True)
class Interval:
    min_value: float
    max_value: float
    unit: str

    @staticmethod
    def from_dict(raw: Mapping[str, Any], prefix: str, unit: str) -> "Interval":
        try:
            low = float(raw[f"{prefix}_min"])
            high = float(raw[f"{prefix}_max"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{prefix} 边界必须是数值") from exc
        if not (math.isfinite(low) and math.isfinite(high)):
            raise ValueError(f"{prefix} 边界必须有限")
        if low > high:
            raise ValueError(f"{prefix}_min 不能大于 {prefix}_max")
        return Interval(low, high, raw.get(f"{prefix}_unit", unit) or unit)

    def contains(self, value: float) -> bool:
        return self.min_value <= float(value) <= self.max_value

    def covers(self, other: "Interval") -> bool:
        """闭区间包含；单位不同时拒绝，避免静默错配。"""

        if self.unit != other.unit:
            raise ValueError(f"单位不一致: {self.unit} != {other.unit}")
        return self.min_value <= other.min_value and other.max_value <= self.max_value

    def intersects(self, other: "Interval") -> bool:
        if self.unit != other.unit:
            raise ValueError(f"单位不一致: {self.unit} != {other.unit}")
        return self.min_value <= other.max_value and other.min_value <= self.max_value

    def as_dict(self) -> dict[str, Any]:
        return {"min": self.min_value, "max": self.max_value, "unit": self.unit}


@dataclass(frozen=True, slots=True)
class Envelope:
    """批准的环境包络：水深、温度、压力三个闭区间加可作业阶段。"""

    depth: Interval
    temperature: Interval
    pressure: Interval
    phases: frozenset[str]

    @staticmethod
    def from_dict(raw: Mapping[str, Any]) -> "Envelope":
        depth = Interval.from_dict(raw["depth"], "depth", "m")
        temperature = Interval.from_dict(raw["temperature"], "temperature", "degC")
        pressure = Interval.from_dict(raw["pressure"], "pressure", "MPa")
        phases = raw.get("phases") or []
        if not isinstance(phases, (list, tuple)) or not phases:
            raise ValueError("至少指定一个作业阶段")
        invalid = [p for p in phases if p not in OPERATION_PHASES]
        if invalid:
            raise ValueError(f"未知作业阶段: {invalid}")
        if len(set(phases)) != len(phases):
            raise ValueError("作业阶段重复")
        return Envelope(depth, temperature, pressure, frozenset(phases))

    def covers_point(
        self, depth_m: float, temperature_c: float, pressure_mpa: float, phase: str | None = None
    ) -> bool:
        if not (self.depth.contains(depth_m) and self.temperature.contains(temperature_c) and self.pressure.contains(pressure_mpa)):
            return False
        return phase is None or phase in self.phases

    def covers(self, other: "Envelope") -> bool:
        """三维区间完全包含且阶段集合包含。"""

        return (
            self.depth.covers(other.depth)
            and self.temperature.covers(other.temperature)
            and self.pressure.covers(other.pressure)
            and other.phases <= self.phases
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "depth": {"depth_min": self.depth.min_value, "depth_max": self.depth.max_value,
                      "depth_unit": self.depth.unit},
            "temperature": {"temperature_min": self.temperature.min_value,
                            "temperature_max": self.temperature.max_value,
                            "temperature_unit": self.temperature.unit},
            "pressure": {"pressure_min": self.pressure.min_value,
                         "pressure_max": self.pressure.max_value,
                         "pressure_unit": self.pressure.unit},
            "phases": sorted(self.phases),
        }

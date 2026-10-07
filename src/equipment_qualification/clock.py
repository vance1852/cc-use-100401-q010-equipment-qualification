"""可注入时间源，保证资格判定与时间旅行解释的确定性。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


@dataclass
class FrozenClock:
    current: datetime

    def now(self) -> datetime:
        if self.current.tzinfo is None:
            raise ValueError("冻结时钟必须带时区")
        return self.current

    def advance(self, **kwargs: float) -> None:
        self.current += timedelta(**kwargs)


def isoformat(value: datetime) -> str:
    if value.tzinfo is None:
        raise ValueError("时间必须带时区")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_at(value: str | None, fallback: datetime | None = None) -> datetime:
    """解析 ISO-8601 字符串；空值返回 fallback（必须带时区）。"""

    if value is None or value == "":
        if fallback is None:
            raise ValueError("缺少时间")
        return fallback.astimezone(timezone.utc)
    text = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("时间必须带时区")
    return parsed.astimezone(timezone.utc)

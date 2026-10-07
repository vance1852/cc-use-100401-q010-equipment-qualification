from __future__ import annotations

import math
import unittest

from equipment_qualification.envelope import OPERATION_PHASES, Envelope, Interval


class IntervalTests(unittest.TestCase):
    def test_contains_and_covers(self) -> None:
        shallow = Interval(0, 500, "m")
        zone = Interval(100, 400, "m")
        self.assertTrue(shallow.covers(zone))
        self.assertFalse(zone.covers(shallow))
        self.assertTrue(shallow.contains(500))  # 闭区间

    def test_unit_mismatch_rejected(self) -> None:
        with self.assertRaises(ValueError):
            Interval(0, 1, "MPa").covers(Interval(0, 1, "bar"))

    def test_invalid_bounds(self) -> None:
        with self.assertRaises(ValueError):
            Interval.from_dict({"depth_min": 10, "depth_max": 1}, "depth", "m")
        with self.assertRaises(ValueError):
            Interval.from_dict({"depth_min": math.nan, "depth_max": 1}, "depth", "m")


class EnvelopeTests(unittest.TestCase):
    def envelope(self) -> Envelope:
        return Envelope.from_dict({
            "depth": {"depth_min": 0, "depth_max": 1500, "depth_unit": "m"},
            "temperature": {"temperature_min": -2, "temperature_max": 80, "temperature_unit": "degC"},
            "pressure": {"pressure_min": 0, "pressure_max": 105, "pressure_unit": "MPa"},
            "phases": ["descent", "logging", "sampling", "ascent"],
        })

    def test_point_in_envelope(self) -> None:
        env = self.envelope()
        self.assertTrue(env.covers_point(1200, 60, 88, "logging"))
        self.assertFalse(env.covers_point(2200, 60, 88, "logging"))   # 超水深
        self.assertFalse(env.covers_point(1200, 150, 88, "logging"))  # 超温度
        self.assertFalse(env.covers_point(1200, 60, 88, "cementing")) # 阶段未批准

    def test_phase_validation(self) -> None:
        with self.assertRaises(ValueError):
            Envelope.from_dict({
                "depth": {"depth_min": 0, "depth_max": 1, "depth_unit": "m"},
                "temperature": {"temperature_min": 0, "temperature_max": 1, "temperature_unit": "degC"},
                "pressure": {"pressure_min": 0, "pressure_max": 1, "pressure_unit": "MPa"},
                "phases": ["moon-mining"],
            })
        self.assertEqual(len(OPERATION_PHASES), 8)


if __name__ == "__main__":
    unittest.main()

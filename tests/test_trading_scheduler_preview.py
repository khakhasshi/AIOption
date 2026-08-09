from __future__ import annotations

import unittest
from datetime import datetime

from ai_option_scanner import trading_scheduler
from ai_option_scanner.time_utils import EASTERN


class TradingSchedulerPreviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._orig_now_et = trading_scheduler.now_et
        self._orig_next_trading_day = trading_scheduler.next_nyse_trading_day
        trading_scheduler.next_nyse_trading_day = lambda value: value

    def tearDown(self) -> None:
        trading_scheduler.now_et = self._orig_now_et
        trading_scheduler.next_nyse_trading_day = self._orig_next_trading_day

    def test_multi_instance_preview_uses_first_future_slot(self) -> None:
        trading_scheduler.now_et = lambda: datetime(2026, 5, 13, 5, 47, tzinfo=EASTERN)

        preview = trading_scheduler.next_config_run_preview(_multi_slot_config())

        self.assertEqual(preview["next_run_mode"], "multi_slot")
        self.assertEqual(preview["next_run_at_et"], "2026-05-13T09:45:00-04:00")
        self.assertEqual(preview["next_run_slot"]["slot_id"], "open_confirmation")

    def test_multi_instance_preview_advances_to_next_slot_after_first_slot(self) -> None:
        trading_scheduler.now_et = lambda: datetime(2026, 5, 13, 10, 45, tzinfo=EASTERN)

        preview = trading_scheduler.next_config_run_preview(_multi_slot_config())

        self.assertEqual(preview["next_run_at_et"], "2026-05-13T12:45:00-04:00")
        self.assertEqual(preview["next_run_slot"]["slot_id"], "midday_structure")

    def test_single_run_preview_still_uses_run_time(self) -> None:
        trading_scheduler.now_et = lambda: datetime(2026, 5, 13, 5, 47, tzinfo=EASTERN)

        preview = trading_scheduler.next_config_run_preview({"multi_instance_enabled": False, "run_time_et": "10:45"})

        self.assertEqual(preview["next_run_mode"], "single_run")
        self.assertEqual(preview["next_run_at_et"], "2026-05-13T10:45:00-04:00")


def _multi_slot_config() -> dict:
    return {
        "multi_instance_enabled": True,
        "run_time_et": "10:45",
        "schedule_slots": [
            {"slot_id": "open_confirmation", "label": "开盘确认", "time_et": "09:45", "enabled": True},
            {"slot_id": "midday_structure", "label": "中盘结构", "time_et": "12:45", "enabled": True},
            {"slot_id": "power_hour_risk", "label": "尾盘风控", "time_et": "15:10", "enabled": True},
        ],
    }

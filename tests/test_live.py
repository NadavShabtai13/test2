"""Live module tests (market-hours guard, broker fill wait)."""
from __future__ import annotations

import datetime as dt
import sys
from unittest.mock import MagicMock, patch

from sptrader.live.broker import IBKRBroker
from sptrader.live.risk import (
    RiskConfig,
    clamp_position,
    sleeve_quantity,
    target_position,
)
from sptrader.live.runner import is_us_market_open
from sptrader.live.tradelog import TradeLogger


def _utc(y, m, d, h, mi=0):
    return dt.datetime(y, m, d, h, mi, tzinfo=dt.timezone.utc)


class TestIsUsMarketOpen:
    def test_edt_mid_session_open(self):
        # 2026-06-10 Wed: 14:00 UTC = 10:00 ET (EDT)
        assert is_us_market_open(_utc(2026, 6, 10, 14)) is True

    def test_edt_before_open(self):
        # 13:00 UTC = 09:00 ET
        assert is_us_market_open(_utc(2026, 6, 10, 13)) is False

    def test_edt_after_close(self):
        # 20:30 UTC = 16:30 ET
        assert is_us_market_open(_utc(2026, 6, 10, 20, 30)) is False

    def test_est_mid_session_open(self):
        # 2026-01-15 Thu: 15:00 UTC = 10:00 ET (EST)
        assert is_us_market_open(_utc(2026, 1, 15, 15)) is True

    def test_est_before_open_fixed_utc_window_would_be_wrong(self):
        # 14:00 UTC = 09:00 ET — old 13:30 UTC guard treated this as open
        assert is_us_market_open(_utc(2026, 1, 15, 14)) is False

    def test_est_late_session_old_utc_window_would_close_early(self):
        # 20:30 UTC = 15:30 ET — old guard stopped at 20:00 UTC
        assert is_us_market_open(_utc(2026, 1, 15, 20, 30)) is True

    def test_weekend_closed(self):
        assert is_us_market_open(_utc(2026, 6, 13, 15)) is False  # Saturday

    def test_israel_summer_open_is_1330_utc(self):
        # 16:30 IDT (UTC+3) = 13:30 UTC = 09:30 ET
        assert is_us_market_open(_utc(2026, 6, 10, 13, 30)) is True


def _broker_with_mock_ib(trade: MagicMock) -> IBKRBroker:
    ib = MagicMock()
    ib.positions.return_value = []
    ib.placeOrder.return_value = trade
    broker = IBKRBroker.__new__(IBKRBroker)
    broker.ib = ib
    return broker


class TestIBKRBrokerFillWait:
    def test_waits_until_trade_done(self):
        trade = MagicMock()
        trade.isDone.side_effect = [False, False, True]
        trade.orderStatus.status = "Filled"
        broker = _broker_with_mock_ib(trade)

        mock_ib = MagicMock()
        mock_ib.MarketOrder = MagicMock(side_effect=lambda action, qty: MagicMock())
        with patch.dict(sys.modules, {"ib_insync": mock_ib}):
            out = broker.set_target("SPY", 10)

        assert trade.isDone.call_count == 3
        assert broker.ib.sleep.call_count == 2
        assert out["status"] == "Filled"
        assert out["filled"] is True

    def test_times_out_without_fill(self):
        trade = MagicMock()
        trade.isDone.return_value = False
        trade.orderStatus.status = "Submitted"
        broker = _broker_with_mock_ib(trade)

        mock_ib = MagicMock()
        mock_ib.MarketOrder = MagicMock(side_effect=lambda action, qty: MagicMock())
        with patch.dict(sys.modules, {"ib_insync": mock_ib}):
            with patch.dict("os.environ", {"IBKR_FILL_TIMEOUT": "0"}):
                out = broker.set_target("SPY", 5)

        assert out["status"] == "Submitted"
        assert out["filled"] is False


def _decision(prev, target, owner="auto-1", price=100.0, ts="2026-06-10T14:30:00+00:00"):
    return {
        "ts": ts,
        "mode": "dry-run",
        "combine": "priority",
        "symbol": "SPY",
        "owner": owner if target != 0 else None,
        "sleeves": [
            {"strategy": "auto-1", "label": "ema_cross(fast=5,slow=500)", "price": price}
        ],
        "net_target": None,
        "target_qty": target,
        "risk": "ok",
        "prev_position": prev,
        "order": {"action": "BUY", "filled": True, "status": "Filled"},
    }


class TestTradeLogger:
    def test_writes_jsonl_and_ledger_on_open(self, tmp_path):
        log = TradeLogger(log_dir=str(tmp_path))
        log.decision(_decision(prev=0, target=10))

        jsonl = list(tmp_path.glob("live-*.jsonl"))
        ledger = list(tmp_path.glob("trades-*.log"))
        assert len(jsonl) == 1 and len(ledger) == 1
        assert "OPEN  LONG 10 SPY @ 100.0" in ledger[0].read_text()
        assert "ema_cross(fast=5,slow=500)" in ledger[0].read_text()

    def test_open_then_close_reports_round_trip(self, tmp_path):
        log = TradeLogger(log_dir=str(tmp_path))
        log.decision(_decision(prev=0, target=10, price=100.0))
        log.decision(
            _decision(prev=10, target=0, owner=None, price=105.0,
                      ts="2026-06-10T16:30:00+00:00")
        )
        text = list(tmp_path.glob("trades-*.log"))[0].read_text()
        assert "CLOSE LONG SPY @ 105.0" in text
        assert "entry=100.0" in text
        assert "move=+5.00%" in text
        assert "held=2h00m" in text

    def test_short_close_pnl_sign(self, tmp_path):
        log = TradeLogger(log_dir=str(tmp_path))
        log.decision(_decision(prev=0, target=-10, price=100.0))
        log.decision(_decision(prev=-10, target=0, owner=None, price=95.0))
        text = list(tmp_path.glob("trades-*.log"))[0].read_text()
        assert "move=+5.00%" in text  # short + price down = profit

    def test_no_ledger_event_when_position_unchanged(self, tmp_path):
        log = TradeLogger(log_dir=str(tmp_path))
        log.decision(_decision(prev=10, target=10))
        ledger = list(tmp_path.glob("trades-*.log"))
        assert ledger == []  # only the jsonl snapshot, no trade event


class TestRiskHelpers:
    def test_sleeve_quantity_long(self):
        assert sleeve_quantity(1.0, RiskConfig(order_qty=10)) == 10

    def test_sleeve_quantity_kill_switch_zeroes(self):
        assert sleeve_quantity(1.0, RiskConfig(order_qty=10, kill_switch=True)) == 0

    def test_sleeve_quantity_short_ban_zeroes(self):
        assert sleeve_quantity(-1.0, RiskConfig(order_qty=10, allow_short=False)) == 0

    def test_sleeve_quantity_short_allowed(self):
        assert sleeve_quantity(-1.0, RiskConfig(order_qty=10, allow_short=True)) == -10

    def test_clamp_within_limit_ok(self):
        qty, reason = clamp_position(10, RiskConfig(max_position=10))
        assert qty == 10 and reason == "ok"

    def test_clamp_above_limit(self):
        qty, reason = clamp_position(30, RiskConfig(max_position=10))
        assert qty == 10 and "capped" in reason

    def test_clamp_below_negative_limit(self):
        qty, reason = clamp_position(-30, RiskConfig(max_position=10))
        assert qty == -10 and "capped" in reason

    def test_target_position_combines_size_and_clamp(self):
        # order_qty 30 but max_position 10 -> long signal clamps to 10
        qty, reason = target_position(1.0, RiskConfig(order_qty=30, max_position=10))
        assert qty == 10 and "capped" in reason

    def test_target_position_kill_switch(self):
        qty, reason = target_position(1.0, RiskConfig(kill_switch=True))
        assert qty == 0 and "kill_switch" in reason

"""账本测试：开户、买卖入账、成本、盈亏、不变量、幂等、回滚、结算。"""

from __future__ import annotations

import json
from datetime import datetime

import pytest

from mai_stock_sim.market.calendar import TZ
from mai_stock_sim.trading.account import AccountStore


def _fill_time(day: str, hour: int = 10) -> datetime:
    return datetime.fromisoformat(day).replace(hour=hour, tzinfo=TZ)


class TestAccountBasics:
    """开户与不变量。"""

    def test_open_account(self, tmp_store: AccountStore) -> None:
        assert tmp_store.get_cash() == 1_000_000.0
        assert tmp_store.get_initial_cash() == 1_000_000.0
        assert tmp_store.get_positions() == []
        # 重复开户不重置
        assert not tmp_store.ensure_account(5_000_000.0)
        assert tmp_store.get_cash() == 1_000_000.0

    def test_initial_cash_config_does_not_reset(self, tmp_path) -> None:
        store = AccountStore(tmp_path / "a.db", initial_cash=1_000_000.0)
        store.ensure_account()
        store.close()
        # 用不同 initial_cash 重新打开：账户不变
        store2 = AccountStore(tmp_path / "a.db", initial_cash=9_999_999.0)
        store2.ensure_account(9_999_999.0)
        assert store2.get_cash() == 1_000_000.0
        store2.close()

    def test_invariants_after_trades(self, tmp_store: AccountStore) -> None:
        prices = {"600519": 100.0}
        assert tmp_store.verify_invariants(lambda symbol: prices.get(symbol)) == []
        tmp_store.apply_fill("o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 0.1, "tencent", 0.0, _fill_time("2026-11-05"))
        assert tmp_store.verify_invariants(lambda symbol: prices.get(symbol)) == []


class TestBuySell:
    """买卖入账与成本。"""

    def test_buy_debits_cash(self, tmp_store: AccountStore) -> None:
        fill_id = tmp_store.apply_fill(
            "o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 10.0, _fill_time("2026-11-05")
        )
        assert fill_id
        # 现金 = 1000000 - 1000*100 - (5+1) = 899994
        assert tmp_store.get_cash() == pytest.approx(899_994.0)
        position = tmp_store.get_position("600519")
        assert position is not None and position["qty"] == 1000
        # 成本含费用：(100000+6)/1000
        assert position["avg_cost"] == pytest.approx(100.006)

    def test_insufficient_cash_rejected(self, tmp_store: AccountStore) -> None:
        with pytest.raises(ValueError, match="现金不足"):
            tmp_store.apply_fill("o1", "d1", "600519", "贵州茅台", "BUY", 1_000_000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-05"))
        # 回滚后现金与持仓不变
        assert tmp_store.get_cash() == 1_000_000.0
        assert tmp_store.get_positions() == []

    def test_sell_credits_cash_and_realized_pnl(self, tmp_store: AccountStore) -> None:
        tmp_store.apply_fill("o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-05"))
        tmp_store.apply_fill("o2", "d1", "600519", "贵州茅台", "SELL", 1000, 110.0, 5.0, 5.5, 1.1, "tencent", 0.0, _fill_time("2026-11-06"))
        # 卖出所得 = 1000*110 - 11.6 = 109988.4
        assert tmp_store.get_cash() == pytest.approx(1_000_000.0 - 100_006.0 + 109_988.4)
        # 已实现盈亏 = 109988.4 - 1000*100.006 = 9982.4
        assert tmp_store.total_realized_pnl() == pytest.approx(9_982.4)
        assert tmp_store.get_positions() == []

    def test_t1_blocks_same_day_sell(self, tmp_store: AccountStore) -> None:
        tmp_store.apply_fill("o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-05"))
        assert tmp_store.sellable_qty("600519", trade_date="2026-11-05") == 0
        with pytest.raises(ValueError, match="可卖数量不足"):
            tmp_store.apply_fill("o2", "d1", "600519", "贵州茅台", "SELL", 100, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-05"))
        # 次日可卖
        assert tmp_store.sellable_qty("600519", trade_date="2026-11-06") == 1000

    def test_sell_more_than_holdings_rejected(self, tmp_store: AccountStore) -> None:
        tmp_store.apply_fill("o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-05"))
        with pytest.raises(ValueError):
            tmp_store.apply_fill("o2", "d1", "600519", "贵州茅台", "SELL", 2000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-06"))

    def test_duplicate_fill_idempotent(self, tmp_store: AccountStore) -> None:
        tmp_store.apply_fill("o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-05"))
        with pytest.raises(ValueError, match="已有成交"):
            tmp_store.apply_fill("o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-05"))
        # 幂等：现金与持仓未被二次扣减
        assert tmp_store.get_cash() == pytest.approx(899_994.0)
        assert tmp_store.get_position("600519")["qty"] == 1000
        assert len(tmp_store.recent_fills()) == 1

    def test_partial_sell_fifo_lots(self, tmp_store: AccountStore) -> None:
        # 两天各买 1000 股，价格不同
        tmp_store.apply_fill("o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-04"))
        tmp_store.apply_fill("o2", "d1", "600519", "贵州茅台", "BUY", 1000, 120.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-05"))
        # 11-06 可卖 2000
        assert tmp_store.sellable_qty("600519", trade_date="2026-11-06") == 2000
        # 卖出 500（FIFO 消耗最早批次）
        tmp_store.apply_fill("o3", "d1", "600519", "贵州茅台", "SELL", 500, 130.0, 5.0, 6.5, 0.65, "tencent", 0.0, _fill_time("2026-11-06"))
        assert tmp_store.get_position("600519")["qty"] == 1500
        assert tmp_store.verify_invariants(lambda symbol: 130.0) == []

    def test_cash_consistency(self, tmp_store: AccountStore) -> None:
        tmp_store.apply_fill("o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-05"))
        tmp_store.apply_fill("o2", "d1", "600519", "贵州茅台", "SELL", 400, 105.0, 5.0, 2.1, 0.42, "tencent", 0.0, _fill_time("2026-11-06"))
        initial, net, cash = tmp_store.check_profit_consistency()
        assert initial + net == pytest.approx(cash)


class TestSettlementStore:
    """结算持久化。"""

    def test_daily_equity_idempotent(self, tmp_store: AccountStore) -> None:
        assert tmp_store.save_daily_equity("2026-11-06", 900_000.0, 100_000.0, 10.0, 5.0, "[]")
        assert not tmp_store.save_daily_equity("2026-11-06", 900_000.0, 100_000.0, 10.0, 5.0, "[]")
        saved = tmp_store.get_daily_equity("2026-11-06")
        assert saved["total_equity"] == pytest.approx(1_000_000.0)

    def test_market_snapshot(self, tmp_store: AccountStore) -> None:
        tmp_store.save_market_snapshot("2026-11-06", "600519", 1453.0, "tencent")
        assert tmp_store.get_market_snapshot("2026-11-06", "600519") == 1453.0
        assert tmp_store.get_market_snapshot("2026-11-05", "600519") is None


class TestSchedulerRuns:
    """调度运行互斥。"""

    def test_run_key_mutex(self, tmp_store: AccountStore) -> None:
        assert tmp_store.try_begin_run("settle:2026-11-06")
        assert not tmp_store.try_begin_run("settle:2026-11-06")  # running 中不可重入
        tmp_store.finish_run("settle:2026-11-06")
        assert not tmp_store.try_begin_run("settle:2026-11-06")  # done 也不可重入


class TestCorporateActions:
    """公司行为。"""

    def test_pending_registration_and_block(self, tmp_store: AccountStore) -> None:
        from mai_stock_sim.trading.corporate_actions import CorporateActionGuard

        guard = CorporateActionGuard(tmp_store)
        assert guard.blocked_symbols() == []
        guard.register_pending("600519", "price_gap_review", "2026-11-06", "跳变 40%")
        assert guard.blocked_symbols() == ["600519"]

    def test_price_gap_detection(self) -> None:
        from mai_stock_sim.market.models import DailyBar
        from mai_stock_sim.trading.corporate_actions import CorporateActionGuard

        guard = CorporateActionGuard.__new__(CorporateActionGuard)
        bars = [
            DailyBar("600519", "2026-11-04", 100, 101, 99, 100.0, 1e6, 1e8, 0.0),
            DailyBar("600519", "2026-11-05", 60, 61, 59, 60.0, 1e6, 6e7, -40.0),
        ]
        assert guard.detect_price_gap(bars) is not None
        normal = [
            DailyBar("600519", "2026-11-04", 100, 101, 99, 100.0, 1e6, 1e8, 0.0),
            DailyBar("600519", "2026-11-05", 100, 103, 99, 102.0, 1e6, 1e8, 2.0),
        ]
        assert guard.detect_price_gap(normal) is None

    def test_dividend_applied_transactionally(self, tmp_store: AccountStore) -> None:
        from mai_stock_sim.trading.corporate_actions import CorporateActionGuard

        tmp_store.apply_fill("o1", "d1", "600519", "贵州茅台", "BUY", 1000, 100.0, 5.0, 0.0, 1.0, "tencent", 0.0, _fill_time("2026-11-05"))
        guard = CorporateActionGuard(tmp_store)
        assert guard.apply_dividend_cash("600519", "2026-11-06", 0.5)
        # 分红 1000*0.5 = 500
        assert tmp_store.get_cash() == pytest.approx(899_994.0 + 500.0)
        initial, net, cash = tmp_store.check_profit_consistency()
        assert initial + net == pytest.approx(cash)


class TestSuggestions:
    """建议存储。"""

    def test_insert_and_consume(self, tmp_store: AccountStore) -> None:
        from datetime import timedelta

        from mai_stock_sim.market.calendar import now_sh

        record = {
            "stream_id": "s1",
            "platform": "qq",
            "user_id": "u1",
            "user_name": "群友甲",
            "text": "我觉得中芯国际还能涨",
            "symbols": ["688981"],
            "expires_at": (now_sh() + timedelta(hours=1)).isoformat(),
        }
        suggestion_id = tmp_store.insert_suggestion(record)
        fresh = tmp_store.fresh_suggestions()
        assert len(fresh) == 1 and fresh[0]["id"] == suggestion_id
        tmp_store.mark_suggestions_consumed([suggestion_id], "dec-1")
        assert tmp_store.fresh_suggestions() == []

    def test_expired_cleanup(self, tmp_store: AccountStore) -> None:
        from datetime import timedelta

        from mai_stock_sim.market.calendar import now_sh

        tmp_store.insert_suggestion(
            {
                "stream_id": "s1",
                "text": "大盘不太好",
                "symbols": [],
                "expires_at": (now_sh() - timedelta(hours=1)).isoformat(),
            }
        )
        assert tmp_store.fresh_suggestions() == []
        assert tmp_store.cleanup_expired_suggestions() == 1

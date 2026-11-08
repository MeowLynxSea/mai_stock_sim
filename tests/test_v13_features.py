"""v1.3.0 新功能测试：研究档案、建议语义识别、事件观察、收益统计、分红送转。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict, List

import pytest

from tests.conftest import FakeQuoteService, make_quote
from mai_stock_sim.config import MaiStockSimConfig
from mai_stock_sim.market.calendar import TZ


def _buy(store, symbol: str = "600519", name: str = "贵州茅台", qty: int = 1000, price: float = 100.0, day: str = "2026-11-05") -> None:
    store.apply_fill(
        f"o-{symbol}-{day}", "d1", symbol, name, "BUY", qty, price, 5.0, 0.0, 1.0, "tencent", 0.0,
        datetime.fromisoformat(day).replace(hour=10, tzinfo=TZ),
    )


class TestResearchNotes:
    """每股票长期研究档案。"""

    def test_watch_creates_note_with_thesis(self, tmp_store) -> None:
        from mai_stock_sim.agent.research import ResearchNotes

        research = ResearchNotes(tmp_store)
        research.record_watch("600519", "贵州茅台", "高端白酒龙头，估值回到合理区间", thesis="消费复苏+提价能力", concerns="动销数据偏弱")
        note = tmp_store.get_stock_note("600519")
        assert note is not None
        assert note["status"] == "watching"
        assert "提价能力" in note["thesis"]
        assert "动销" in note["concerns"]
        assert note["events"][0]["kind"] == "watch"

    def test_buy_sell_give_up_lifecycle(self, tmp_store) -> None:
        from mai_stock_sim.agent.research import ResearchNotes

        research = ResearchNotes(tmp_store)
        research.record_watch("600519", "贵州茅台", "估值合理")
        research.record_buy("600519", "贵州茅台", 1000, 100.0, "回调充分")
        assert tmp_store.get_stock_note("600519")["status"] == "holding"
        research.record_sell("600519", "贵州茅台", 1000, 110.0, "达到目标价")
        research.record_give_up("600519", "动销持续走弱，逻辑破坏")
        note = tmp_store.get_stock_note("600519")
        assert note["status"] == "given_up"
        kinds = [event["kind"] for event in note["events"]]
        assert kinds == ["watch", "buy", "sell", "give_up"]

    def test_rewatch_after_give_up(self, tmp_store) -> None:
        from mai_stock_sim.agent.research import ResearchNotes

        research = ResearchNotes(tmp_store)
        research.record_watch("600519", "贵州茅台", "估值合理")
        research.record_give_up("600519", "逻辑破坏")
        research.record_watch("600519", "贵州茅台", "季报超预期，重新看看")
        note = tmp_store.get_stock_note("600519")
        assert note["status"] == "watching"
        assert note["events"][-1]["kind"] == "rewatch"

    def test_summary_renders_for_focus_symbols(self, tmp_store) -> None:
        from mai_stock_sim.agent.research import ResearchNotes

        research = ResearchNotes(tmp_store)
        research.record_watch("600519", "贵州茅台", "估值合理", thesis="品牌护城河")
        research.record_watch("000001", "平安银行", "高股息")
        summary = research.summary(["600519"])
        assert "贵州茅台" in summary and "品牌护城河" in summary
        assert "观察中" in summary
        assert "平安银行" not in summary  # 只展示焦点股票


class TestSuggestionSemantic:
    """建议语义识别与追问。"""

    @staticmethod
    def _insert_suggestion(store, text: str = "600519 要起飞了，赶紧上车", symbols: List[str] = None, sid: int = None) -> int:
        from mai_stock_sim.market.calendar import now_sh
        from datetime import timedelta

        return store.insert_suggestion(
            {
                "stream_id": "s1",
                "platform": "qq",
                "user_id": "u1",
                "user_name": "群友甲",
                "message_id": f"m{sid or 1}",
                "text": text,
                "symbols": symbols or ["600519"],
                "stance": "neutral",
                "expires_at": (now_sh() + timedelta(hours=6)).isoformat(),
            }
        )

    @staticmethod
    def _llm_returning(payload: Any):
        async def fake_llm(**kwargs: Any) -> Dict[str, Any]:
            if isinstance(payload, Exception):
                raise payload
            return {
                "success": True,
                "response": payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False),
                "model": "fake",
                "usage": {"prompt_tokens": 30, "completion_tokens": 8},
            }

        return fake_llm

    async def test_parse_labels_suggestions(self, tmp_store) -> None:
        from mai_stock_sim.social.semantic import SuggestionAnalyzer

        first = self._insert_suggestion(tmp_store, sid=1)
        second = self._insert_suggestion(tmp_store, "这票雷大了，财报造假", sid=2)
        analyzer = SuggestionAnalyzer(tmp_store, self._llm_returning([
            {"id": first, "stance": "bullish", "confidence": 0.9, "basis": "语气兴奋且催促买入"},
            {"id": second, "stance": "bearish", "confidence": 0.8, "basis": "明确指出风险"},
        ]))
        labeled = await analyzer.parse_pending()
        assert labeled == 2
        row = tmp_store.connection().execute(
            "SELECT stance, stance_confidence, parsed FROM suggestions WHERE id = ?", (first,)
        ).fetchone()
        assert row["stance"] == "bullish" and row["parsed"] == 1 and row["stance_confidence"] == pytest.approx(0.9)

    async def test_parse_invalid_entries_dropped(self, tmp_store) -> None:
        from mai_stock_sim.social.semantic import SuggestionAnalyzer

        first = self._insert_suggestion(tmp_store, sid=1)
        analyzer = SuggestionAnalyzer(tmp_store, self._llm_returning([
            {"id": first, "stance": "非常看好", "confidence": 5.0, "basis": "x"},  # 非法 stance + 置信度越界
        ]))
        assert await analyzer.parse_pending() == 0
        row = tmp_store.connection().execute("SELECT parsed FROM suggestions WHERE id = ?", (first,)).fetchone()
        assert row["parsed"] == 0  # 保持待解析，下轮重试

    async def test_parse_model_failure_keeps_pending(self, tmp_store) -> None:
        from mai_stock_sim.social.semantic import SuggestionAnalyzer

        self._insert_suggestion(tmp_store, sid=1)
        analyzer = SuggestionAnalyzer(tmp_store, self._llm_returning(RuntimeError("不可用")))
        assert await analyzer.parse_pending() == 0
        assert tmp_store.suggestions_to_parse()  # 仍待解析

    def test_pick_clarification_target(self, tmp_store) -> None:
        from mai_stock_sim.social.semantic import SuggestionAnalyzer

        clear = {"id": 1, "parsed": 1, "asked_clarification": 0, "symbols": ["600519"], "stance": "bullish", "stance_confidence": 0.9}
        uncertain = {"id": 2, "parsed": 1, "asked_clarification": 0, "symbols": ["000001"], "stance": "uncertain", "stance_confidence": 0.5}
        asked = {"id": 3, "parsed": 1, "asked_clarification": 1, "symbols": ["300750"], "stance": "uncertain", "stance_confidence": 0.3}
        no_symbol = {"id": 4, "parsed": 1, "asked_clarification": 0, "symbols": [], "stance": "uncertain", "stance_confidence": 0.2}
        assert SuggestionAnalyzer.pick_clarification_target([clear, uncertain, asked, no_symbol])["id"] == 2

    def test_render_suggestion_line_with_stance(self, tmp_store) -> None:
        from mai_stock_sim.social.semantic import render_suggestion_line

        item = {
            "created_at": "2026-11-08 10:00:00", "user_name": "群友甲", "text": "600519 要起飞",
            "symbols": ["600519"], "parsed": 1, "stance": "bullish", "stance_confidence": 0.8, "stance_basis": "语气兴奋",
        }
        line = render_suggestion_line(item)
        assert "观点: 看多 0.8" in line and "语气兴奋" in line
        plain = render_suggestion_line({**item, "parsed": 0})
        assert "观点" not in plain

    async def test_context_carries_stance(self, tmp_store, fake_quotes, tmp_path) -> None:
        """已解析的观点进入决策上下文。"""
        from mai_stock_sim.agent.context import DecisionContextBuilder
        from mai_stock_sim.agent.stock_selection import CandidatePoolBuilder
        from mai_stock_sim.agent.strategy_memory import StrategyMemory
        from mai_stock_sim.market.calendar import TradeCalendar
        from mai_stock_sim.trading.risk import RiskParams
        from tests.test_full_loop import FrozenOpenCalendar

        self._insert_suggestion(tmp_store, sid=1)
        from mai_stock_sim.social.semantic import SuggestionAnalyzer

        analyzer = SuggestionAnalyzer(
            tmp_store,
            self._llm_returning([{"id": tmp_store.connection().execute("SELECT id FROM suggestions").fetchone()[0], "stance": "bearish", "confidence": 0.7, "basis": "提示风险"}]),
        )
        await analyzer.parse_pending()
        builder = DecisionContextBuilder(
            tmp_store, fake_quotes, FrozenOpenCalendar(tmp_path), StrategyMemory(tmp_store),
            CandidatePoolBuilder(tmp_store), lambda: RiskParams(),
        )
        context = await builder.build("人格")
        assert "观点: 看空 0.7" in context["suggestions"]


class TestEventWatcher:
    """事件驱动的主动观察。"""

    @staticmethod
    def _watcher(tmp_store, fake_quotes, tmp_path):
        from mai_stock_sim.market.calendar import TradeCalendar
        from mai_stock_sim.runtime.events import EventPolicy, EventWatcher

        watcher = EventWatcher(tmp_store, fake_quotes, TradeCalendar(tmp_path))
        watcher.bind_policy(lambda: EventPolicy())
        return watcher

    def test_move_event(self, tmp_store, fake_quotes, tmp_path) -> None:
        fake_quotes.set(make_quote(symbol="600519", last_price=108.0, previous_close=100.0))
        events = self._watcher(tmp_store, fake_quotes, tmp_path).detect(["600519"])
        assert [event.kind for event in events] == ["move"]
        assert "+8.0" in events[0].detail

    def test_limit_event_on_main_board(self, tmp_store, fake_quotes, tmp_path) -> None:
        fake_quotes.set(make_quote(symbol="600519", last_price=110.0, previous_close=100.0))
        events = self._watcher(tmp_store, fake_quotes, tmp_path).detect(["600519"])
        assert events[0].kind == "limit"
        assert "涨停" in events[0].detail

    def test_gap_event(self, tmp_store, fake_quotes, tmp_path) -> None:
        fake_quotes.set(make_quote(symbol="600519", last_price=100.5, previous_close=100.0))
        quote = fake_quotes.quotes["600519"]
        object.__setattr__(quote, "open_price", 95.0)  # 低开 5%，现回稳
        events = self._watcher(tmp_store, fake_quotes, tmp_path).detect(["600519"])
        assert [event.kind for event in events] == ["gap"]
        assert "跳空" in events[0].detail

    def test_volume_spike_event(self, tmp_store, fake_quotes, tmp_path) -> None:
        from mai_stock_sim.market.history import HistoryService
        from mai_stock_sim.market.models import DailyBar

        bars = [
            DailyBar("600519", f"2026-10-{day:02d}", 10, 10.2, 9.9, 10.0, 1e6, 1e7, 0.1)
            for day in range(1, 25)
        ]
        HistoryService(tmp_store.conn_provider()).save_bars(bars)
        fake_quotes.set(make_quote(symbol="600519", last_price=10.0, previous_close=10.0, volume=3.5e6))
        watcher = self._watcher(tmp_store, fake_quotes, tmp_path)
        watcher._session_elapsed_fraction = lambda: 1.0  # 收盘口径：现量即全天量
        events = watcher.detect(["600519"])
        assert [event.kind for event in events] == ["volume"]
        assert "3.5 倍" in events[0].detail

    def test_breakout_event(self, tmp_store, fake_quotes, tmp_path) -> None:
        from mai_stock_sim.market.history import HistoryService
        from mai_stock_sim.market.models import DailyBar

        bars = [
            DailyBar("600519", f"2026-10-{day:02d}", 9.5, 10.5, 9.4, 10.0, 1e6, 1e7, 0.1)
            for day in range(1, 25)
        ]
        HistoryService(tmp_store.conn_provider()).save_bars(bars)
        # +6%（低于快速波动阈值）但越过 20 日高点 10.5
        fake_quotes.set(make_quote(symbol="600519", last_price=10.6, previous_close=10.0))
        events = self._watcher(tmp_store, fake_quotes, tmp_path).detect(["600519"])
        assert events[0].kind == "breakout_up"

    def test_trigger_cooldown_and_daily_cap(self, tmp_store, fake_quotes, tmp_path) -> None:
        from mai_stock_sim.runtime.events import EventPolicy, EventWatcher

        from mai_stock_sim.market.calendar import TradeCalendar

        watcher = EventWatcher(tmp_store, fake_quotes, TradeCalendar(tmp_path))
        watcher.bind_policy(lambda: EventPolicy(enabled=True, max_triggers_per_day=2, cooldown_minutes=60))
        assert watcher.should_trigger("600519")
        assert not watcher.should_trigger("600519")  # 冷却中
        assert watcher.should_trigger("000001")
        assert not watcher.should_trigger("300750")  # 每日上限已到

    def test_disabled_policy(self, tmp_store, fake_quotes, tmp_path) -> None:
        from mai_stock_sim.runtime.events import EventPolicy, EventWatcher

        from mai_stock_sim.market.calendar import TradeCalendar

        watcher = EventWatcher(tmp_store, fake_quotes, TradeCalendar(tmp_path))
        watcher.bind_policy(lambda: EventPolicy(enabled=False))
        assert not watcher.should_trigger("600519")


class TestCorporateAutoProcess:
    """分红送转自动处理。"""

    @staticmethod
    def _guard(store, plans_by_symbol: Dict[str, List[Dict[str, Any]]]):
        from mai_stock_sim.trading.corporate_actions import CorporateActionGuard

        return CorporateActionGuard(store, plans_provider=lambda symbol: plans_by_symbol.get(symbol, []))

    async def test_dividend_and_bonus_applied(self, tmp_store) -> None:
        from mai_stock_sim.market.calendar import today_sh

        _buy(tmp_store, qty=1000, price=100.0)
        ex_date = today_sh().isoformat()
        guard = self._guard(
            tmp_store,
            {"600519": [{"ex_date": ex_date, "record_date": ex_date, "cash_per_share": 2.0, "bonus_per_share": 0.1, "transfer_per_share": 0.2, "report_date": ex_date}]},
        )
        results = await guard.auto_process(["600519"], lambda symbol: 100.0)
        assert results[0]["action"] == "applied"
        # 现金分红 1000*2=2000；送转 10+20=30% → 1300 股；成本摊薄
        assert tmp_store.get_cash() == pytest.approx(1_000_000.0 - 100_006.0 + 2000.0)
        position = tmp_store.get_position("600519")
        assert position["qty"] == 1300
        assert position["avg_cost"] == pytest.approx(100_006.0 / 1300, rel=1e-4)
        # 审计：cash_events + applied 公司行为
        events = [row["event_type"] for row in tmp_store.connection().execute("SELECT event_type FROM cash_events")]
        assert "dividend" in events
        assert "600519" not in guard.blocked_symbols()

    async def test_idempotent_no_double_apply(self, tmp_store) -> None:
        from mai_stock_sim.market.calendar import today_sh

        _buy(tmp_store)
        ex_date = today_sh().isoformat()
        plans = {"600519": [{"ex_date": ex_date, "cash_per_share": 1.0, "bonus_per_share": 0.0, "transfer_per_share": 0.0, "report_date": ex_date}]}
        guard = self._guard(tmp_store, plans)
        await guard.auto_process(["600519"], lambda symbol: 100.0)
        cash_after_first = tmp_store.get_cash()
        results = await guard.auto_process(["600519"], lambda symbol: 100.0)
        assert results == []
        assert tmp_store.get_cash() == cash_after_first

    async def test_future_ex_date_not_applied(self, tmp_store) -> None:
        _buy(tmp_store)
        guard = self._guard(
            tmp_store,
            {"600519": [{"ex_date": "2026-12-31", "cash_per_share": 1.0, "bonus_per_share": 0.0, "transfer_per_share": 0.0, "report_date": "2026-12-31"}]},
        )
        results = await guard.auto_process(["600519"], lambda symbol: 100.0)
        assert results == []

    async def test_absurd_data_goes_manual(self, tmp_store) -> None:
        from mai_stock_sim.market.calendar import today_sh

        _buy(tmp_store)
        ex_date = today_sh().isoformat()
        guard = self._guard(
            tmp_store,
            {"600519": [{"ex_date": ex_date, "cash_per_share": 50.0, "bonus_per_share": 0.0, "transfer_per_share": 0.0, "report_date": ex_date}]},
        )
        results = await guard.auto_process(["600519"], lambda symbol: 100.0)  # 派现 = 现价 50%
        assert results[0]["action"] == "pending_review"
        assert "600519" in guard.blocked_symbols()  # 暂停新增买入
        assert tmp_store.get_cash() == pytest.approx(1_000_000.0 - 100_006.0)  # 账户未动

    async def test_share_adjustment_keeps_invariants(self, tmp_store) -> None:
        from mai_stock_sim.trading.account import AccountStore

        _buy(tmp_store, qty=1000, price=100.0, day="2026-11-03")
        _buy(tmp_store, qty=500, price=110.0, day="2026-11-04")
        conn = tmp_store.connection()
        conn.execute("BEGIN IMMEDIATE")
        result = AccountStore.adjust_shares_in_conn(conn, "600519", 0.3)
        conn.commit()
        assert result["new_qty"] == 1950  # 1500 * 1.3
        # 批次同比例缩放：两个昨日批次都可卖
        assert tmp_store.sellable_qty("600519") == 1950
        assert tmp_store.verify_invariants(lambda symbol: 100.0) == []


class TestAnalytics:
    """收益统计引擎。"""

    @staticmethod
    def _seed_performance(store) -> None:
        _buy(store, qty=1000, price=100.0, day="2026-11-03")
        store.apply_fill(
            "o-sell", "d2", "600519", "贵州茅台", "SELL", 1000, 120.0, 5.0, 60.0, 1.2, "tencent", 0.0,
            datetime.fromisoformat("2026-11-05").replace(hour=14, tzinfo=TZ),
        )
        for day, equity, day_pnl in (
            ("2026-11-04", 999_000.0, -1_000.0),
            ("2026-11-05", 1_019_000.0, 20_000.0),
            ("2026-11-06", 1_009_000.0, -10_000.0),
        ):
            store.save_daily_equity(day, equity, 0.0, day_pnl, 0.0, "[]")
        store.save_market_snapshot("2026-11-06", "600519", 101.0, "tencent")
        store.upsert_index_bars(
            "INDEX000300",
            [{"date": "2026-11-04", "open": 4000, "close": 4000, "high": 4010, "low": 3990, "volume": 1e8},
             {"date": "2026-11-06", "open": 4000, "close": 4080, "high": 4090, "low": 3990, "volume": 1e8}],
        )

    def test_overview_statistics(self, tmp_store) -> None:
        from mai_stock_sim.trading.analytics import PerformanceAnalytics

        self._seed_performance(tmp_store)
        overview = PerformanceAnalytics(tmp_store).overview()
        assert overview["initialized"]
        assert overview["total_return_pct"] == pytest.approx(0.9)  # 1,009,000 / 1,000,000 - 1
        assert overview["drawdown"]["max_drawdown_pct"] == pytest.approx(0.98, abs=0.05)  # 1,019k → 1,009k
        assert overview["trades"]["closed_trades"] == 1
        assert overview["trades"]["win_rate_pct"] == 100.0
        assert overview["benchmark_return_pct"] == pytest.approx(2.0)  # 4000 → 4080
        assert overview["trades"]["avg_holding_days"] == pytest.approx(2.2)  # 11-03 10:00 → 11-05 14:00

    def test_summary_text_mentions_benchmark(self, tmp_store) -> None:
        from mai_stock_sim.trading.analytics import PerformanceAnalytics

        self._seed_performance(tmp_store)
        text = PerformanceAnalytics(tmp_store).summary_text()
        assert "沪深300 +2.00%" in text
        assert "跑输" in text  # 0.9% < 2.0%
        assert "胜率 100.0%" in text

    def test_contribution_ranking(self, tmp_store) -> None:
        from mai_stock_sim.trading.analytics import PerformanceAnalytics

        self._seed_performance(tmp_store)
        contributions = PerformanceAnalytics(tmp_store).per_symbol_contribution()
        assert contributions[0]["symbol"] == "600519"
        assert contributions[0]["realized"] == pytest.approx(19_927.8, abs=1.0)

    def test_uninitialized(self, tmp_store) -> None:
        from mai_stock_sim.trading.analytics import PerformanceAnalytics

        assert PerformanceAnalytics(tmp_store).summary_text() == ""


class TestV13Config:
    """新配置项默认值。"""

    def test_new_sections_defaults(self) -> None:
        config = MaiStockSimConfig()
        assert config.decision.research_notes_enabled is True
        assert config.chat.semantic_enabled is True
        assert config.chat.ask_back_enabled is True
        assert config.events.event_trigger_enabled is True
        assert config.events.event_move_threshold_pct == 7.0
        assert config.events.event_max_per_day == 6
        assert config.events.event_cooldown_minutes == 60
        assert config.advanced.corporate_actions_auto is True

    async def test_decision_extra_context_merge(self, tmp_store, fake_quotes, tmp_path) -> None:
        """extra 上下文覆盖默认占位并进入审计输入。"""
        from mai_stock_sim.agent.context import DecisionContextBuilder
        from mai_stock_sim.agent.stock_selection import CandidatePoolBuilder
        from mai_stock_sim.agent.strategy_memory import StrategyMemory
        from mai_stock_sim.trading.risk import RiskParams
        from tests.test_full_loop import FrozenOpenCalendar

        builder = DecisionContextBuilder(
            tmp_store, fake_quotes, FrozenOpenCalendar(tmp_path), StrategyMemory(tmp_store),
            CandidatePoolBuilder(tmp_store), lambda: RiskParams(),
        )
        context = await builder.build("人格", extra={"research": "- 贵州茅台［观察中］看好：品牌", "market_events": "- 600519 快速波动"})
        assert "品牌" in context["research"]
        assert "快速波动" in context["market_events"]

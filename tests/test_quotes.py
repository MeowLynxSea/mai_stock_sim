"""行情解析与数据质量测试（离线 Fixture，不依赖网络）。"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from mai_stock_sim.market.models import QualityLevel
from mai_stock_sim.market.quality import basic_sanity, cross_check, freshness_age, mark_staleness
from mai_stock_sim.market.quotes import QuoteService
from mai_stock_sim.market.sina import parse_sina_payload
from mai_stock_sim.market.symbols import (
    display_symbol,
    exchange_and_board,
    is_risk_warning_name,
    normalize_symbol,
    provider_symbol,
)
from mai_stock_sim.market.tencent import parse_tencent_payload

# 2026-11-06 实际响应裁剪样本
_TENCENT_SAMPLE = (
    'v_sh600519="1~贵州茅台~600519~1453.00~1452.00~1450.00~42848~23139~19709~1452.00~2~1451.60~1~'
    "1451.33~1~1451.01~1~1451.00~5~1453.00~2~1453.01~2~1453.06~1~1453.07~8~1453.19~1~~"
    '20261106161458~1.00~0.07~1457.72~1436.00~1453.00/42848/6199528289~42848~619953~0.34~22.11~~'
    '1457.72~1436.00~1.50~18163.69~18163.69~6.70~1597.20~1306.80~0.65~-4~1446.87~21.13~22.06~~~-0.02~'
    '619952.8289~232.4800~16~   A~GP-A";'
)

_SINA_SAMPLE = (
    'var hq_str_sh600519="贵州茅台,1450.000,1452.000,1453.000,1457.720,1436.000,1452.000,1453.000,'
    "4284772,6199528289.000,200,1452.000,100,1451.600,100,1451.330,100,1451.010,500,1451.000,182,"
    "1453.000,200,1453.010,100,1453.060,800,1453.070,100,1453.190,2026-11-06,15:34:59,00\";"
)


class TestTencentParsing:
    """腾讯行情解析。"""

    def test_normal_parse(self) -> None:
        quotes = parse_tencent_payload(_TENCENT_SAMPLE)
        assert "600519" in quotes
        quote = quotes["600519"]
        assert quote.name == "贵州茅台"
        assert quote.last_price == pytest.approx(1453.00)
        assert quote.previous_close == pytest.approx(1452.00)
        assert quote.high_price == pytest.approx(1457.72)
        assert quote.low_price == pytest.approx(1436.00)
        assert quote.volume == pytest.approx(42848 * 100)  # 手 → 股
        assert quote.turnover == pytest.approx(619953 * 10000)  # 万元 → 元
        assert quote.timestamp == datetime(2026, 11, 6, 16, 14, 58, tzinfo=quote.timestamp.tzinfo)
        assert quote.source == "tencent"

    def test_gbk_decode_and_empty_payload(self) -> None:
        assert parse_tencent_payload("") == {}
        assert parse_tencent_payload("pv_none_match=1;") == {}

    def test_truncated_payload_skipped(self) -> None:
        # 字段过少的行直接跳过，不抛异常
        assert parse_tencent_payload('v_sh600519="1~贵州茅台";') == {}

    def test_code_mismatch_rejected(self) -> None:
        # 变量名与字段代码不一致时拒绝
        payload = _TENCENT_SAMPLE.replace("v_sh600519", "v_sz000001")
        quotes = parse_tencent_payload(payload)
        assert quotes.get("000001") is None or quotes.get("600519") is None


class TestSinaParsing:
    """新浪行情解析。"""

    def test_normal_parse(self) -> None:
        quotes = parse_sina_payload(_SINA_SAMPLE)
        assert "600519" in quotes
        quote = quotes["600519"]
        assert quote.name == "贵州茅台"
        assert quote.last_price == pytest.approx(1453.0)
        assert quote.previous_close == pytest.approx(1452.0)
        assert quote.volume == pytest.approx(4284772)  # 新浪单位是股
        assert quote.turnover == pytest.approx(6199528289.0)
        assert quote.source == "sina"

    def test_suspended_stock(self) -> None:
        # 新浪停牌股最新价 0：应标记 SUSPENDED，价格退回昨收
        fields = (
            ["浦发银行", "0.000", "10.000", "0.000", "0.000", "0.000", "0.000", "0.000", "0", "0.000"]
            + ["0", "0.000"] * 10  # 五档买卖量价对（20 个字段，索引 10-29）
            + ["2026-11-06", "15:00:00", "00"]
        )
        lines = 'var hq_str_sh600000="' + ",".join(fields) + '";'
        quotes = parse_sina_payload(lines)
        quote = quotes["600000"]
        assert quote.suspended
        assert quote.quality is QualityLevel.SUSPENDED
        assert quote.last_price == pytest.approx(10.0)


class TestSymbols:
    """代码规范化。"""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("600519", "600519"),
            ("sh600519", "600519"),
            ("SH600519", "600519"),
            ("600519.SH", "600519"),
            ("000001.SZ", "000001"),
            ("bj430047", "430047"),
        ],
    )
    def test_normalize(self, raw: str, expected: str) -> None:
        assert normalize_symbol(raw) == expected

    @pytest.mark.parametrize("raw", ["", "6005", "6005190", "abc", "sh_600519"])
    def test_normalize_invalid(self, raw: str) -> None:
        assert normalize_symbol(raw) is None

    @pytest.mark.parametrize(
        ("symbol", "exchange", "board_name"),
        [
            ("600519", "SH", "SH_MAIN"),
            ("688981", "SH", "STAR"),
            ("000001", "SZ", "SZ_MAIN"),
            ("300750", "SZ", "CHINEXT"),
            ("430047", "BJ", "BSE"),
            ("920085", "BJ", "BSE"),
        ],
    )
    def test_board_detection(self, symbol: str, exchange: str, board_name: str) -> None:
        info = exchange_and_board(symbol)
        assert info is not None
        assert info[0] == exchange
        assert info[1].value == board_name

    def test_provider_symbol(self) -> None:
        assert provider_symbol("600519", "tencent") == "sh600519"
        assert provider_symbol("000001", "sina") == "sz000001"
        assert provider_symbol("430047", "tencent") == "bj430047"

    def test_display_symbol(self) -> None:
        assert display_symbol("600519") == "600519.SH"
        assert display_symbol("300750") == "300750.SZ"

    def test_risk_warning_name(self) -> None:
        assert is_risk_warning_name("*ST美丽")
        assert is_risk_warning_name("ST安全")
        assert not is_risk_warning_name("贵州茅台")


class TestQuality:
    """数据质量检查。"""

    def test_basic_sanity_ok(self) -> None:
        from tests.conftest import make_quote

        assert basic_sanity(make_quote()) is None

    def test_basic_sanity_problems(self) -> None:
        from tests.conftest import make_quote

        assert "非正" in (basic_sanity(make_quote(last_price=0)) or "")
        high_low_flipped = make_quote()
        high_low_flipped.high_price = 1.0
        high_low_flipped.low_price = 100.0
        assert "最高价低于最低价" in (basic_sanity(high_low_flipped) or "")

    def test_cross_check(self) -> None:
        from tests.conftest import make_quote

        primary = make_quote()
        secondary = make_quote(last_price=100.5)
        assert cross_check(primary, secondary, 0.5) is None
        mismatch = make_quote(last_price=110.0)
        assert cross_check(primary, mismatch, 0.5) is not None

    def test_staleness(self) -> None:
        from tests.conftest import make_quote

        from mai_stock_sim.market.calendar import now_sh

        old = make_quote(timestamp=now_sh() - timedelta(seconds=600))
        mark_staleness(old, 120)
        assert old.quality is QualityLevel.STALE
        day_old = make_quote(timestamp=now_sh() - timedelta(days=2))
        mark_staleness(day_old, 120)
        assert day_old.quality is QualityLevel.STATIC

    def test_freshness_age(self) -> None:
        from tests.conftest import make_quote

        from mai_stock_sim.market.calendar import now_sh

        quote = make_quote(timestamp=now_sh() - timedelta(seconds=30))
        assert freshness_age(quote) >= 29


class TestQuoteServiceOffline:
    """QuoteService 纯逻辑部分（不联网）。"""

    def test_stats_record(self) -> None:
        service = QuoteService()
        service.stats.record("tencent", True)
        service.stats.record("tencent", False)
        snapshot = service.stats.snapshot()
        assert snapshot["tencent"] == {"requests": 2, "failures": 1}

    def test_update_settings(self) -> None:
        service = QuoteService()
        service.update_settings(max_age_seconds=99)
        assert service.max_age_seconds == 99

    def test_conflict_blocks_tradable(self) -> None:
        from tests.conftest import make_quote

        service = QuoteService()
        service._cross_conflict["600519"] = "差异 2%"  # noqa: SLF001
        ok, reason = service.is_tradable(make_quote())
        assert not ok
        assert "双源行情冲突" in reason

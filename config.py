"""插件配置模型（WebUI 表单由宿主根据本模型自动生成）。"""

from __future__ import annotations

from typing import Any, ClassVar, Dict, List, Literal

from maibot_sdk import Field, PluginConfigBase

PLUGIN_VERSION = "1.1.0"


class PluginSection(PluginConfigBase):
    """[plugin] 基础段。"""

    __ui_label__: ClassVar[str] = "插件"
    __ui_icon__: ClassVar[str] = "package"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=True,
        description="启用插件。关闭后停止一切行情、决策与播报。",
        json_schema_extra={"label": "启用插件", "order": 1},
    )
    config_version: str = Field(
        default=PLUGIN_VERSION,
        description="配置文件版本号（自动维护，请勿手改）",
        json_schema_extra={"label": "配置版本", "disabled": True, "order": 2},
    )


class GeneralSection(PluginConfigBase):
    """[general] 常用设置。"""

    __ui_label__: ClassVar[str] = "常用"
    __ui_icon__: ClassVar[str] = "settings"
    __ui_order__: ClassVar[int] = 1

    auto_trade_enabled: bool = Field(
        default=False,
        description=(
            "启用自动模拟交易。默认关闭：请先通过 /股票 命令与行情检查确认数据源正常，"
            "再在设置页开启。开启后麦麦将自主决策买卖（虚拟资金）。"
        ),
        json_schema_extra={"label": "启用自动模拟交易", "hint": "开启后麦麦自主交易，人工不能干预具体买卖", "order": 1},
    )
    broadcast_stream_ids: List[str] = Field(
        default_factory=list,
        description=(
            "播报目标聊天流 ID（即 MaiBot 的 session_id / stream_id，WebUI 聊天流页面可查，"
            "或在目标群发送 /股票 状态，回复末尾会显示本群的聊天流 ID）。不是 QQ 群号。"
            "留空则不播报。"
        ),
        json_schema_extra={
            "label": "播报聊天流 ID",
            "hint": "填聊天流 ID（session_id），不是群号；留空则不播报。可在目标群发 /股票 状态 获取",
            "placeholder": "例如 c1f2c4c6...",
            "order": 2,
        },
    )
    broadcast_enabled: bool = Field(
        default=True,
        description="允许麦麦主动播报交易与收盘总结。",
        json_schema_extra={"label": "允许主动播报", "order": 3},
    )
    broadcast_min_interval_minutes: int = Field(
        default=10,
        ge=1,
        le=240,
        description="两次主动播报之间的最短间隔（分钟）。",
        json_schema_extra={"label": "播报最短间隔(分钟)", "order": 4},
    )
    broadcast_daily_max: int = Field(
        default=8,
        ge=1,
        le=50,
        description="每日最大主动播报次数。",
        json_schema_extra={"label": "每日播报上限", "order": 5},
    )
    initial_cash: float = Field(
        default=1_000_000.0,
        gt=10_000.0,
        le=1_000_000_000.0,
        description="初始虚拟资金（人民币元）。仅在账户首次创建时生效，之后修改不会重置现有账户。",
        json_schema_extra={"label": "初始虚拟资金(元)", "hint": "仅开户时生效", "order": 6},
    )
    allowed_boards: List[str] = Field(
        default_factory=lambda: ["SH_MAIN", "SZ_MAIN", "CHINEXT", "STAR"],
        description="允许交易的市场板块（SH_MAIN/SZ_MAIN/CHINEXT/STAR/BSE）。",
        json_schema_extra={"label": "允许的板块", "order": 7},
    )


class TradeSection(PluginConfigBase):
    """[trade] 交易与风控参数。"""

    __ui_label__: ClassVar[str] = "交易"
    __ui_icon__: ClassVar[str] = "trending-up"
    __ui_order__: ClassVar[int] = 2

    preset: Literal["conservative", "balanced", "aggressive", "unlimited", "custom"] = Field(
        default="balanced",
        description=(
            "交易风格预设。选择预设后按预设参数执行（下方自定义参数不再生效）；"
            "选「自定义」时使用下方参数。注意：任何预设（包括「无限制」）都不会解除"
            "A 股市场规则（T+1、涨跌停、申报数量、禁止卖空与负现金）与原子记账约束。"
        ),
        json_schema_extra={
            "label": "交易风格预设",
            "hint": "无限制仅取消风控层面的仓位/频率限制，市场规则始终生效",
            "order": 0,
        },
    )
    max_position_pct: float = Field(
        default=0.20,
        gt=0.01,
        le=1.0,
        description="单只股票最大仓位（占总权益比例）。",
        json_schema_extra={"label": "单股最大仓位", "step": 0.01, "depends_on": "preset", "depends_value": "custom", "order": 1},
    )
    max_total_position_pct: float = Field(
        default=0.70,
        gt=0.05,
        le=1.0,
        description="总持仓市值上限（占总权益比例）。",
        json_schema_extra={"label": "总仓位上限", "step": 0.05, "depends_on": "preset", "depends_value": "custom", "order": 2},
    )
    day_loss_stop_pct: float = Field(
        default=0.03,
        gt=0.005,
        le=0.5,
        description="当日权益回撤达到该比例后停止新增买入。",
        json_schema_extra={"label": "单日亏损停止买入阈值", "step": 0.005, "depends_on": "preset", "depends_value": "custom", "order": 3},
    )
    max_day_trades: int = Field(
        default=5,
        ge=1,
        le=50,
        description="每日最大成交笔数。",
        json_schema_extra={"label": "每日最大成交笔数", "depends_on": "preset", "depends_value": "custom", "order": 4},
    )
    max_orders_per_decision: int = Field(
        default=3,
        ge=1,
        le=10,
        description="每轮决策允许产生的最大订单数。",
        json_schema_extra={"label": "每轮最大订单数", "depends_on": "preset", "depends_value": "custom", "order": 5},
    )
    slippage_bps: float = Field(
        default=10.0,
        ge=0.0,
        le=200.0,
        description="模拟滑点（基点）。买入上浮、卖出下浮。",
        json_schema_extra={"label": "模拟滑点(bp)", "depends_on": "preset", "depends_value": "custom", "order": 6},
    )
    max_volume_participation: float = Field(
        default=0.01,
        gt=0.0001,
        le=0.1,
        description="单笔模拟成交占该股当日成交量的最大比例（流动性约束）。",
        json_schema_extra={"label": "单笔成交量占比上限", "step": 0.005, "depends_on": "preset", "depends_value": "custom", "order": 7},
    )
    commission_rate: float = Field(
        default=0.00025,
        ge=0.0,
        le=0.01,
        description="佣金费率（双向）。",
        json_schema_extra={"label": "佣金费率", "step": 0.00005, "order": 8},
    )
    min_commission: float = Field(
        default=5.0,
        ge=0.0,
        le=100.0,
        description="单笔最低佣金（元）。",
        json_schema_extra={"label": "最低佣金(元)", "order": 9},
    )
    stamp_tax_rate: float = Field(
        default=0.0005,
        ge=0.0,
        le=0.01,
        description="印花税费率（仅卖出）。",
        json_schema_extra={"label": "印花税费率", "step": 0.00005, "order": 10},
    )
    transfer_fee_rate: float = Field(
        default=0.00001,
        ge=0.0,
        le=0.001,
        description="过户费费率（双向）。",
        json_schema_extra={"label": "过户费费率", "step": 0.000005, "order": 11},
    )
    allow_risk_warning_stocks: bool = Field(
        default=False,
        description="是否允许交易风险警示股票（ST/*ST）。",
        json_schema_extra={"label": "允许风险警示股票", "depends_on": "preset", "depends_value": "custom", "order": 12},
    )
    allow_new_listing: bool = Field(
        default=False,
        description="是否允许交易上市不足 30 天的新股。",
        json_schema_extra={"label": "允许交易新股", "depends_on": "preset", "depends_value": "custom", "order": 13},
    )
    min_liquidity_amount: float = Field(
        default=50_000_000.0,
        ge=0.0,
        le=10_000_000_000.0,
        description="候选股票最低当日成交额（元）。",
        json_schema_extra={"label": "最低流动性要求(元)", "depends_on": "preset", "depends_value": "custom", "order": 14},
    )


# 各预设的参数表（费用模型不在预设内，始终使用自定义费率）
TRADE_PRESETS: Dict[str, Dict[str, Any]] = {
    "conservative": {
        "max_position_pct": 0.10,
        "max_total_position_pct": 0.40,
        "day_loss_stop_pct": 0.02,
        "max_day_trades": 3,
        "max_orders_per_decision": 2,
        "slippage_bps": 15.0,
        "max_volume_participation": 0.005,
        "allow_risk_warning_stocks": False,
        "allow_new_listing": False,
        "min_liquidity_amount": 200_000_000.0,
    },
    "balanced": {
        "max_position_pct": 0.20,
        "max_total_position_pct": 0.70,
        "day_loss_stop_pct": 0.03,
        "max_day_trades": 5,
        "max_orders_per_decision": 3,
        "slippage_bps": 10.0,
        "max_volume_participation": 0.01,
        "allow_risk_warning_stocks": False,
        "allow_new_listing": False,
        "min_liquidity_amount": 50_000_000.0,
    },
    "aggressive": {
        "max_position_pct": 0.30,
        "max_total_position_pct": 0.90,
        "day_loss_stop_pct": 0.06,
        "max_day_trades": 10,
        "max_orders_per_decision": 5,
        "slippage_bps": 8.0,
        "max_volume_participation": 0.02,
        "allow_risk_warning_stocks": True,
        "allow_new_listing": False,
        "min_liquidity_amount": 20_000_000.0,
    },
    "unlimited": {
        "max_position_pct": 1.0,
        "max_total_position_pct": 1.0,
        "day_loss_stop_pct": 0.999,
        "max_day_trades": 100,
        "max_orders_per_decision": 10,
        "slippage_bps": 5.0,
        "max_volume_participation": 0.05,
        "allow_risk_warning_stocks": True,
        "allow_new_listing": True,
        "min_liquidity_amount": 0.0,
    },
}


def resolve_trade_params(config: "MaiStockSimConfig") -> Dict[str, Any]:
    """按预设或自定义解析出实际生效的交易参数。

    Returns:
        Dict[str, Any]: 生效参数（含 preset 覆盖的风控/滑点/流动性字段；费用字段始终来自自定义配置）。
    """
    preset_name = str(config.trade.preset)
    if preset_name == "custom":
        source: Dict[str, Any] = dict(config.trade.model_dump())
    else:
        source = dict(TRADE_PRESETS[preset_name])
    # 费用模型不参与预设
    source["commission_rate"] = config.trade.commission_rate
    source["min_commission"] = config.trade.min_commission
    source["stamp_tax_rate"] = config.trade.stamp_tax_rate
    source["transfer_fee_rate"] = config.trade.transfer_fee_rate
    source["preset"] = preset_name
    return source


class DecisionSection(PluginConfigBase):
    """[decision] 麦麦决策参数。"""

    __ui_label__: ClassVar[str] = "麦麦决策"
    __ui_icon__: ClassVar[str] = "brain"
    __ui_order__: ClassVar[int] = 3

    model_name: str = Field(
        default="",
        description="决策使用的模型名称（留空使用宿主默认模型）。",
        json_schema_extra={"label": "决策模型", "hint": "留空使用默认模型", "order": 1},
    )
    decision_interval_minutes: int = Field(
        default=30,
        ge=5,
        le=240,
        description="交易时段内周期性决策间隔（分钟）。模型可在 5~480 分钟内建议下次观察时间。",
        json_schema_extra={"label": "决策间隔(分钟)", "order": 2},
    )
    min_decision_interval_minutes: int = Field(
        default=5,
        ge=2,
        le=60,
        description="允许的最短决策间隔（分钟，限制模型自行缩短观察的频率）。",
        json_schema_extra={"label": "最短决策间隔(分钟)", "order": 3},
    )
    budget_enabled: bool = Field(
        default=False,
        description=(
            "启用每日模型预算限制（每日决策次数 + Token 预算）。默认关闭即不限制"
            "决策次数与 Token 用量（用量仍会记录统计，达到预算不再中断交易系统，"
            "行情、风控与结算照常运行）。"
        ),
        json_schema_extra={"label": "启用模型预算限制", "hint": "默认关闭（不限制）", "order": 3},
    )
    max_decision_calls_per_day: int = Field(
        default=16,
        ge=1,
        le=100,
        description="每日最大决策（模型调用）次数。",
        json_schema_extra={"label": "每日决策次数上限", "depends_on": "budget_enabled", "depends_value": True, "order": 4},
    )
    max_tokens_per_day: int = Field(
        default=400_000,
        ge=10_000,
        le=10_000_000,
        description="每日模型 Token 预算。",
        json_schema_extra={"label": "每日Token预算", "depends_on": "budget_enabled", "depends_value": True, "order": 5},
    )
    max_output_tokens: int = Field(
        default=2000,
        ge=200,
        le=8000,
        description="单次决策最大输出 Token（此项不受预算开关影响，始终生效）。",
        json_schema_extra={"label": "单次最大输出Token", "order": 6},
    )
    candidate_pool_size: int = Field(
        default=40,
        ge=10,
        le=100,
        description="候选池规模（进入决策上下文的股票数量上限）。",
        json_schema_extra={"label": "候选池数量", "order": 7},
    )
    use_strategy_memory: bool = Field(
        default=True,
        description="决策时参考历史交易经历与复盘记忆。",
        json_schema_extra={"label": "参考历史交易经历", "order": 8},
    )
    allow_watchlist_update: bool = Field(
        default=True,
        description="允许麦麦自主更新观察列表。",
        json_schema_extra={"label": "允许自主更新观察列表", "order": 9},
    )


class ChatSection(PluginConfigBase):
    """[chat] 群聊影响设置。"""

    __ui_label__: ClassVar[str] = "群聊影响"
    __ui_icon__: ClassVar[str] = "message-circle"
    __ui_order__: ClassVar[int] = 4

    suggestions_enabled: bool = Field(
        default=True,
        description="启用群聊建议识别（旁路观察，不阻塞消息链）。",
        json_schema_extra={"label": "启用群聊建议", "order": 1},
    )
    suggestion_stream_mode: Literal["allowlist", "blocklist"] = Field(
        default="allowlist",
        description=(
            "聊天流监听模式。白名单：只监听下方列表中的聊天流（列表为空 = 不监听任何群）；"
            "黑名单：监听除下方列表外的所有聊天流（列表为空 = 监听全部，注意范围）。"
        ),
        json_schema_extra={"label": "监听模式", "hint": "白名单=只监听列出的；黑名单=监听除列出外的全部", "order": 2},
    )
    allowed_stream_ids: List[str] = Field(
        default_factory=list,
        description=(
            "聊天流 ID 列表（即 MaiBot 的 session_id / stream_id，不是 QQ 群号）。"
            "WebUI 聊天流页面可查，或在目标群发送 /股票 状态，回复末尾会显示本群聊天流 ID。"
            "白名单模式下留空 = 不监听任何群；黑名单模式下留空 = 监听全部聊天流。"
        ),
        json_schema_extra={
            "label": "聊天流 ID 列表",
            "hint": "填聊天流 ID（session_id），不是群号；可在目标群发 /股票 状态 获取",
            "placeholder": "例如 c1f2c4c6...",
            "order": 3,
        },
    )
    suggestion_ttl_hours: int = Field(
        default=24,
        ge=1,
        le=168,
        description="建议保留时长（小时），过期自动清理。",
        json_schema_extra={"label": "建议有效时间(小时)", "order": 4},
    )
    max_suggestions_in_context: int = Field(
        default=15,
        ge=1,
        le=50,
        description="每轮决策上下文携带的最大建议条数。",
        json_schema_extra={"label": "上下文建议上限", "order": 5},
    )


class DataSourceSection(PluginConfigBase):
    """[data_source] 行情与数据源设置。"""

    __ui_label__: ClassVar[str] = "行情与数据源"
    __ui_icon__: ClassVar[str] = "database"
    __ui_order__: ClassVar[int] = 5

    tencent_enabled: bool = Field(default=True, description="启用腾讯财经行情（主源）。", json_schema_extra={"label": "腾讯财经", "order": 1})
    sina_enabled: bool = Field(default=True, description="启用新浪财经行情（备用源）。", json_schema_extra={"label": "新浪财经", "order": 2})
    akshare_enabled: bool = Field(default=True, description="启用 AKShare（历史K线/候选池/交易日历）。", json_schema_extra={"label": "AKShare", "order": 3})
    quote_interval_minutes: int = Field(
        default=5,
        ge=1,
        le=60,
        description="交易时段行情监控间隔（分钟）。休市时自动降频。",
        json_schema_extra={"label": "行情监控间隔(分钟)", "order": 4},
    )
    request_timeout_seconds: float = Field(
        default=8.0,
        ge=2.0,
        le=30.0,
        description="行情请求超时（秒）。",
        json_schema_extra={"label": "请求超时(秒)", "order": 5},
    )
    retry_count: int = Field(
        default=2,
        ge=0,
        le=5,
        description="数据源失败重试次数（指数退避）。",
        json_schema_extra={"label": "重试次数", "order": 6},
    )
    quote_cache_ttl_seconds: float = Field(
        default=20.0,
        ge=5.0,
        le=300.0,
        description="行情缓存有效期（秒）。",
        json_schema_extra={"label": "缓存时间(秒)", "order": 7},
    )
    quote_max_age_seconds: int = Field(
        default=120,
        ge=30,
        le=3600,
        description="行情新鲜度阈值（秒）：交易时段内行情超过该年龄禁止成交。",
        json_schema_extra={"label": "行情过期阈值(秒)", "order": 8},
    )
    cross_source_tolerance_pct: float = Field(
        default=0.5,
        ge=0.05,
        le=10.0,
        description="双源价格差异容忍度（%），超过则标记行情异常并禁止成交。",
        json_schema_extra={"label": "双源差异阈值(%)", "order": 9},
    )
    request_interval_seconds: float = Field(
        default=0.5,
        ge=0.0,
        le=10.0,
        description="对同一数据源两次请求的最小间隔（秒，限速）。",
        json_schema_extra={"label": "请求限速(秒/次)", "order": 10},
    )


class AdvancedSection(PluginConfigBase):
    """[advanced] 高级设置。"""

    __ui_label__: ClassVar[str] = "高级"
    __ui_icon__: ClassVar[str] = "sliders"
    __ui_order__: ClassVar[int] = 6

    settlement_time: str = Field(
        default="15:10",
        description="每日结算执行时间（HH:MM，收盘后行情稳定时段）。",
        json_schema_extra={"label": "结算时间", "order": 1},
    )
    pool_refresh_time: str = Field(
        default="08:40",
        description="候选池每日刷新时间（HH:MM，开盘前）。",
        json_schema_extra={"label": "候选池刷新时间", "order": 2},
    )
    quiet_start: str = Field(
        default="23:00",
        description="播报静默时段开始（HH:MM）。",
        json_schema_extra={"label": "静默开始", "order": 3},
    )
    quiet_end: str = Field(
        default="07:30",
        description="播报静默时段结束（HH:MM）。",
        json_schema_extra={"label": "静默结束", "order": 4},
    )
    data_retention_days: int = Field(
        default=180,
        ge=30,
        le=3650,
        description="决策与建议数据保留天数（成交与净值记录永久保留）。",
        json_schema_extra={"label": "数据保留(天)", "order": 5},
    )
    log_level: str = Field(
        default="INFO",
        description="插件日志级别（DEBUG/INFO/WARNING/ERROR）。",
        json_schema_extra={"label": "日志级别", "order": 6},
    )


class MaiStockSimConfig(PluginConfigBase):
    """麦麦股票模拟盘完整配置。"""

    plugin: PluginSection = Field(default_factory=PluginSection)
    general: GeneralSection = Field(default_factory=GeneralSection)
    trade: TradeSection = Field(default_factory=TradeSection)
    decision: DecisionSection = Field(default_factory=DecisionSection)
    chat: ChatSection = Field(default_factory=ChatSection)
    data_source: DataSourceSection = Field(default_factory=DataSourceSection)
    advanced: AdvancedSection = Field(default_factory=AdvancedSection)

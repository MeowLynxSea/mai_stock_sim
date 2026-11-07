"""虚拟账户与账本（SQLite，事务化）。

全部账户变更通过 :class:`AccountStore` 的事务方法完成，保证
「扣款与持仓变更同生同灭」。所有表属于插件私有数据库，不触碰主程序业务表。
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..market.calendar import now_sh, today_sh
from ..market.models import Board
from ..market.symbols import exchange_and_board

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

_SCHEMA_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS schema_migrations (
        version INTEGER PRIMARY KEY,
        applied_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS accounts (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        initial_cash REAL NOT NULL,
        cash REAL NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS positions (
        symbol TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        board TEXT NOT NULL,
        qty INTEGER NOT NULL DEFAULT 0,
        avg_cost REAL NOT NULL DEFAULT 0.0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS position_lots (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        qty INTEGER NOT NULL
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS idx_lots_symbol ON position_lots(symbol, trade_date)
    """,
    """
    CREATE TABLE IF NOT EXISTS orders (
        order_id TEXT PRIMARY KEY,
        decision_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        intent_qty INTEGER,
        target_position_pct REAL,
        status TEXT NOT NULL,
        reject_reason TEXT,
        created_at TEXT NOT NULL,
        trade_date TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fills (
        fill_id TEXT PRIMARY KEY,
        order_id TEXT NOT NULL,
        symbol TEXT NOT NULL,
        side TEXT NOT NULL,
        qty INTEGER NOT NULL,
        price REAL NOT NULL,
        fee REAL NOT NULL,
        tax REAL NOT NULL,
        transfer_fee REAL NOT NULL,
        filled_at TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        quote_source TEXT NOT NULL,
        slippage_bps REAL NOT NULL DEFAULT 0.0,
        realized_pnl REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS cash_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_type TEXT NOT NULL,
        amount REAL NOT NULL,
        ref_id TEXT,
        note TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS corporate_actions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol TEXT NOT NULL,
        action_type TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending',
        payload TEXT,
        applied_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decisions (
        decision_id TEXT PRIMARY KEY,
        trade_date TEXT NOT NULL,
        market_view TEXT,
        reasoning TEXT,
        public_comment TEXT,
        raw_output TEXT,
        model TEXT NOT NULL DEFAULT '',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        next_review_minutes INTEGER,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS decision_inputs (
        decision_id TEXT NOT NULL,
        key TEXT NOT NULL,
        value TEXT,
        PRIMARY KEY (decision_id, key)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watchlist (
        symbol TEXT PRIMARY KEY,
        name TEXT,
        reason TEXT,
        added_by TEXT NOT NULL,
        added_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS stock_pool (
        symbol TEXT PRIMARY KEY,
        name TEXT,
        board TEXT,
        last_price REAL,
        pct_chg REAL,
        turnover REAL,
        market_cap REAL,
        turnover_rate REAL,
        risk_warning INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_bars (
        symbol TEXT NOT NULL,
        trade_date TEXT NOT NULL,
        open REAL, high REAL, low REAL, close REAL,
        volume REAL, amount REAL, pct_chg REAL,
        PRIMARY KEY (symbol, trade_date)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS daily_equity (
        trade_date TEXT PRIMARY KEY,
        cash REAL NOT NULL,
        market_value REAL NOT NULL,
        total_equity REAL NOT NULL,
        day_pnl REAL NOT NULL DEFAULT 0.0,
        realized_pnl_day REAL NOT NULL DEFAULT 0.0,
        positions_json TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS risk_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        symbol TEXT,
        rule TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS scheduler_runs (
        run_key TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        detail TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS model_usage (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trade_date TEXT NOT NULL,
        purpose TEXT NOT NULL,
        model TEXT NOT NULL DEFAULT '',
        prompt_tokens INTEGER NOT NULL DEFAULT 0,
        completion_tokens INTEGER NOT NULL DEFAULT 0,
        success INTEGER NOT NULL DEFAULT 1,
        created_at TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS suggestions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        stream_id TEXT NOT NULL,
        platform TEXT NOT NULL DEFAULT '',
        user_id TEXT NOT NULL DEFAULT '',
        user_name TEXT NOT NULL DEFAULT '',
        message_id TEXT,
        text TEXT NOT NULL,
        symbols_json TEXT,
        stance TEXT NOT NULL DEFAULT 'neutral',
        expires_at TEXT NOT NULL,
        created_at TEXT NOT NULL,
        decision_id TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS market_snapshots (
        trade_date TEXT NOT NULL,
        symbol TEXT NOT NULL,
        close REAL NOT NULL,
        source TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (trade_date, symbol)
    )
    """,
]


@dataclass
class AccountSnapshot:
    """账户快照。"""

    cash: float
    initial_cash: float
    positions: List[Dict[str, Any]]

    @property
    def market_value(self) -> float:
        """持仓市值合计。"""
        return sum(item["market_value"] for item in self.positions)

    @property
    def total_equity(self) -> float:
        """总权益。"""
        return self.cash + self.market_value


class AccountStore:
    """SQLite 账本。单写者模型：所有写操作在 ``self._write_lock`` 内执行。"""

    def __init__(self, db_path: Path, initial_cash: float = 1_000_000.0) -> None:
        """初始化并建库。

        Args:
            db_path: 数据库文件路径（插件 data_dir 下）。
            initial_cash: 初始虚拟资金（仅开户时生效）。
        """
        self._db_path = db_path
        self._initial_cash = initial_cash
        self._write_lock = threading.RLock()
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._migrate()

    # ── 连接管理 ──────────────────────────────────────────────

    def connection(self) -> sqlite3.Connection:
        """返回当前线程的 SQLite 连接（WAL 模式，外键开启）。"""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self._db_path, timeout=30.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            self._local.conn = conn
        return conn

    def conn_provider(self) -> Callable[[], sqlite3.Connection]:
        """返回连接工厂（供 HistoryService 等复用）。"""
        return self.connection

    def close(self) -> None:
        """关闭当前线程连接。"""
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    def _migrate(self) -> None:
        """建表与版本迁移。"""
        conn = self.connection()
        with self._write_lock:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
            )
            applied = {row[0] for row in conn.execute("SELECT version FROM schema_migrations")}
            for statement in _SCHEMA_STATEMENTS:
                conn.execute(statement)
            if SCHEMA_VERSION not in applied:
                conn.execute(
                    "INSERT OR REPLACE INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                    (SCHEMA_VERSION, now_sh().isoformat()),
                )
            conn.commit()

    # ── 开户与查询 ────────────────────────────────────────────

    def ensure_account(self, initial_cash: Optional[float] = None) -> bool:
        """确保虚拟账户存在。

        Returns:
            bool: 是否新开了账户。账户一经创建，初始资金不可通过配置修改。
        """
        conn = self.connection()
        with self._write_lock:
            row = conn.execute("SELECT id FROM accounts WHERE id = 1").fetchone()
            if row is not None:
                return False
            seed = self._initial_cash if initial_cash is None else initial_cash
            now = now_sh().isoformat()
            conn.execute(
                "INSERT INTO accounts(id, initial_cash, cash, created_at, updated_at) VALUES (1, ?, ?, ?, ?)",
                (seed, seed, now, now),
            )
            conn.execute(
                "INSERT INTO cash_events(event_type, amount, ref_id, note, created_at) VALUES (?, ?, ?, ?, ?)",
                ("initial", seed, None, "开户初始虚拟资金", now),
            )
            conn.commit()
            return True

    def get_cash(self) -> float:
        """返回可用现金。"""
        row = self.connection().execute("SELECT cash FROM accounts WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("虚拟账户尚未创建")
        return float(row["cash"])

    def get_initial_cash(self) -> float:
        """返回开户初始资金。"""
        row = self.connection().execute("SELECT initial_cash FROM accounts WHERE id = 1").fetchone()
        if row is None:
            raise RuntimeError("虚拟账户尚未创建")
        return float(row["initial_cash"])

    def get_positions(self) -> List[Dict[str, Any]]:
        """返回全部持仓（不含市值，估值由 settlement/调用方补充）。"""
        rows = self.connection().execute("SELECT * FROM positions WHERE qty > 0 ORDER BY symbol").fetchall()
        return [dict(row) for row in rows]

    def get_position(self, symbol: str) -> Optional[Dict[str, Any]]:
        """返回单只持仓。"""
        row = self.connection().execute(
            "SELECT * FROM positions WHERE symbol = ? AND qty > 0", (symbol,)
        ).fetchone()
        return dict(row) if row else None

    def sellable_qty(self, symbol: str, trade_date: Optional[str] = None) -> int:
        """按 T+1 计算可卖数量（今日之前买入的持仓部分）。"""
        day = trade_date or today_sh().isoformat()
        row = self.connection().execute("SELECT qty FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        if row is None:
            return 0
        total = int(row["qty"])
        lots_before = self.connection().execute(
            "SELECT COALESCE(SUM(qty), 0) FROM position_lots WHERE symbol = ? AND trade_date < ?",
            (symbol, day),
        ).fetchone()[0]
        return max(0, min(total, int(lots_before)))

    def account_exists(self) -> bool:
        """账户是否已创建。"""
        return (
            self.connection().execute("SELECT 1 FROM accounts WHERE id = 1").fetchone() is not None
        )

    # ── 交易入账（唯一写路径） ────────────────────────────────

    def record_decision(
        self,
        decision_id: str,
        market_view: str,
        reasoning: str,
        public_comment: str,
        raw_output: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        next_review_minutes: Optional[int],
        inputs: Optional[Dict[str, str]] = None,
    ) -> None:
        """记录一次决策及其输入摘要（审计用）。"""
        conn = self.connection()
        with self._write_lock:
            conn.execute(
                "INSERT OR IGNORE INTO decisions(decision_id, trade_date, market_view, reasoning, "
                "public_comment, raw_output, model, prompt_tokens, completion_tokens, next_review_minutes, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    today_sh().isoformat(),
                    market_view,
                    reasoning,
                    public_comment,
                    raw_output,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    next_review_minutes,
                    now_sh().isoformat(),
                ),
            )
            for key, value in (inputs or {}).items():
                conn.execute(
                    "INSERT OR REPLACE INTO decision_inputs(decision_id, key, value) VALUES (?, ?, ?)",
                    (decision_id, key, value),
                )
            conn.commit()

    def record_order(
        self,
        order_id: str,
        decision_id: str,
        symbol: str,
        side: str,
        intent_qty: Optional[int],
        target_position_pct: Optional[float],
        status: str,
        reject_reason: Optional[str],
    ) -> None:
        """记录订单（含拒绝的订单，附明确原因）。"""
        conn = self.connection()
        with self._write_lock:
            conn.execute(
                "INSERT OR REPLACE INTO orders(order_id, decision_id, symbol, side, intent_qty, "
                "target_position_pct, status, reject_reason, created_at, trade_date) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    order_id,
                    decision_id,
                    symbol,
                    side,
                    intent_qty,
                    target_position_pct,
                    status,
                    reject_reason,
                    now_sh().isoformat(),
                    today_sh().isoformat(),
                ),
            )
            conn.commit()

    def apply_fill(
        self,
        order_id: str,
        decision_id: str,
        symbol: str,
        name: str,
        side: str,
        qty: int,
        price: float,
        fee: float,
        tax: float,
        transfer_fee: float,
        quote_source: str,
        slippage_bps: float,
        filled_at: datetime,
    ) -> str:
        """入账一笔成交（事务：现金、持仓、批次、流水同生同灭）。

        Returns:
            str: fill_id。

        Raises:
            ValueError: 现金不足、可卖不足或重复成交时抛出，事务回滚。
        """
        fill_id = f"fill-{uuid.uuid4().hex[:16]}"
        trade_date = filled_at.date().isoformat()
        conn = self.connection()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                if conn.execute("SELECT 1 FROM fills WHERE fill_id = ?", (fill_id,)).fetchone():
                    raise ValueError("重复成交")
                # 幂等：同一订单不允许产生第二笔成交（本模型为整单一次性撮合）
                if conn.execute("SELECT 1 FROM fills WHERE order_id = ?", (order_id,)).fetchone():
                    raise ValueError(f"订单 {order_id} 已有成交，拒绝重复入账")
                cash_row = conn.execute("SELECT cash FROM accounts WHERE id = 1").fetchone()
                if cash_row is None:
                    raise ValueError("虚拟账户尚未创建")
                cash = float(cash_row["cash"])
                total_fee = round(fee + tax + transfer_fee, 2)

                if side == "BUY":
                    debit = round(price * qty + total_fee, 2)
                    if debit > cash + 1e-6:
                        raise ValueError(f"现金不足: 需要 {debit:.2f}，可用 {cash:.2f}")
                    conn.execute(
                        "UPDATE accounts SET cash = cash - ?, updated_at = ? WHERE id = 1",
                        (debit, now_sh().isoformat()),
                    )
                    self._apply_buy_position(conn, symbol, name, qty, price, total_fee, trade_date)
                    realized = None
                elif side == "SELL":
                    if self.sellable_qty_conn(conn, symbol, trade_date) < qty:
                        raise ValueError("可卖数量不足（T+1 或持仓缺失）")
                    credit = round(price * qty - total_fee, 2)
                    conn.execute(
                        "UPDATE accounts SET cash = cash + ?, updated_at = ? WHERE id = 1",
                        (credit, now_sh().isoformat()),
                    )
                    realized = self._apply_sell_position(conn, symbol, qty, price, total_fee, trade_date)
                else:
                    raise ValueError(f"非法交易方向: {side}")

                conn.execute(
                    "INSERT INTO fills(fill_id, order_id, symbol, side, qty, price, fee, tax, transfer_fee, "
                    "filled_at, trade_date, quote_source, slippage_bps, realized_pnl) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        fill_id,
                        order_id,
                        symbol,
                        side,
                        qty,
                        price,
                        fee,
                        tax,
                        transfer_fee,
                        filled_at.isoformat(),
                        trade_date,
                        quote_source,
                        slippage_bps,
                        realized,
                    ),
                )
                conn.execute(
                    "UPDATE orders SET status = 'filled', reject_reason = NULL WHERE order_id = ?",
                    (order_id,),
                )
                conn.execute(
                    "INSERT INTO cash_events(event_type, amount, ref_id, note, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        f"trade_{side.lower()}",
                        debit if side == "BUY" else credit,
                        fill_id,
                        f"{side} {symbol} {qty}股 @ {price}",
                        now_sh().isoformat(),
                    ),
                )
                conn.commit()
                return fill_id
            except Exception:
                conn.rollback()
                raise

    def _apply_buy_position(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        name: str,
        qty: int,
        price: float,
        total_fee: float,
        trade_date: str,
    ) -> None:
        """买入后的持仓与批次更新（调用方已在事务中）。"""
        info = exchange_and_board(symbol)
        board = info[1].value if info else Board.SH_MAIN.value
        row = conn.execute("SELECT qty, avg_cost FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        cost = round(price * qty + total_fee, 2)
        if row is None or row["qty"] == 0:
            conn.execute(
                "INSERT OR REPLACE INTO positions(symbol, name, board, qty, avg_cost, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (symbol, name, board, qty, cost / qty, now_sh().isoformat()),
            )
        else:
            old_qty = int(row["qty"])
            old_cost = float(row["avg_cost"]) * old_qty
            new_qty = old_qty + qty
            conn.execute(
                "UPDATE positions SET name = ?, qty = ?, avg_cost = ?, updated_at = ? WHERE symbol = ?",
                (name, new_qty, (old_cost + cost) / new_qty, now_sh().isoformat(), symbol),
            )
        conn.execute(
            "INSERT INTO position_lots(symbol, trade_date, qty) VALUES (?, ?, ?)",
            (symbol, trade_date, qty),
        )

    def _apply_sell_position(
        self,
        conn: sqlite3.Connection,
        symbol: str,
        qty: int,
        price: float,
        total_fee: float,
        trade_date: str,
    ) -> float:
        """卖出后的持仓与批次更新，返回已实现盈亏（调用方已在事务中）。"""
        row = conn.execute("SELECT qty, avg_cost, name, board FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        if row is None or int(row["qty"]) < qty:
            raise ValueError("持仓数量不足")
        avg_cost = float(row["avg_cost"])
        realized = round(price * qty - total_fee - avg_cost * qty, 2)
        remaining = int(row["qty"]) - qty
        conn.execute(
            "UPDATE positions SET qty = ?, updated_at = ? WHERE symbol = ?",
            (remaining, now_sh().isoformat(), symbol),
        )
        if remaining == 0:
            conn.execute("UPDATE positions SET avg_cost = 0.0 WHERE symbol = ?", (symbol,))
            conn.execute("DELETE FROM position_lots WHERE symbol = ?", (symbol,))
        else:
            # FIFO 消耗最早批次
            left = qty
            lots = conn.execute(
                "SELECT id, qty FROM position_lots WHERE symbol = ? AND trade_date < ? ORDER BY trade_date, id",
                (symbol, trade_date),
            ).fetchall()
            for lot in lots:
                if left <= 0:
                    break
                take = min(left, int(lot["qty"]))
                if take >= int(lot["qty"]):
                    conn.execute("DELETE FROM position_lots WHERE id = ?", (lot["id"],))
                else:
                    conn.execute("UPDATE position_lots SET qty = qty - ? WHERE id = ?", (take, lot["id"]))
                left -= take
            if left > 0:
                raise ValueError("批次数据与持仓不一致，事务回滚")
        return realized

    @staticmethod
    def sellable_qty_conn(conn: sqlite3.Connection, symbol: str, trade_date: str) -> int:
        """在既有事务/连接内计算可卖数量。"""
        row = conn.execute("SELECT qty FROM positions WHERE symbol = ?", (symbol,)).fetchone()
        if row is None:
            return 0
        total = int(row["qty"])
        lots_before = conn.execute(
            "SELECT COALESCE(SUM(qty), 0) FROM position_lots WHERE symbol = ? AND trade_date < ?",
            (symbol, trade_date),
        ).fetchone()[0]
        return max(0, min(total, int(lots_before)))

    # ── 公司行为 ──────────────────────────────────────────────

    def pending_corporate_actions(self) -> List[Dict[str, Any]]:
        """待处理公司行为列表。"""
        rows = self.connection().execute(
            "SELECT * FROM corporate_actions WHERE status = 'pending' ORDER BY trade_date"
        ).fetchall()
        return [dict(row) for row in rows]

    def record_corporate_action(self, symbol: str, action_type: str, trade_date: str, payload: Dict[str, Any]) -> None:
        """登记一条待处理公司行为（不自动改账）。"""
        conn = self.connection()
        with self._write_lock:
            conn.execute(
                "INSERT INTO corporate_actions(symbol, action_type, trade_date, status, payload) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (symbol, action_type, trade_date, json.dumps(payload, ensure_ascii=False)),
            )
            conn.commit()

    def apply_corporate_action(self, action_id: int, apply_fn: Callable[[sqlite3.Connection], None]) -> None:
        """在事务中执行公司行为调整并标记完成。

        Args:
            action_id: 待处理事件 ID。
            apply_fn: 收到连接的回调，负责现金/持仓调整；抛错则整体回滚。
        """
        conn = self.connection()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                apply_fn(conn)
                conn.execute(
                    "UPDATE corporate_actions SET status = 'applied', applied_at = ? WHERE id = ?",
                    (now_sh().isoformat(), action_id),
                )
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    # ── 管理员重置（账户管理操作，非市场交易） ─────────────────

    def admin_reset(
        self,
        new_initial_cash: float,
        price_lookup,
        operator: str = "",
    ) -> Dict[str, Any]:
        """管理员重置：清仓并把现金与初始资金重设为指定值。

        这是账户管理操作而非市场交易：清仓不经过撮合引擎，按参考价
        （最新缓存价，缺省用成本价）折算价值并全部记入审计事件；
        历史成交、决策与净值记录**保留不删**，重置本身也写入 cash_events
        与 risk_events 留痕。

        Args:
            new_initial_cash: 重设后的初始资金（也是新现金余额）。
            price_lookup: symbol -> 参考价回调（None 时用持仓成本价）。
            operator: 执行者标识（审计用）。

        Returns:
            Dict[str, Any]: 重置摘要（清仓明细、清零前权益、新初始资金等）。
        """
        if new_initial_cash <= 0:
            raise ValueError("新初始资金必须为正数")
        conn = self.connection()
        now = now_sh().isoformat()
        today = today_sh().isoformat()
        with self._write_lock:
            conn.execute("BEGIN IMMEDIATE")
            try:
                # 1. 清仓：按参考价折算价值计入现金（审计逐票留痕）
                liquidated: List[Dict[str, Any]] = []
                positions = conn.execute("SELECT * FROM positions WHERE qty > 0").fetchall()
                total_liquidation = 0.0
                for row in positions:
                    qty = int(row["qty"])
                    price = price_lookup(row["symbol"])
                    if price is None or price <= 0:
                        price = float(row["avg_cost"])
                    value = round(price * qty, 2)
                    total_liquidation += value
                    liquidated.append(
                        {"symbol": row["symbol"], "name": row["name"], "qty": qty, "price": price, "value": value}
                    )
                    conn.execute(
                        "INSERT INTO cash_events(event_type, amount, ref_id, note, created_at) VALUES (?, ?, ?, ?, ?)",
                        (
                            "admin_liquidation",
                            value,
                            None,
                            f"管理员重置清仓 {row['symbol']} {qty}股 @ {price:.2f}",
                            now,
                        ),
                    )
                if total_liquidation:
                    conn.execute("UPDATE accounts SET cash = cash + ?, updated_at = ? WHERE id = 1", (round(total_liquidation, 2), now))
                conn.execute("UPDATE positions SET qty = 0, avg_cost = 0.0, updated_at = ? WHERE qty > 0", (now,))
                conn.execute("DELETE FROM position_lots")

                # 2. 重设现金与初始资金（清零前状态留痕）
                before = conn.execute("SELECT cash, initial_cash FROM accounts WHERE id = 1").fetchone()
                conn.execute(
                    "INSERT INTO cash_events(event_type, amount, ref_id, note, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        "admin_reset",
                        round(float(new_initial_cash), 2),
                        None,
                        f"管理员重置账户：清零前现金 {float(before['cash']):.2f}（初始资金 {float(before['initial_cash']):.2f}）"
                        f"→ 新初始资金 {new_initial_cash:.2f}；执行者 {operator or '未标识'}",
                        now,
                    ),
                )
                conn.execute(
                    "UPDATE accounts SET initial_cash = ?, cash = ?, updated_at = ? WHERE id = 1",
                    (round(float(new_initial_cash), 2), round(float(new_initial_cash), 2), now),
                )
                conn.execute(
                    "INSERT INTO risk_events(trade_date, symbol, rule, detail, created_at) VALUES (?, NULL, ?, ?, ?)",
                    (today, "admin_reset", f"账户被管理员重置，新初始资金 {new_initial_cash:.2f}，执行者 {operator or '未标识'}", now),
                )

                # 3. 删除今日净值快照（如有），收盘结算按新状态重建
                conn.execute("DELETE FROM daily_equity WHERE trade_date >= ?", (today,))

                # 4. 今日风控/成交频率计数基于 fills 表（历史保留），重置后自然重新累计
                conn.commit()
            except Exception:
                conn.rollback()
                raise
        return {
            "liquidated": liquidated,
            "liquidation_total": round(total_liquidation, 2),
            "cash_before": round(float(before["cash"]), 2),
            "new_initial_cash": round(float(new_initial_cash), 2),
        }

    # ── 结算与快照 ────────────────────────────────────────────

    def save_daily_equity(
        self,
        trade_date: str,
        cash: float,
        market_value: float,
        day_pnl: float,
        realized_pnl_day: float,
        positions_json: str,
    ) -> bool:
        """保存每日净值快照（幂等：同一交易日重复保存返回 False）。"""
        conn = self.connection()
        with self._write_lock:
            if conn.execute("SELECT 1 FROM daily_equity WHERE trade_date = ?", (trade_date,)).fetchone():
                return False
            conn.execute(
                "INSERT INTO daily_equity(trade_date, cash, market_value, total_equity, day_pnl, "
                "realized_pnl_day, positions_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    trade_date,
                    cash,
                    market_value,
                    round(cash + market_value, 2),
                    day_pnl,
                    realized_pnl_day,
                    positions_json,
                    now_sh().isoformat(),
                ),
            )
            conn.commit()
            return True

    def get_daily_equity(self, trade_date: str) -> Optional[Dict[str, Any]]:
        """读取某日净值快照。"""
        row = self.connection().execute(
            "SELECT * FROM daily_equity WHERE trade_date = ?", (trade_date,)
        ).fetchone()
        return dict(row) if row else None

    def equity_history(self, limit: int = 30) -> List[Dict[str, Any]]:
        """最近的净值序列（升序）。"""
        rows = self.connection().execute(
            "SELECT * FROM daily_equity ORDER BY trade_date DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(row) for row in reversed(rows)]

    def save_market_snapshot(self, trade_date: str, symbol: str, close: float, source: str) -> None:
        """保存收盘快照（估值依据，未复权）。"""
        conn = self.connection()
        with self._write_lock:
            conn.execute(
                "INSERT OR REPLACE INTO market_snapshots(trade_date, symbol, close, source, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (trade_date, symbol, close, source, now_sh().isoformat()),
            )
            conn.commit()

    def get_market_snapshot(self, trade_date: str, symbol: str) -> Optional[float]:
        """读取收盘快照。"""
        row = self.connection().execute(
            "SELECT close FROM market_snapshots WHERE trade_date = ? AND symbol = ?",
            (trade_date, symbol),
        ).fetchone()
        return float(row["close"]) if row else None

    # ── 订单与成交查询 ────────────────────────────────────────

    def recent_fills(self, limit: int = 20) -> List[Dict[str, Any]]:
        """最近成交记录。"""
        rows = self.connection().execute(
            "SELECT * FROM fills ORDER BY filled_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(row) for row in rows]

    def recent_orders(self, limit: int = 20) -> List[Dict[str, Any]]:
        """最近订单记录。"""
        rows = self.connection().execute(
            "SELECT * FROM orders ORDER BY created_at DESC LIMIT ?", (int(limit),)
        ).fetchall()
        return [dict(row) for row in rows]

    def fills_on(self, trade_date: str) -> List[Dict[str, Any]]:
        """某交易日的全部成交。"""
        rows = self.connection().execute(
            "SELECT * FROM fills WHERE trade_date = ? ORDER BY filled_at", (trade_date,)
        ).fetchall()
        return [dict(row) for row in rows]

    def day_trade_count(self, trade_date: Optional[str] = None) -> int:
        """当日成交笔数（风控用）。"""
        day = trade_date or today_sh().isoformat()
        return int(
            self.connection().execute("SELECT COUNT(*) FROM fills WHERE trade_date = ?", (day,)).fetchone()[0]
        )

    def day_realized_pnl(self, trade_date: Optional[str] = None) -> float:
        """当日已实现盈亏合计。"""
        day = trade_date or today_sh().isoformat()
        row = self.connection().execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM fills WHERE trade_date = ? AND realized_pnl IS NOT NULL",
            (day,),
        ).fetchone()
        return float(row[0])

    def total_realized_pnl(self) -> float:
        """累计已实现盈亏。"""
        row = self.connection().execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM fills WHERE realized_pnl IS NOT NULL"
        ).fetchone()
        return float(row[0])

    # ── 观察列表与候选池 ──────────────────────────────────────

    def upsert_watchlist(self, symbol: str, name: str, reason: str, added_by: str) -> None:
        """添加或更新观察列表。"""
        conn = self.connection()
        with self._write_lock:
            conn.execute(
                "INSERT INTO watchlist(symbol, name, reason, added_by, added_at) VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(symbol) DO UPDATE SET name = excluded.name, reason = excluded.reason",
                (symbol, name, reason, added_by, now_sh().isoformat()),
            )
            conn.commit()

    def remove_watchlist(self, symbol: str) -> bool:
        """移除观察项。"""
        conn = self.connection()
        with self._write_lock:
            cursor = conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))
            conn.commit()
            return cursor.rowcount > 0

    def get_watchlist(self) -> List[Dict[str, Any]]:
        """观察列表。"""
        rows = self.connection().execute("SELECT * FROM watchlist ORDER BY added_at DESC").fetchall()
        return [dict(row) for row in rows]

    def replace_stock_pool(self, records: Sequence[Dict[str, Any]]) -> int:
        """全量替换候选池。"""
        conn = self.connection()
        now = now_sh().isoformat()
        with self._write_lock:
            conn.execute("DELETE FROM stock_pool")
            conn.executemany(
                "INSERT OR REPLACE INTO stock_pool(symbol, name, board, last_price, pct_chg, turnover, "
                "market_cap, turnover_rate, risk_warning, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    (
                        str(record["symbol"]),
                        str(record.get("name", "")),
                        str(record.get("board", "")),
                        float(record.get("last_price") or 0.0),
                        record.get("pct_chg"),
                        record.get("turnover"),
                        record.get("market_cap"),
                        record.get("turnover_rate"),
                        1 if record.get("risk_warning") else 0,
                        now,
                    )
                    for record in records
                ],
            )
            conn.commit()
            return len(records)

    def stock_pool_rows(self, turnover_min: float = 0.0) -> List[Dict[str, Any]]:
        """读取候选池记录。"""
        rows = self.connection().execute(
            "SELECT * FROM stock_pool WHERE turnover >= ? ORDER BY turnover DESC", (turnover_min,)
        ).fetchall()
        return [dict(row) for row in rows]

    def stock_name(self, symbol: str) -> str:
        """查询股票名称（候选池/持仓/观察列表任一来源）。"""
        conn = self.connection()
        for table in ("positions", "stock_pool", "watchlist"):
            row = conn.execute(f"SELECT name FROM {table} WHERE symbol = ?", (symbol,)).fetchone()  # noqa: S608
            if row is not None and row["name"]:
                return str(row["name"])
        return symbol

    # ── 群聊建议 ──────────────────────────────────────────────

    def insert_suggestion(self, record: Dict[str, Any]) -> int:
        """保存一条群聊建议。"""
        conn = self.connection()
        with self._write_lock:
            cursor = conn.execute(
                "INSERT INTO suggestions(stream_id, platform, user_id, user_name, message_id, text, "
                "symbols_json, stance, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record["stream_id"],
                    record.get("platform", ""),
                    record.get("user_id", ""),
                    record.get("user_name", ""),
                    record.get("message_id"),
                    record["text"],
                    json.dumps(record.get("symbols", []), ensure_ascii=False),
                    record.get("stance", "neutral"),
                    record["expires_at"],
                    record.get("created_at", now_sh().isoformat()),
                ),
            )
            conn.commit()
            return int(cursor.lastrowid or 0)

    def fresh_suggestions(self, limit: int = 30) -> List[Dict[str, Any]]:
        """读取未过期且未被决策消费过的建议。"""
        now = now_sh().isoformat()
        rows = self.connection().execute(
            "SELECT * FROM suggestions WHERE expires_at > ? AND decision_id IS NULL "
            "ORDER BY created_at DESC LIMIT ?",
            (now, int(limit)),
        ).fetchall()
        results: List[Dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["symbols"] = json.loads(item.get("symbols_json") or "[]")
            results.append(item)
        return results

    def mark_suggestions_consumed(self, suggestion_ids: Sequence[int], decision_id: str) -> None:
        """把建议标记为已被某决策参考过。"""
        if not suggestion_ids:
            return
        conn = self.connection()
        with self._write_lock:
            conn.executemany(
                "UPDATE suggestions SET decision_id = ? WHERE id = ?",
                [(decision_id, sid) for sid in suggestion_ids],
            )
            conn.commit()

    def cleanup_expired_suggestions(self) -> int:
        """清理过期建议。"""
        conn = self.connection()
        with self._write_lock:
            cursor = conn.execute(
                "DELETE FROM suggestions WHERE expires_at < ?", (now_sh().isoformat(),)
            )
            conn.commit()
            return cursor.rowcount or 0

    # ── 风控与调度记录 ────────────────────────────────────────

    def record_risk_event(self, rule: str, symbol: Optional[str], detail: str) -> None:
        """记录风控事件。"""
        conn = self.connection()
        with self._write_lock:
            conn.execute(
                "INSERT INTO risk_events(trade_date, symbol, rule, detail, created_at) VALUES (?, ?, ?, ?, ?)",
                (today_sh().isoformat(), symbol, rule, detail, now_sh().isoformat()),
            )
            conn.commit()

    def day_risk_events(self, trade_date: Optional[str] = None) -> List[Dict[str, Any]]:
        """当日风控事件。"""
        day = trade_date or today_sh().isoformat()
        rows = self.connection().execute(
            "SELECT * FROM risk_events WHERE trade_date = ? ORDER BY created_at", (day,)
        ).fetchall()
        return [dict(row) for row in rows]

    def try_begin_run(self, run_key: str) -> bool:
        """尝试登记一次调度运行（互斥/幂等）。

        Returns:
            bool: True 表示获得运行权；False 表示该 run_key 已在执行或已完成。
        """
        conn = self.connection()
        now = now_sh().isoformat()
        with self._write_lock:
            try:
                conn.execute(
                    "INSERT INTO scheduler_runs(run_key, status, started_at) VALUES (?, 'running', ?)",
                    (run_key, now),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                row = conn.execute("SELECT status FROM scheduler_runs WHERE run_key = ?", (run_key,)).fetchone()
                if row is not None and row["status"] == "running":
                    # 可能是崩溃残留：超过 2 小时的 running 记录允许重入
                    started = conn.execute(
                        "SELECT started_at FROM scheduler_runs WHERE run_key = ?", (run_key,)
                    ).fetchone()["started_at"]
                    try:
                        started_at = datetime.fromisoformat(str(started))
                        if (now_sh() - started_at).total_seconds() > 7200:
                            conn.execute(
                                "UPDATE scheduler_runs SET status = 'stale', finished_at = ? WHERE run_key = ?",
                                (now, run_key),
                            )
                            conn.commit()
                            return False
                    except ValueError:
                        pass
                return False

    def finish_run(self, run_key: str, status: str = "done", detail: str = "") -> None:
        """标记一次调度运行结束。"""
        conn = self.connection()
        with self._write_lock:
            conn.execute(
                "UPDATE scheduler_runs SET status = ?, finished_at = ?, detail = ? WHERE run_key = ?",
                (status, now_sh().isoformat(), detail, run_key),
            )
            conn.commit()

    def record_model_usage(
        self,
        purpose: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        success: bool,
    ) -> None:
        """记录一次模型调用（预算统计）。"""
        conn = self.connection()
        with self._write_lock:
            conn.execute(
                "INSERT INTO model_usage(trade_date, purpose, model, prompt_tokens, completion_tokens, success, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    today_sh().isoformat(),
                    purpose,
                    model,
                    prompt_tokens,
                    completion_tokens,
                    1 if success else 0,
                    now_sh().isoformat(),
                ),
            )
            conn.commit()

    def day_model_usage(self, trade_date: Optional[str] = None) -> Dict[str, int]:
        """当日模型用量统计。"""
        day = trade_date or today_sh().isoformat()
        row = self.connection().execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(prompt_tokens), 0) AS prompt_tokens, "
            "COALESCE(SUM(completion_tokens), 0) AS completion_tokens "
            "FROM model_usage WHERE trade_date = ?",
            (day,),
        ).fetchone()
        return {
            "calls": int(row["calls"]),
            "prompt_tokens": int(row["prompt_tokens"]),
            "completion_tokens": int(row["completion_tokens"]),
        }

    def day_decision_count(self, trade_date: Optional[str] = None) -> int:
        """当日决策次数（预算控制）。"""
        day = trade_date or today_sh().isoformat()
        return int(
            self.connection().execute(
                "SELECT COUNT(*) FROM decisions WHERE trade_date = ?", (day,)
            ).fetchone()[0]
        )

    # ── 不变量检查 ────────────────────────────────────────────

    def verify_invariants(self, price_lookup: Callable[[str], Optional[float]]) -> List[str]:
        """检查账户不变量。

        Args:
            price_lookup: 返回某股票最新估值价格的回调。

        Returns:
            List[str]: 违规描述列表；空列表表示全部通过。
        """
        problems: List[str] = []
        conn = self.connection()
        cash = float(conn.execute("SELECT cash FROM accounts WHERE id = 1").fetchone()["cash"])
        if cash < -1e-6:
            problems.append(f"现金为负: {cash}")
        market_value = 0.0
        for row in conn.execute("SELECT symbol, qty FROM positions").fetchall():
            qty = int(row["qty"])
            if qty < 0:
                problems.append(f"{row['symbol']} 持仓为负: {qty}")
                continue
            if qty > 0:
                price = price_lookup(row["symbol"])
                if price is None:
                    problems.append(f"{row['symbol']} 无法估值（缺行情）")
                else:
                    market_value += price * qty
                lots_total = conn.execute(
                    "SELECT COALESCE(SUM(qty), 0) FROM position_lots WHERE symbol = ?", (row["symbol"],)
                ).fetchone()[0]
                if int(lots_total) != qty:
                    problems.append(
                        f"{row['symbol']} 批次合计 {lots_total} 与持仓 {qty} 不一致"
                    )
        # 成交重复入账检查（每订单最多一笔成交）
        dup = conn.execute(
            "SELECT order_id FROM fills GROUP BY order_id HAVING COUNT(*) > 1"
        ).fetchall()
        for row in dup:
            problems.append(f"订单 {row['order_id']} 存在重复成交")
        return problems

    def snapshot(self, price_lookup: Callable[[str], Optional[float]]) -> AccountSnapshot:
        """返回带估值的账户快照。"""
        positions: List[Dict[str, Any]] = []
        for row in self.get_positions():
            price = price_lookup(row["symbol"])
            qty = int(row["qty"])
            market_value = round(price * qty, 2) if price is not None else 0.0
            positions.append(
                {
                    **row,
                    "last_price": price,
                    "market_value": market_value,
                    "unrealized_pnl": (
                        round((price - float(row["avg_cost"])) * qty, 2) if price is not None else None
                    ),
                    "sellable": self.sellable_qty(row["symbol"]),
                }
            )
        return AccountSnapshot(
            cash=self.get_cash(),
            initial_cash=self.get_initial_cash(),
            positions=positions,
        )

    def check_profit_consistency(self) -> Tuple[float, float, float]:
        """校验现金流水一致性：初始资金 + 交易净现金流 + 分红等 = 当前现金。

        Returns:
            tuple: (初始资金, 交易净现金流, 当前现金)。
        """
        conn = self.connection()
        initial = float(conn.execute("SELECT initial_cash FROM accounts WHERE id = 1").fetchone()["initial_cash"])
        net = 0.0
        for row in conn.execute("SELECT * FROM cash_events").fetchall():
            amount = float(row["amount"])
            if row["event_type"] == "initial":
                continue
            if row["event_type"] == "trade_buy":
                net -= amount  # 买入记录的是实际扣款额（含费用）
            elif row["event_type"] in ("trade_sell", "dividend", "corporate_action"):
                net += amount  # 卖出记录的是实际到账额（扣除费用）
        return initial, round(net, 2), self.get_cash()

"""Capa de persistencia SQLite (sqlite3 de la librería estándar)."""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import pandas as pd

SCHEMA = """
CREATE TABLE IF NOT EXISTS market_data (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker  TEXT    NOT NULL,
    date    TEXT    NOT NULL,
    open    REAL,
    high    REAL,
    low     REAL,
    close   REAL,
    volume  REAL,
    UNIQUE (ticker, date)
);

CREATE TABLE IF NOT EXISTS fundamentals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker       TEXT NOT NULL,
    date         TEXT NOT NULL,
    pe_ratio     REAL,
    eps_growth   REAL,
    debt_equity  REAL,
    fcf          REAL,
    UNIQUE (ticker, date)
);

CREATE TABLE IF NOT EXISTS signals_log (
    id                        INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp                 TEXT NOT NULL,
    ticker                    TEXT NOT NULL,
    fundamental_score         REAL,
    technical_score           REAL,
    sentiment_score           REAL,
    final_score               INTEGER,
    recommended_action        TEXT NOT NULL
        CHECK (recommended_action IN ('BUY', 'HOLD', 'REDUCE', 'SELL')),
    confidence_level          TEXT CHECK (confidence_level IN ('LOW', 'MEDIUM', 'HIGH')),
    stop_loss_calculated      REAL,
    position_size_calculated  REAL,
    llm_justification         TEXT
);

-- Noticias corporativas ya notificadas (evita repetir la misma alerta cada noche).
CREATE TABLE IF NOT EXISTS corporate_news_seen (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    company   TEXT NOT NULL,
    news_key  TEXT NOT NULL,
    title     TEXT,
    seen_at   TEXT NOT NULL,
    UNIQUE (company, news_key)
);

-- Cartera real: una fila por activo. precio_medio_compra en la divisa del activo.
CREATE TABLE IF NOT EXISTS portfolio (
    ticker               TEXT PRIMARY KEY,
    nombre_activo        TEXT,
    cantidad             REAL NOT NULL CHECK (cantidad > 0),
    precio_medio_compra  REAL NOT NULL CHECK (precio_medio_compra > 0),
    divisa               TEXT NOT NULL DEFAULT 'EUR'
);

-- Capital líquido (cash) en EUR. Una sola fila (id = 1).
CREATE TABLE IF NOT EXISTS capital (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    cash_eur    REAL NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Histórico de operaciones registradas desde Telegram o init_portfolio.py.
CREATE TABLE IF NOT EXISTS trades (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp           TEXT NOT NULL,
    ticker              TEXT NOT NULL,
    side                TEXT NOT NULL CHECK (side IN ('BUY', 'SELL', 'INIT')),
    cantidad            REAL NOT NULL,
    precio              REAL NOT NULL,
    divisa              TEXT NOT NULL,
    fx_to_eur           REAL NOT NULL,
    importe_eur         REAL NOT NULL,
    pnl_realizado_eur   REAL
);

CREATE INDEX IF NOT EXISTS idx_market_ticker_date ON market_data (ticker, date);
CREATE INDEX IF NOT EXISTS idx_signals_ticker_ts ON signals_log (ticker, timestamp);
"""


class Database:
    def __init__(self, path: str = "portfolio.db") -> None:
        self.path = path
        with self._conn() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ------------------------------------------------------------ market_data
    def upsert_market_data(self, ticker: str, ohlcv: pd.DataFrame) -> int:
        rows = [
            (
                ticker,
                pd.Timestamp(idx).strftime("%Y-%m-%d"),
                float(r["open"]), float(r["high"]), float(r["low"]),
                float(r["close"]), float(r["volume"]),
            )
            for idx, r in ohlcv.iterrows()
        ]
        with self._conn() as conn:
            conn.executemany(
                """INSERT INTO market_data (ticker, date, open, high, low, close, volume)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ticker, date) DO UPDATE SET
                     open=excluded.open, high=excluded.high, low=excluded.low,
                     close=excluded.close, volume=excluded.volume""",
                rows,
            )
        return len(rows)

    # ------------------------------------------------------------ fundamentals
    def upsert_fundamentals(
        self,
        ticker: str,
        pe_ratio: float | None,
        eps_growth: float | None,
        debt_equity: float | None,
        fcf: float | None,
        date: str | None = None,
    ) -> None:
        date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO fundamentals (ticker, date, pe_ratio, eps_growth, debt_equity, fcf)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(ticker, date) DO UPDATE SET
                     pe_ratio=excluded.pe_ratio, eps_growth=excluded.eps_growth,
                     debt_equity=excluded.debt_equity, fcf=excluded.fcf""",
                (ticker, date, pe_ratio, eps_growth, debt_equity, fcf),
            )

    # ------------------------------------------------------------- signals_log
    def log_signal(
        self,
        ticker: str,
        fundamental_score: float | None,
        technical_score: float | None,
        sentiment_score: float | None,
        final_score: int,
        recommended_action: str,
        confidence_level: str,
        stop_loss_calculated: float | None,
        position_size_calculated: float | None,
        llm_justification: str,
        timestamp: str | None = None,
    ) -> int:
        timestamp = timestamp or datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT INTO signals_log (
                       timestamp, ticker, fundamental_score, technical_score, sentiment_score,
                       final_score, recommended_action, confidence_level,
                       stop_loss_calculated, position_size_calculated, llm_justification)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    timestamp, ticker, fundamental_score, technical_score, sentiment_score,
                    final_score, recommended_action, confidence_level,
                    stop_loss_calculated, position_size_calculated, llm_justification,
                ),
            )
            return int(cur.lastrowid)

    def recent_signals(self, ticker: str, days: int) -> list[dict[str, Any]]:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals_log WHERE ticker = ? AND timestamp >= ? ORDER BY timestamp DESC",
                (ticker, since),
            ).fetchall()
        return [dict(r) for r in rows]

    def last_signals(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals_log ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def signals_between(self, start: datetime, end: datetime) -> list[dict[str, Any]]:
        """Señales con `start <= timestamp < end` (fechas con zona horaria, se comparan en UTC)."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM signals_log WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp",
                (
                    start.astimezone(timezone.utc).isoformat(timespec="seconds"),
                    end.astimezone(timezone.utc).isoformat(timespec="seconds"),
                ),
            ).fetchall()
        return [dict(r) for r in rows]

    def close_on_or_before(self, ticker: str, date: str) -> float | None:
        """Último cierre guardado en `market_data` hasta `date` (YYYY-MM-DD) inclusive."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT close FROM market_data WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 1",
                (ticker, date),
            ).fetchone()
        return None if row is None else float(row["close"])

    # ----------------------------------------------------- corporate_news_seen
    def mark_news_seen(self, company: str, news_key: str, title: str = "") -> bool:
        """Registra la noticia. Devuelve True si era nueva, False si ya se había visto."""
        with self._conn() as conn:
            cur = conn.execute(
                """INSERT OR IGNORE INTO corporate_news_seen (company, news_key, title, seen_at)
                   VALUES (?, ?, ?, ?)""",
                (company, news_key, title, datetime.now(timezone.utc).isoformat(timespec="seconds")),
            )
            return cur.rowcount == 1

    # ---------------------------------------------------------------- capital
    def get_cash(self) -> float | None:
        """Capital líquido en EUR, o None si la cartera no se ha inicializado."""
        with self._conn() as conn:
            row = conn.execute("SELECT cash_eur FROM capital WHERE id = 1").fetchone()
        return None if row is None else float(row["cash_eur"])

    def set_cash(self, cash_eur: float, conn: sqlite3.Connection | None = None) -> None:
        sql = """INSERT INTO capital (id, cash_eur, updated_at) VALUES (1, ?, ?)
                 ON CONFLICT(id) DO UPDATE SET cash_eur=excluded.cash_eur, updated_at=excluded.updated_at"""
        args = (float(cash_eur), datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if conn is not None:
            conn.execute(sql, args)
            return
        with self._conn() as c:
            c.execute(sql, args)

    # -------------------------------------------------------------- portfolio
    def get_positions(self) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM portfolio ORDER BY ticker").fetchall()
        return [dict(r) for r in rows]

    def get_position(self, ticker: str) -> dict[str, Any] | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM portfolio WHERE ticker = ?", (ticker,)).fetchone()
        return None if row is None else dict(row)

    def reset_portfolio(self, positions: list[dict[str, Any]], cash_eur: float) -> None:
        """Borra la cartera y carga `positions` + cash en una sola transacción."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self._conn() as conn:
            conn.execute("DELETE FROM portfolio")
            for p in positions:
                conn.execute(
                    """INSERT INTO portfolio (ticker, nombre_activo, cantidad, precio_medio_compra, divisa)
                       VALUES (?, ?, ?, ?, ?)""",
                    (p["ticker"], p.get("nombre_activo"), p["cantidad"], p["precio_medio_compra"], p["divisa"]),
                )
                conn.execute(
                    """INSERT INTO trades (timestamp, ticker, side, cantidad, precio, divisa, fx_to_eur, importe_eur)
                       VALUES (?, ?, 'INIT', ?, ?, ?, 1.0, 0.0)""",
                    (now, p["ticker"], p["cantidad"], p["precio_medio_compra"], p["divisa"]),
                )
            self.set_cash(cash_eur, conn)

    def apply_trade(
        self,
        ticker: str,
        side: str,
        cantidad: float,
        precio: float,
        divisa: str,
        fx_to_eur: float,
        new_position: dict[str, Any] | None,
        new_cash_eur: float,
        pnl_realizado_eur: float | None = None,
    ) -> None:
        """Escribe posición, cash y operación de forma atómica. `new_position=None` borra la posición."""
        with self._conn() as conn:
            if new_position is None:
                conn.execute("DELETE FROM portfolio WHERE ticker = ?", (ticker,))
            else:
                conn.execute(
                    """INSERT INTO portfolio (ticker, nombre_activo, cantidad, precio_medio_compra, divisa)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(ticker) DO UPDATE SET
                         nombre_activo=excluded.nombre_activo, cantidad=excluded.cantidad,
                         precio_medio_compra=excluded.precio_medio_compra, divisa=excluded.divisa""",
                    (ticker, new_position.get("nombre_activo"), new_position["cantidad"],
                     new_position["precio_medio_compra"], new_position["divisa"]),
                )
            self.set_cash(new_cash_eur, conn)
            conn.execute(
                """INSERT INTO trades (timestamp, ticker, side, cantidad, precio, divisa, fx_to_eur,
                                       importe_eur, pnl_realizado_eur)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (datetime.now(timezone.utc).isoformat(timespec="seconds"), ticker, side, cantidad, precio,
                 divisa, fx_to_eur, cantidad * precio * fx_to_eur, pnl_realizado_eur),
            )

    def trades(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._conn() as conn:
            rows = conn.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

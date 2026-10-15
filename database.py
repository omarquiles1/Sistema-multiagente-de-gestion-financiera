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

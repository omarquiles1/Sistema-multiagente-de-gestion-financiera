"""Descarga de datos crudos: OHLCV, fundamentales, noticias, divisa y tipos de interés."""
from __future__ import annotations

import io
import logging

import pandas as pd
import requests

from config import SETTINGS, Settings
from models import MarketSnapshot

log = logging.getLogger(__name__)

FRED_FED_FUNDS_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFF"
ECB_DEPOSIT_RATE_CSV = (
    "https://data-api.ecb.europa.eu/service/data/FM/D.U2.EUR.4F.KR.DFR.LEV"
    "?lastNObservations=1&format=csvdata"
)
BENCHMARK = "^GSPC"


class DataFetcher:
    """Encapsula yfinance y las fuentes macro para que los agentes no toquen la red."""

    def __init__(self, settings: Settings = SETTINGS, http_timeout: float = 10.0) -> None:
        self.settings = settings
        self.http_timeout = http_timeout

    # -------------------------------------------------------------- público
    def fetch(self, ticker: str) -> MarketSnapshot:
        import yfinance as yf

        t = yf.Ticker(ticker)
        ohlcv = self._ohlcv(t)
        if ohlcv.empty:
            raise ValueError(f"Sin datos de precio para {ticker}")

        info = self._safe(lambda: t.info, {}) or {}
        currency = info.get("currency") or "USD"
        ohlcv, currency = self._normalize_minor_currency(ohlcv, currency)

        return MarketSnapshot(
            ticker=ticker.upper(),
            ohlcv=ohlcv,
            info=info,
            income_stmt=self._safe(lambda: t.income_stmt, None),
            cashflow=self._safe(lambda: t.cashflow, None),
            news_headlines=self._news(t),
            currency=currency,
            fx_to_eur=self._fx_to_eur(currency),
            fed_rate=self._fed_rate(),
            ecb_rate=self._ecb_rate(),
            benchmark_close=self._benchmark(),
        )

    # -------------------------------------------------------------- precios
    def _ohlcv(self, t) -> pd.DataFrame:
        df = t.history(period=self.settings.history_period, interval="1d", auto_adjust=True)
        if df is None or df.empty:
            return pd.DataFrame()
        df = df.rename(columns=str.lower)[["open", "high", "low", "close", "volume"]]
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_localize(None)
        return df.dropna(subset=["close"])

    @staticmethod
    def _normalize_minor_currency(df: pd.DataFrame, currency: str) -> tuple[pd.DataFrame, str]:
        # Bolsa de Londres cotiza en peniques (GBp / GBX).
        if currency in ("GBp", "GBX"):
            df = df.copy()
            df[["open", "high", "low", "close"]] /= 100.0
            return df, "GBP"
        return df, currency

    def _benchmark(self) -> pd.Series | None:
        import yfinance as yf

        def load():
            h = yf.Ticker(BENCHMARK).history(period=self.settings.history_period, auto_adjust=True)
            s = h["Close"]
            if getattr(s.index, "tz", None) is not None:
                s.index = s.index.tz_localize(None)
            return s

        return self._safe(load, None)

    # -------------------------------------------------------------- noticias
    def _news(self, t) -> list[str]:
        n = self.settings.news_count
        items = self._safe(lambda: t.get_news(count=n), None)
        if items is None:
            items = self._safe(lambda: t.news, []) or []
        headlines: list[str] = []
        for item in items:
            # yfinance >= 0.2.50 anida el contenido en item["content"]
            content = item.get("content") if isinstance(item, dict) else None
            title = (content or {}).get("title") if content else item.get("title")
            if title:
                headlines.append(str(title).strip())
        return headlines[:n]

    # -------------------------------------------------------------- divisa
    def _fx_to_eur(self, currency: str) -> float:
        if currency == self.settings.base_currency:
            return 1.0
        import yfinance as yf

        pair = f"{currency}{self.settings.base_currency}=X"
        rate = self._safe(lambda: float(yf.Ticker(pair).history(period="5d")["Close"].iloc[-1]), None)
        if rate is None or rate <= 0:
            log.warning("No se pudo obtener %s; se usa 1.0 (los importes en € serán aproximados)", pair)
            return 1.0
        return rate

    # -------------------------------------------------------------- macro
    def _fed_rate(self) -> float:
        def load() -> float:
            r = requests.get(FRED_FED_FUNDS_CSV, timeout=self.http_timeout)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            return float(pd.to_numeric(df.iloc[:, -1], errors="coerce").dropna().iloc[-1])

        rate = self._safe(load, None)
        return self.settings.fallback_fed_rate if rate is None else rate

    def _ecb_rate(self) -> float:
        def load() -> float:
            r = requests.get(ECB_DEPOSIT_RATE_CSV, timeout=self.http_timeout)
            r.raise_for_status()
            df = pd.read_csv(io.StringIO(r.text))
            return float(df["OBS_VALUE"].iloc[-1])

        rate = self._safe(load, None)
        return self.settings.fallback_ecb_rate if rate is None else rate

    # -------------------------------------------------------------- util
    @staticmethod
    def _safe(fn, default):
        try:
            return fn()
        except Exception as exc:  # las fuentes externas fallan con frecuencia
            log.warning("Fallo al descargar datos: %s", exc)
            return default

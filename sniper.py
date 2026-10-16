"""Francotirador: detector de caídas intradía ("Buy the Dip") con velas de 1 minuto.

Cada `sniper_interval_minutes`, para los activos de la watchlist cuyo mercado está abierto:
  1. Descarga en lote las velas de 1 minuto de hoy (precio en vivo) y el diario de 3 meses
     (cierre de ayer y RSI) con `yfinance`.
  2. Dispara si la caída frente al cierre de ayer supera `dip_drop_pct` (5 %) o si el RSI(14),
     recalculado con el precio en vivo, cruza por debajo de `dip_rsi` (30).
  3. Ejecuta INMEDIATAMENTE los 5 agentes con el precio en vivo y avisa por Telegram. Si el
     Fundamental sigue alto (>= `dip_min_fundamental`), la alerta va encabezada con
     "🚨 OPORTUNIDAD DE CAÍDA DETECTADA" y el CIO, en modo Buy the Dip, ignora los vetos de
     caída libre / volatilidad extrema y emite BUY (con el tamaño del RiskManager).

Cada disparo se avisa una vez al día por activo; una caída que se agrava vuelve a avisar
en cada nuevo tramo de 5 % (−10 %, −15 %…).

Latencia real: la vela de 1 minuto es la resolución más fina de Yahoo, pero Yahoo publica el
mercado español con ~15 min de retraso; EE. UU. llega prácticamente en tiempo real.
"""
from __future__ import annotations

import html
import logging
import math
from dataclasses import dataclass, field
from datetime import date, datetime, time, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo

import pandas as pd

from agents.cio import fundamental_intact
from agents.technical import rsi
from config import SETTINGS, AlertRules
from notifier import format_signal, support_from_reports

log = logging.getLogger("sniper")

MADRID = ZoneInfo("Europe/Madrid")
EU_SUFFIXES = (".MC", ".DE", ".AS", ".PA", ".MI", ".L", ".SW", ".BR", ".LS", ".VI")
MARKET_HOURS = {
    "EU": (ZoneInfo("Europe/Madrid"), time(9, 0), time(17, 30)),
    "US": (ZoneInfo("America/New_York"), time(9, 30), time(16, 0)),
}


def market_of(ticker: str) -> str:
    if ticker.endswith(("-USD", "-EUR")):
        return "CRYPTO"
    return "EU" if ticker.upper().endswith(EU_SUFFIXES) else "US"


def market_is_open(ticker: str, now: datetime) -> bool:
    """Horario regular de lunes a viernes (sin festivos: un festivo no tiene vela de hoy)."""
    market = market_of(ticker)
    if market == "CRYPTO":
        return True
    tz, start, end = MARKET_HOURS[market]
    local = now.astimezone(tz)
    return local.weekday() < 5 and start <= local.time() <= end


@dataclass
class LiveQuote:
    ticker: str
    price: float
    bar_time: datetime
    prev_close: float
    rsi_now: float
    rsi_prev: float

    @property
    def change_pct(self) -> float:
        return (self.price / self.prev_close - 1.0) * 100.0


def build_quote(ticker: str, minute_close: pd.Series, daily_close: pd.Series) -> LiveQuote | None:
    """Combina la última vela de 1 min con el diario (sesiones anteriores a la de la vela)."""
    minute_close, daily_close = minute_close.dropna(), daily_close.dropna()
    if minute_close.empty or daily_close.empty:
        return None
    bar_time = pd.Timestamp(minute_close.index[-1])
    session = bar_time.date()
    history = daily_close[[pd.Timestamp(i).date() < session for i in daily_close.index]]
    if len(history) < 15:
        return None
    price = float(minute_close.iloc[-1])
    rsi_prev = float(rsi(history.reset_index(drop=True)).iloc[-1])
    live = pd.concat([history, pd.Series([price])], ignore_index=True)
    rsi_now = float(rsi(live).iloc[-1])
    return LiveQuote(ticker, price, bar_time.to_pydatetime(), float(history.iloc[-1]), rsi_now, rsi_prev)


def _close_column(df: pd.DataFrame, ticker: str, single: bool) -> pd.Series:
    if df is None or df.empty:
        return pd.Series(dtype=float)
    if isinstance(df.columns, pd.MultiIndex):
        if ticker not in df.columns.get_level_values(0):
            return pd.Series(dtype=float)
        return df[ticker]["Close"]
    return df["Close"] if single else pd.Series(dtype=float)


def fetch_live_quotes(tickers: list[str]) -> dict[str, LiveQuote]:
    """Dos descargas en lote: velas de 1 minuto de hoy y diario de 3 meses."""
    import yfinance as yf

    if not tickers:
        return {}
    common = {"group_by": "ticker", "auto_adjust": True, "progress": False, "threads": True}
    minute = yf.download(tickers, period="1d", interval="1m", prepost=False, **common)
    daily = yf.download(tickers, period="3mo", interval="1d", **common)
    single = len(tickers) == 1
    quotes = {}
    for t in tickers:
        q = build_quote(t, _close_column(minute, t, single), _close_column(daily, t, single))
        if q is not None:
            quotes[t] = q
    return quotes


@dataclass
class SniperEvent:
    quote: LiveQuote
    reasons: list[str]
    message: str
    opportunity: bool
    result: Any = None


@dataclass
class Sniper:
    analyze_fn: Callable[..., Any]                 # analyze(ticker, live_price=..., dip_mode=True)
    notify: Callable[[str], Any]
    fetch: Callable[[list[str]], dict[str, LiveQuote]] = fetch_live_quotes
    rules: AlertRules = field(default_factory=lambda: SETTINGS.alerts)
    on_opportunity: Callable[[str], Any] | None = None    # p. ej. rearmar el aviso de veto del activo
    _fired: set[tuple[date, str, str]] = field(default_factory=set)

    # ------------------------------------------------------------ reglas
    def triggers(self, q: LiveQuote, today: date) -> list[tuple[str, str]]:
        """[(clave de deduplicación, motivo legible)] de las reglas que se cumplen."""
        out = []
        drop = -q.change_pct
        if drop >= self.rules.dip_drop_pct:
            level = int(drop // self.rules.dip_drop_pct)      # 1 = −5 %, 2 = −10 %…
            out.append((f"drop{level}", f"Caída de {drop:.1f}% frente al cierre de ayer"))
        if math.isfinite(q.rsi_now) and q.rsi_now < self.rules.dip_rsi <= q.rsi_prev:
            out.append(("rsi", f"RSI(14) cruza a la baja {self.rules.dip_rsi:.0f}: {q.rsi_prev:.0f} → {q.rsi_now:.0f}"))
        return [(k, r) for k, r in out if (today, q.ticker, k) not in self._fired]

    # ------------------------------------------------------------ escaneo
    def scan(self, tickers: list[str], now: datetime | None = None) -> list[SniperEvent]:
        now = now or datetime.now(timezone.utc)
        live = [t for t in tickers if market_is_open(t, now)]
        if not live:
            return []
        try:
            quotes = self.fetch(live)
        except Exception as exc:
            log.warning("Francotirador: fallo descargando velas de 1 min: %s", exc)
            return []
        today = now.astimezone(MADRID).date()
        events = []
        for ticker, q in quotes.items():
            if q.bar_time.date() != now.astimezone(q.bar_time.tzinfo or MADRID).date():
                continue    # sin sesión hoy (festivo) o datos de ayer
            hits = self.triggers(q, today)
            if not hits:
                continue
            for key, _ in hits:
                self._fired.add((today, ticker, key))
            reasons = [r for _, r in hits]
            log.warning("Francotirador: %s %+.2f%% · %s", ticker, q.change_pct, "; ".join(reasons))
            events.append(self._fire(q, reasons))
        return events

    def _fire(self, q: LiveQuote, reasons: list[str]) -> SniperEvent:
        e = html.escape
        try:
            result = self.analyze_fn(q.ticker, live_price=q.price, dip_mode=True)
        except Exception as exc:
            msg = (
                f"📉 <b>Caída en {e(q.ticker)}: {q.change_pct:+.1f}%</b>\n" + "\n".join(f"• {e(r)}" for r in reasons)
                + f"\n❌ El análisis completo falló: {e(str(exc))}"
            )
            self.notify(msg)
            return SniperEvent(q, reasons, msg, False)

        fund = result.reports["fundamental"]
        intact = fundamental_intact(result.reports, self.rules.dip_min_fundamental)
        if intact:
            what = f"ha caído un {-q.change_pct:.1f}%" if q.change_pct < 0 else f"entra en sobreventa (RSI {q.rsi_now:.0f})"
            header = f"🚨 <b>OPORTUNIDAD DE CAÍDA DETECTADA: {e(q.ticker)} {what}. El fundamental sigue intacto.</b>"
        else:
            why = "sin datos fundamentales" if fund.error else f"Fundamental {fund.score:.0f}/100"
            header = (
                f"📉 <b>CAÍDA FUERTE: {e(q.ticker)} {q.change_pct:+.1f}%.</b> "
                f"No se considera oportunidad ({e(why)})."
            )
        context = (
            f"⏱️ Precio 1m: <b>{q.price:,.2f}</b> ({q.bar_time.astimezone(MADRID):%H:%M} Madrid) · "
            f"cierre ayer {q.prev_close:,.2f} · RSI {q.rsi_now:.0f}\n"
            f"🏛️ Fundamental {fund.score:.0f}/100\n" + "\n".join(f"• {e(r)}" for r in reasons)
        )
        d = result.decision
        msg = format_signal(
            d.to_output_json(), d.vetoes, d.guardrail_notes,
            support=support_from_reports(result.reports), header=f"{header}\n{context}\n",
        )
        self.notify(msg)
        if intact and self.on_opportunity:
            self.on_opportunity(q.ticker)
        return SniperEvent(q, reasons, msg, intact, result)

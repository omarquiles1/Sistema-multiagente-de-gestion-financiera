"""Auditoría de rendimiento de las señales emitidas.

Para cada horizonte (7 y 30 días) toma las señales emitidas en la ventana
[ahora - horizonte - ventana, ahora - horizonte) —con la ejecución semanal cada señal se
audita una sola vez por horizonte— y compara el cierre del día de la señal (guardado en
`market_data`) con el precio actual de yfinance.

Criterio de acierto:
  * BUY            -> acierto si el precio ha subido.
  * SELL / REDUCE  -> acierto si el precio ha bajado.
  * HOLD           -> no es direccional: se informa del movimiento pero no cuenta en el % de acierto.
`edge_pct` es el retorno visto desde la señal (positivo = la señal tenía razón).

Uso:
  python performance_tracker.py            # imprime el resumen
  python performance_tracker.py --send     # y lo envía por Telegram
"""
from __future__ import annotations

import argparse
import html
import logging
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from config import SETTINGS
from database import Database

log = logging.getLogger("performance")

DIRECTION = {"BUY": 1, "SELL": -1, "REDUCE": -1, "HOLD": 0}


@dataclass
class SignalOutcome:
    signal_id: int
    ticker: str
    action: str
    final_score: int
    signal_date: str
    entry_price: float
    current_price: float
    stop_loss: float | None

    @property
    def return_pct(self) -> float:
        return (self.current_price / self.entry_price - 1.0) * 100.0

    @property
    def direction(self) -> int:
        return DIRECTION.get(self.action, 0)

    @property
    def edge_pct(self) -> float:
        return self.direction * self.return_pct

    @property
    def hit(self) -> bool | None:
        return None if self.direction == 0 else self.edge_pct > 0

    @property
    def stop_breached(self) -> bool:
        return self.action == "BUY" and self.stop_loss is not None and self.current_price <= self.stop_loss


@dataclass
class HorizonReport:
    horizon_days: int
    outcomes: list[SignalOutcome] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def directional(self) -> list[SignalOutcome]:
        return [o for o in self.outcomes if o.direction != 0]

    @property
    def hit_rate(self) -> float | None:
        d = self.directional
        return None if not d else 100.0 * sum(bool(o.hit) for o in d) / len(d)

    @property
    def mean_edge_pct(self) -> float | None:
        d = self.directional
        return None if not d else sum(o.edge_pct for o in d) / len(d)


def yfinance_last_price(ticker: str) -> float | None:
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
    if hist is None or hist.empty:
        return None
    return float(hist["Close"].dropna().iloc[-1])


def _normalize_pence(ticker: str, price: float) -> float:
    # El DataFetcher guarda las cotizaciones de Londres en GBP, yfinance las da en peniques.
    return price / 100.0 if ticker.upper().endswith(".L") else price


class PerformanceTracker:
    def __init__(
        self,
        db: Database,
        price_fn: Callable[[str], float | None] = yfinance_last_price,
        horizons: tuple[int, ...] = SETTINGS.alerts.performance_horizons_days,
        window_days: int = 7,
    ) -> None:
        self.db = db
        self.price_fn = price_fn
        self.horizons = horizons
        self.window_days = window_days

    def _current_price(self, ticker: str, cache: dict[str, float | None]) -> float | None:
        if ticker not in cache:
            try:
                p = self.price_fn(ticker)
                cache[ticker] = None if p is None else _normalize_pence(ticker, p)
            except Exception as exc:
                log.warning("Sin precio actual de %s: %s", ticker, exc)
                cache[ticker] = None
        return cache[ticker]

    def evaluate(self, now: datetime | None = None) -> list[HorizonReport]:
        now = now or datetime.now(timezone.utc)
        prices: dict[str, float | None] = {}
        reports = []
        for h in self.horizons:
            end = now - timedelta(days=h)
            rep = HorizonReport(h)
            for sig in self.db.signals_between(end - timedelta(days=self.window_days), end):
                ticker = sig["ticker"]
                date = datetime.fromisoformat(sig["timestamp"]).strftime("%Y-%m-%d")
                entry = self.db.close_on_or_before(ticker, date)
                current = self._current_price(ticker, prices)
                if not entry or current is None:
                    rep.skipped.append(f"{ticker} ({date}): sin precio de referencia o actual")
                    continue
                rep.outcomes.append(SignalOutcome(
                    signal_id=sig["id"], ticker=ticker, action=sig["recommended_action"],
                    final_score=int(sig["final_score"] or 0), signal_date=date,
                    entry_price=entry, current_price=current, stop_loss=sig["stop_loss_calculated"],
                ))
            reports.append(rep)
        return reports


def format_report(reports: list[HorizonReport], max_lines: int = 12) -> str:
    e = html.escape
    lines = ["📊 <b>Auditoría semanal de señales</b>"]
    for rep in reports:
        lines.append(f"\n<b>Señales de hace {rep.horizon_days} días</b>")
        if not rep.outcomes:
            lines.append("Sin señales que auditar." + (f" ({len(rep.skipped)} sin precio)" if rep.skipped else ""))
            continue
        d = rep.directional
        if d:
            lines.append(
                f"Acierto: <b>{rep.hit_rate:.0f}%</b> ({sum(bool(o.hit) for o in d)}/{len(d)}) · "
                f"Desviación media a favor: <b>{rep.mean_edge_pct:+.2f}%</b>"
            )
        for o in sorted(d, key=lambda o: o.edge_pct, reverse=True)[:max_lines]:
            mark = "✅" if o.hit else "❌"
            stop = " · 🛑 stop perforado" if o.stop_breached else ""
            lines.append(
                f"{mark} {e(o.action)} {e(o.ticker)} ({o.signal_date}, {o.final_score:+d}): "
                f"{o.entry_price:.2f} → {o.current_price:.2f} ({o.return_pct:+.2f}%){stop}"
            )
        if len(d) > max_lines:
            lines.append(f"… y {len(d) - max_lines} más")
        holds = [o for o in rep.outcomes if o.direction == 0]
        if holds:
            avg = sum(abs(o.return_pct) for o in holds) / len(holds)
            lines.append(f"⏸️ {len(holds)} HOLD (movimiento medio absoluto {avg:.2f}%)")
        if rep.skipped:
            lines.append(f"({len(rep.skipped)} señales sin precio de referencia)")
    return "\n".join(lines)


def run_weekly_report(db: Database, send: bool = True, tracker: PerformanceTracker | None = None) -> str:
    from notifier import send_telegram_message

    text = format_report((tracker or PerformanceTracker(db)).evaluate())
    if send:
        send_telegram_message(text)
    return text


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Auditoría de acierto de las señales emitidas")
    parser.add_argument("--db", default=SETTINGS.db_path)
    parser.add_argument("--send", action="store_true", help="Envía el resumen por Telegram")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):  # emojis en la consola de Windows
        sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print(run_weekly_report(Database(args.db), send=args.send))
    return 0


if __name__ == "__main__":
    sys.exit(main())

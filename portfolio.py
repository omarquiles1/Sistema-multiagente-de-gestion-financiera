"""Gestor de la cartera real: valoración, compras, ventas y capital total.

* `precio_medio_compra` y los precios de las órdenes van en la divisa del activo.
* El capital líquido (cash) está en EUR.
* El P&L en euros convierte coste y valor al tipo de cambio ACTUAL: refleja el movimiento del
  activo, no el efecto divisa desde la compra (para eso habría que guardar el coste en EUR).
"""
from __future__ import annotations

import html
import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

from database import Database

log = logging.getLogger(__name__)

EPS = 1e-9


class PortfolioError(ValueError):
    """Operación no válida; el mensaje se muestra tal cual al usuario."""


# ------------------------------------------------------------ fuentes de datos
def yf_last_price(ticker: str) -> float | None:
    import yfinance as yf

    hist = yf.Ticker(ticker).history(period="5d", auto_adjust=True)
    if hist is None or hist.empty:
        return None
    return float(hist["Close"].dropna().iloc[-1])


def yf_fx_to_eur(currency: str) -> float | None:
    if currency == "EUR":
        return 1.0
    return yf_last_price(f"{currency}EUR=X")


def yf_metadata(ticker: str) -> tuple[str, str]:
    """(divisa, nombre) del activo."""
    import yfinance as yf

    info = yf.Ticker(ticker).info or {}
    return info.get("currency") or "EUR", info.get("longName") or info.get("shortName") or ticker


@dataclass
class PositionValue:
    ticker: str
    nombre: str
    cantidad: float
    precio_medio: float
    divisa: str
    precio_actual: float
    fx_to_eur: float
    precio_disponible: bool          # False -> se valora al precio medio (sin P&L)

    @property
    def coste_eur(self) -> float:
        return self.cantidad * self.precio_medio * self.fx_to_eur

    @property
    def valor_eur(self) -> float:
        return self.cantidad * self.precio_actual * self.fx_to_eur

    @property
    def pnl_eur(self) -> float:
        return self.valor_eur - self.coste_eur

    @property
    def pnl_pct(self) -> float:
        return (self.precio_actual / self.precio_medio - 1.0) * 100.0


@dataclass
class PortfolioSnapshot:
    positions: list[PositionValue]
    cash_eur: float

    @property
    def invested_value_eur(self) -> float:
        return sum(p.valor_eur for p in self.positions)

    @property
    def cost_eur(self) -> float:
        return sum(p.coste_eur for p in self.positions)

    @property
    def pnl_eur(self) -> float:
        return self.invested_value_eur - self.cost_eur

    @property
    def total_capital_eur(self) -> float:
        return self.cash_eur + self.invested_value_eur


@dataclass
class TradeResult:
    side: str
    ticker: str
    cantidad: float
    precio: float
    divisa: str
    importe_eur: float
    cash_eur: float
    position: dict | None
    pnl_realizado_eur: float | None = None


class PortfolioManager:
    def __init__(
        self,
        db: Database,
        price_fn: Callable[[str], float | None] = yf_last_price,
        fx_fn: Callable[[str], float | None] = yf_fx_to_eur,
        meta_fn: Callable[[str], tuple[str, str]] = yf_metadata,
        cache_ttl_s: float = 600.0,
    ) -> None:
        self.db = db
        self.price_fn, self.fx_fn, self.meta_fn = price_fn, fx_fn, meta_fn
        self.cache_ttl_s = cache_ttl_s
        self._cache: dict[str, tuple[float, float | None]] = {}
        self._lock = threading.Lock()     # compras/ventas desde Telegram y análisis en paralelo

    # ------------------------------------------------------------ precios
    def _cached(self, key: str, fn: Callable[[], float | None]) -> float | None:
        hit = self._cache.get(key)
        if hit and time.monotonic() - hit[0] < self.cache_ttl_s:
            return hit[1]
        try:
            value = fn()
        except Exception as exc:
            log.warning("Sin dato para %s: %s", key, exc)
            value = None
        if value is not None and value <= 0:
            value = None
        self._cache[key] = (time.monotonic(), value)
        return value

    def price(self, ticker: str) -> float | None:
        return self._cached(f"px:{ticker}", lambda: self.price_fn(ticker))

    def fx(self, currency: str) -> float | None:
        if currency == "EUR":
            return 1.0
        return self._cached(f"fx:{currency}", lambda: self.fx_fn(currency))

    # ------------------------------------------------------------ valoración
    def snapshot(self) -> PortfolioSnapshot:
        cash = self.db.get_cash()
        if cash is None:
            raise PortfolioError("La cartera no está inicializada: ejecuta `python init_portfolio.py`.")
        values = []
        for p in self.db.get_positions():
            px = self.price(p["ticker"])
            fx = self.fx(p["divisa"])
            values.append(PositionValue(
                ticker=p["ticker"], nombre=p["nombre_activo"] or p["ticker"], cantidad=p["cantidad"],
                precio_medio=p["precio_medio_compra"], divisa=p["divisa"],
                precio_actual=px if px is not None else p["precio_medio_compra"],
                fx_to_eur=fx if fx is not None else 1.0,
                precio_disponible=px is not None and fx is not None,
            ))
        return PortfolioSnapshot(values, cash)

    def capital(self) -> tuple[float, float] | None:
        """(capital total, liquidez) en EUR para el RiskManager; None si no hay cartera cargada."""
        if self.db.get_cash() is None:
            return None
        snap = self.snapshot()
        return snap.total_capital_eur, snap.cash_eur

    def position_value_eur(self, ticker: str) -> float:
        """Valor actual en EUR de lo que ya se tiene de `ticker` (0 si no está en cartera)."""
        pos = self.db.get_position(ticker.upper())
        if pos is None:
            return 0.0
        px = self.price(pos["ticker"]) or pos["precio_medio_compra"]
        return pos["cantidad"] * px * (self.fx(pos["divisa"]) or 1.0)

    # ------------------------------------------------------------ operaciones
    def _resolve(self, ticker: str, precio: float | None, existing: dict | None) -> tuple[float, str, str, float]:
        if precio is None:
            precio = self.price(ticker)
            if precio is None:
                raise PortfolioError(f"No hay cotización de {ticker}: indica el precio en la orden.")
        if precio <= 0:
            raise PortfolioError("El precio debe ser positivo.")
        if existing:
            divisa, nombre = existing["divisa"], existing["nombre_activo"] or ticker
        else:
            try:
                divisa, nombre = self.meta_fn(ticker)
            except Exception as exc:
                raise PortfolioError(f"No encuentro {ticker} en Yahoo Finance ({exc}).") from exc
        fx = self.fx(divisa)
        if fx is None:
            raise PortfolioError(f"No hay tipo de cambio {divisa}/EUR ahora mismo; prueba más tarde.")
        return precio, divisa, nombre, fx

    def buy(self, ticker: str, cantidad: float, precio: float | None = None) -> TradeResult:
        ticker = ticker.upper().strip()
        if cantidad <= 0:
            raise PortfolioError("La cantidad debe ser positiva.")
        with self._lock:
            cash = self.db.get_cash()
            if cash is None:
                raise PortfolioError("La cartera no está inicializada: ejecuta `python init_portfolio.py`.")
            existing = self.db.get_position(ticker)
            precio, divisa, nombre, fx = self._resolve(ticker, precio, existing)
            importe = cantidad * precio * fx
            if importe > cash + EPS:
                raise PortfolioError(f"Liquidez insuficiente: la compra cuesta {importe:.2f} € y tienes {cash:.2f} €.")
            q0 = existing["cantidad"] if existing else 0.0
            a0 = existing["precio_medio_compra"] if existing else 0.0
            new_qty = q0 + cantidad
            position = {
                "nombre_activo": nombre, "cantidad": new_qty,
                "precio_medio_compra": (q0 * a0 + cantidad * precio) / new_qty, "divisa": divisa,
            }
            self.db.apply_trade(ticker, "BUY", cantidad, precio, divisa, fx, position, cash - importe)
        return TradeResult("BUY", ticker, cantidad, precio, divisa, importe, cash - importe, position)

    def sell(self, ticker: str, cantidad: float, precio: float | None = None) -> TradeResult:
        ticker = ticker.upper().strip()
        if cantidad <= 0:
            raise PortfolioError("La cantidad debe ser positiva.")
        with self._lock:
            cash = self.db.get_cash()
            if cash is None:
                raise PortfolioError("La cartera no está inicializada: ejecuta `python init_portfolio.py`.")
            existing = self.db.get_position(ticker)
            if existing is None:
                raise PortfolioError(f"No tienes {ticker} en cartera.")
            held = existing["cantidad"]
            if cantidad > held + EPS:
                raise PortfolioError(f"Solo tienes {held:g} de {ticker}; no puedes vender {cantidad:g}.")
            precio, divisa, _, fx = self._resolve(ticker, precio, existing)
            importe = cantidad * precio * fx
            pnl = cantidad * (precio - existing["precio_medio_compra"]) * fx
            remaining = held - cantidad
            position = None if remaining <= EPS else {**existing, "cantidad": remaining}
            self.db.apply_trade(ticker, "SELL", cantidad, precio, divisa, fx, position, cash + importe, pnl)
        return TradeResult("SELL", ticker, cantidad, precio, divisa, importe, cash + importe, position, pnl)


# --------------------------------------------------------------------- formato
def _eur(x: float) -> str:
    return f"{x:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def _signed_eur(x: float) -> str:
    x = round(x, 2) + 0.0   # evita "−0,00 €"
    return ("+" if x >= 0 else "−") + _eur(abs(x))


def format_portfolio(snap: PortfolioSnapshot, max_position_pct: float = 0.10, max_risk_pct: float = 0.02) -> str:
    e = html.escape
    lines = ["💼 <b>Cartera</b>"]
    for p in sorted(snap.positions, key=lambda p: p.valor_eur, reverse=True):
        icon = "📈" if round(p.pnl_eur, 2) >= 0 else "📉"
        if not p.precio_disponible:
            icon = "❔"
        weight = 100 * p.valor_eur / snap.total_capital_eur if snap.total_capital_eur else 0
        lines.append(
            f"{icon} <b>{e(p.ticker)}</b> · {e(p.nombre[:32])}\n"
            f"    {p.cantidad:g} × {p.precio_actual:,.2f} {e(p.divisa)} = {_eur(p.valor_eur)} ({weight:.1f}%)\n"
            f"    P&amp;L {_signed_eur(p.pnl_eur)} ({p.pnl_pct:+.2f}%) · medio {p.precio_medio:,.2f}"
        )
    pnl_pct = 100 * snap.pnl_eur / snap.cost_eur if snap.cost_eur else 0.0
    total = snap.total_capital_eur
    lines += [
        "",
        f"Invertido (coste): {_eur(snap.cost_eur)}",
        f"Valor posiciones: {_eur(snap.invested_value_eur)}",
        f"<b>P&amp;L total: {_signed_eur(snap.pnl_eur)} ({pnl_pct:+.2f}%)</b>",
        f"💶 Liquidez: {_eur(snap.cash_eur)}",
        f"🏦 <b>Capital total: {_eur(total)}</b>",
        f"Límites RiskManager: posición máx. {_eur(total * max_position_pct)} · "
        f"riesgo máx. {_eur(total * max_risk_pct)}",
    ]
    if any(not p.precio_disponible for p in snap.positions):
        lines.append("❔ Sin cotización: valorado al precio medio.")
    if any(p.divisa != "EUR" for p in snap.positions):
        lines.append("<i>P&amp;L de activos en divisa sin efecto del tipo de cambio.</i>")
    return "\n".join(lines)


def format_trade(t: TradeResult) -> str:
    e = html.escape
    if t.side == "BUY":
        head = f"🟢 <b>Compra registrada · {e(t.ticker)}</b>"
        pos = t.position
        detail = f"Posición: {pos['cantidad']:g} a precio medio {pos['precio_medio_compra']:,.2f} {e(t.divisa)}"
    else:
        head = f"🔴 <b>Venta registrada · {e(t.ticker)}</b>"
        detail = (
            f"Posición restante: {t.position['cantidad']:g}" if t.position else "Posición cerrada"
        ) + f"\nP&amp;L realizado: {_signed_eur(t.pnl_realizado_eur or 0.0)}"
    return (
        f"{head}\n{t.cantidad:g} × {t.precio:,.2f} {e(t.divisa)} = {_eur(t.importe_eur)}\n"
        f"{detail}\n💶 Liquidez: {_eur(t.cash_eur)}"
    )

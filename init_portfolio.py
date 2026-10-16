"""Carga la cartera real en SQLite. BORRA la tabla `portfolio` y fija la liquidez.

Uso:
  python init_portfolio.py                 # cartera de abajo + 3.900 € de liquidez
  python init_portfolio.py --cash 4100     # otra liquidez
  python init_portfolio.py --db otra.db

Las operaciones posteriores se registran desde Telegram (/comprar, /vender); volver a
ejecutar este script las sobrescribe.
"""
from __future__ import annotations

import argparse
import sys

from config import SETTINGS
from database import Database
from portfolio import PortfolioManager, format_portfolio, yf_fx_to_eur, yf_last_price

INITIAL_CASH_EUR = 3900.0

# precio_medio_compra en la divisa del activo.
POSITIONS = [
    {"ticker": "SPPW.DE", "nombre_activo": "SPDR MSCI World", "cantidad": 23.88, "precio_medio_compra": 39.74, "divisa": "EUR"},
    {"ticker": "L0CK.DE", "nombre_activo": "iShares Digital Security (Acc)", "cantidad": 26.08, "precio_medio_compra": 8.43, "divisa": "EUR"},
    {"ticker": "IS3N.DE", "nombre_activo": "iShares Core MSCI EM IMI", "cantidad": 5.22, "precio_medio_compra": 36.00, "divisa": "EUR"},
    {"ticker": "BTC-EUR", "nombre_activo": "Bitcoin", "cantidad": 0.00524, "precio_medio_compra": 68000.0, "divisa": "EUR"},
    {"ticker": "O", "nombre_activo": "Realty Income", "cantidad": 0.16, "precio_medio_compra": 62.64, "divisa": "USD"},
]

# PayPal: se conoce el importe invertido, no la cantidad. Se calcula al precio de hoy.
EUR_AMOUNT_POSITIONS = [
    {"ticker": "PYPL", "nombre_activo": "PayPal", "importe_eur": 10.0, "divisa": "USD"},
]


def position_from_eur_amount(item: dict, price_fn=yf_last_price, fx_fn=yf_fx_to_eur) -> dict:
    price, fx = price_fn(item["ticker"]), fx_fn(item["divisa"])
    if not price or not fx:
        raise RuntimeError(f"Sin cotización de {item['ticker']} o {item['divisa']}/EUR: no se puede calcular la cantidad")
    return {
        "ticker": item["ticker"], "nombre_activo": item["nombre_activo"],
        "cantidad": round(item["importe_eur"] / (price * fx), 6),
        "precio_medio_compra": round(price, 4), "divisa": item["divisa"],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inicializa la cartera real (borra la anterior)")
    parser.add_argument("--db", default=SETTINGS.db_path)
    parser.add_argument("--cash", type=float, default=INITIAL_CASH_EUR, help="Liquidez en EUR")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    db = Database(args.db)
    old = db.get_positions()
    positions = POSITIONS + [position_from_eur_amount(i) for i in EUR_AMOUNT_POSITIONS]
    db.reset_portfolio(positions, args.cash)
    print(f"Cartera cargada en {args.db}: {len(positions)} posiciones (antes {len(old)}), liquidez {args.cash:.2f} €\n")
    print(format_portfolio(PortfolioManager(db).snapshot()).replace("&amp;", "&"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

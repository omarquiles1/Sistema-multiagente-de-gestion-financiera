"""RiskManager: matemática de riesgo HARDCODED (nunca delegada al LLM).

Reglas:
  * Capital base 7.000 €.
  * Tamaño máximo por posición = 10 % del capital (700 €).
  * Tamaño mínimo por operación = 300 € (dilución de comisiones).
  * Riesgo máximo por operación (entrada -> stop) = 2 % del capital (140 €).
  * Stop-loss dinámico = precio - 2 * ATR(14), con tope duro del -15 %;
    se toma el que esté MÁS CERCA del precio (el más alto).
  * Take-profit inicial con ratio riesgo/recompensa 1:2.

Los precios se expresan en la divisa de cotización del activo; los importes
en euros se obtienen con el tipo de cambio `fx_to_eur`.
"""
from __future__ import annotations

import math

from config import RiskRules
from models import RiskPlan


class RiskManager:
    def __init__(self, rules: RiskRules | None = None) -> None:
        self.rules = rules or RiskRules()

    # ------------------------------------------------------------------ stops
    def stop_loss(self, price: float, atr: float | None) -> tuple[float, float, float]:
        """Devuelve (stop_final, stop_atr, stop_duro)."""
        hard_stop = price * (1.0 - self.rules.hard_stop_pct)
        if atr is None or not math.isfinite(atr) or atr <= 0:
            return hard_stop, float("nan"), hard_stop
        atr_stop = price - self.rules.atr_multiplier * atr
        # "Lo que esté más cerca del precio" -> el stop más alto.
        return max(atr_stop, hard_stop), atr_stop, hard_stop

    def take_profit(self, price: float, stop: float) -> float:
        return price + self.rules.reward_risk_ratio * (price - stop)

    # ------------------------------------------------------------ position size
    def build_plan(
        self,
        price: float,
        atr: float | None,
        fx_to_eur: float = 1.0,
        cash_available_eur: float | None = None,
    ) -> RiskPlan:
        if price <= 0 or not math.isfinite(price):
            raise ValueError(f"Precio inválido: {price}")
        if fx_to_eur <= 0 or not math.isfinite(fx_to_eur):
            raise ValueError(f"Tipo de cambio inválido: {fx_to_eur}")

        stop, atr_stop, hard_stop = self.stop_loss(price, atr)
        risk_per_share = price - stop
        take_profit = self.take_profit(price, stop)
        stop_distance_pct = risk_per_share / price

        # Regla 1: tamaño máximo por diversificación (700 €).
        cap_eur = self.rules.max_position_eur
        # Regla 2: riesgo de ruina -> pérdida en el stop <= 140 €.
        cap_by_risk_eur = self.rules.max_risk_eur / stop_distance_pct
        position_eur = min(cap_eur, cap_by_risk_eur)
        if cash_available_eur is not None:
            position_eur = min(position_eur, max(cash_available_eur, 0.0))

        price_eur = price * fx_to_eur
        shares = position_eur / price_eur
        if not self.rules.allow_fractional_shares:
            shares = math.floor(shares)
        position_eur = shares * price_eur

        # Regla 3: tamaño mínimo por costes (300 €).
        if position_eur < self.rules.min_trade_eur:
            reason = (
                f"Posición resultante {position_eur:.2f} € < mínimo {self.rules.min_trade_eur:.0f} € "
                "(operación no rentable por comisiones)"
            )
            return RiskPlan(
                entry_price=price, entry_price_eur=price_eur, atr=atr if atr else float("nan"),
                atr_stop=atr_stop, hard_stop=hard_stop, stop_loss_price=stop,
                take_profit_target=take_profit, risk_per_share=risk_per_share,
                shares=0.0, position_size_eur=0.0, risk_eur=0.0, tradeable=False, reason=reason,
            )

        risk_eur = shares * risk_per_share * fx_to_eur
        # Defensa en profundidad: nunca superar el riesgo máximo por redondeos.
        assert risk_eur <= self.rules.max_risk_eur + 1e-6, "Violación de la regla del 2 %"
        assert position_eur <= self.rules.max_position_eur + 1e-6, "Violación de la regla del 10 %"

        return RiskPlan(
            entry_price=price, entry_price_eur=price_eur, atr=atr if atr else float("nan"),
            atr_stop=atr_stop, hard_stop=hard_stop, stop_loss_price=stop,
            take_profit_target=take_profit, risk_per_share=risk_per_share,
            shares=shares, position_size_eur=position_eur, risk_eur=risk_eur,
            tradeable=True, reason="OK",
        )

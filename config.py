"""Configuración central del sistema.

Todas las reglas de negocio de riesgo viven aquí como constantes inmutables:
NO se delegan al LLM. El LLM solo puede leerlas, nunca modificarlas.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RiskRules:
    """Reglas duras de gestión de riesgo (cartera de 7.000 €)."""

    base_capital_eur: float = 7_000.0
    max_position_pct: float = 0.10          # 10 % -> 700 €
    min_trade_eur: float = 300.0            # diluir comisiones
    max_risk_pct: float = 0.02              # 2 % -> 140 € por operación
    atr_period: int = 14
    atr_multiplier: float = 2.0             # SL = precio - 2 * ATR(14)
    hard_stop_pct: float = 0.15             # tope duro -15 %
    reward_risk_ratio: float = 2.0          # TP con ratio 1:2
    allow_fractional_shares: bool = True    # brókers con fraccionadas (TR, Revolut, IBKR...)

    @property
    def max_position_eur(self) -> float:
        return self.base_capital_eur * self.max_position_pct

    @property
    def max_risk_eur(self) -> float:
        return self.base_capital_eur * self.max_risk_pct


@dataclass(frozen=True)
class ConsensusWeights:
    """Pesos del consenso del CIO (deben sumar 1)."""

    fundamental: float = 0.35
    technical: float = 0.30
    sentiment: float = 0.20
    risk: float = 0.15

    def __post_init__(self) -> None:
        total = self.fundamental + self.technical + self.sentiment + self.risk
        if abs(total - 1.0) > 1e-9:
            raise ValueError(f"Los pesos deben sumar 1.0 (suman {total})")


@dataclass(frozen=True)
class Thresholds:
    """Umbrales de decisión sobre el final_score (-100..100)."""

    buy: int = 30            # >= 30 -> BUY
    reduce: int = -10        # < -10 -> REDUCE
    sell: int = -40          # < -40 -> SELL
    max_llm_adjustment: int = 15   # el CIO solo puede mover ±15 pts el score ponderado
    extreme_volatility_annual: float = 0.60   # 60 % vol. anualizada = "Volatilidad extrema"
    extreme_atr_pct: float = 0.06             # ATR > 6 % del precio = "Volatilidad extrema"
    signal_cooldown_days: int = 7             # anti-overtrading
    wash_sale_days: int = 61                  # regla de los 2 meses (art. 33.5 LIRPF)


# P/E de referencia por sector (aprox. medianas del S&P 500, revisables).
# yfinance no ofrece el P/E sectorial; se mantiene editable aquí.
SECTOR_PE: dict[str, float] = {
    "Technology": 30.0,
    "Communication Services": 20.0,
    "Consumer Cyclical": 22.0,
    "Consumer Defensive": 21.0,
    "Healthcare": 22.0,
    "Financial Services": 15.0,
    "Industrials": 21.0,
    "Energy": 12.0,
    "Utilities": 17.0,
    "Real Estate": 30.0,
    "Basic Materials": 17.0,
}
DEFAULT_SECTOR_PE = 20.0


@dataclass(frozen=True)
class Settings:
    risk: RiskRules = field(default_factory=RiskRules)
    weights: ConsensusWeights = field(default_factory=ConsensusWeights)
    thresholds: Thresholds = field(default_factory=Thresholds)
    llm_model: str = os.getenv("LLM_MODEL", "claude-opus-5-5")
    db_path: str = os.getenv("DB_PATH", "portfolio.db")
    history_period: str = "2y"
    news_count: int = 15
    fallback_fed_rate: float = float(os.getenv("FALLBACK_FED_RATE", "4.25"))
    fallback_ecb_rate: float = float(os.getenv("FALLBACK_ECB_RATE", "2.00"))
    base_currency: str = "EUR"


SETTINGS = Settings()

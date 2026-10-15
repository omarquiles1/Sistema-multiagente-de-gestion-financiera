"""Estructuras de datos compartidas entre el Orquestador y los agentes."""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any

import pandas as pd


class Action(str, Enum):
    BUY = "BUY"
    HOLD = "HOLD"
    REDUCE = "REDUCE"
    SELL = "SELL"


class Confidence(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass
class MarketSnapshot:
    """Datos crudos descargados por el Orquestador para un ticker."""

    ticker: str
    ohlcv: pd.DataFrame                      # columnas: open, high, low, close, volume
    info: dict[str, Any]                     # fundamentales (yfinance .info)
    income_stmt: pd.DataFrame | None         # cuenta de resultados anual
    cashflow: pd.DataFrame | None            # flujo de caja anual
    news_headlines: list[str]
    currency: str                            # divisa de cotización (USD, EUR...)
    fx_to_eur: float                         # 1 unidad de `currency` = fx_to_eur €
    fed_rate: float | None
    ecb_rate: float | None
    benchmark_close: pd.Series | None = None  # para la beta (S&P 500)

    @property
    def last_price(self) -> float:
        return float(self.ohlcv["close"].iloc[-1])


@dataclass
class AgentReport:
    """Salida normalizada de cualquier agente especialista."""

    agent: str
    score: float                       # escala propia del agente (ver `scale`)
    scale: tuple[float, float]         # (min, max) de la escala del score
    metrics: dict[str, Any] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)   # alertas p.ej. "Volatilidad extrema"
    summary: str = ""
    error: str | None = None

    def normalized(self) -> float:
        """Convierte el score a la escala común -100..100."""
        lo, hi = self.scale
        if hi == lo:
            return 0.0
        return float((self.score - lo) / (hi - lo) * 200.0 - 100.0)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["normalized_score"] = round(self.normalized(), 2)
        return d


@dataclass
class RiskPlan:
    """Parámetros de ejecución calculados por el RiskManager (código duro)."""

    entry_price: float                 # en divisa de cotización
    entry_price_eur: float
    atr: float
    atr_stop: float
    hard_stop: float
    stop_loss_price: float             # en divisa de cotización
    take_profit_target: float          # en divisa de cotización
    risk_per_share: float
    shares: float
    position_size_eur: float
    risk_eur: float
    tradeable: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {k: (round(v, 4) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class CIODecision:
    ticker: str
    action: Action
    final_score: int
    confidence_level: Confidence
    justification: str
    suggested_position_size_eur: float
    stop_loss_price: float
    take_profit_target: float
    vetoes: list[str] = field(default_factory=list)
    guardrail_notes: list[str] = field(default_factory=list)

    def to_output_json(self) -> dict[str, Any]:
        """Esquema EXACTO de salida exigido por la especificación."""
        return {
            "ticker": self.ticker,
            "action": self.action.value,
            "final_score": int(self.final_score),
            "confidence_level": self.confidence_level.value,
            "justification": self.justification,
            "execution_parameters": {
                "suggested_position_size_eur": round(float(self.suggested_position_size_eur), 2),
                "stop_loss_price": round(float(self.stop_loss_price), 2),
                "take_profit_target": round(float(self.take_profit_target), 2),
            },
        }

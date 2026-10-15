import math

import pytest

from config import RiskRules
from risk_manager import RiskManager


@pytest.fixture
def rm():
    return RiskManager(RiskRules())


def test_atr_stop_used_when_closer_than_hard_stop(rm):
    plan = rm.build_plan(price=100.0, atr=2.0)        # ATR stop 96 > hard 85
    assert plan.stop_loss_price == pytest.approx(96.0)
    assert plan.take_profit_target == pytest.approx(108.0)   # 1:2
    assert plan.position_size_eur == pytest.approx(700.0)    # tope 10 %
    assert plan.risk_eur == pytest.approx(700 * 0.04)


def test_hard_stop_caps_at_minus_15pct(rm):
    plan = rm.build_plan(price=100.0, atr=10.0)       # ATR stop 80 < hard 85
    assert plan.stop_loss_price == pytest.approx(85.0)
    assert plan.take_profit_target == pytest.approx(130.0)
    assert plan.risk_eur <= 140.0 + 1e-9


def test_missing_atr_falls_back_to_hard_stop(rm):
    plan = rm.build_plan(price=50.0, atr=float("nan"))
    assert plan.stop_loss_price == pytest.approx(42.5)


def test_risk_rule_binds_when_stop_is_wide():
    # Con stop del 30 % (regla relajada), 700 € arriesgarían 210 € > 140 € -> se reduce el tamaño.
    rm = RiskManager(RiskRules(hard_stop_pct=0.30))
    plan = rm.build_plan(price=100.0, atr=20.0)       # ATR stop 60 < hard 70 -> 70
    assert plan.stop_loss_price == pytest.approx(70.0)
    assert plan.position_size_eur == pytest.approx(140 / 0.30)
    assert plan.risk_eur == pytest.approx(140.0)


def test_min_trade_size_blocks_operation():
    rm = RiskManager(RiskRules(allow_fractional_shares=False))
    plan = rm.build_plan(price=500.0, atr=5.0)        # 1 acción = 500 € OK
    assert plan.tradeable and plan.shares == 1
    plan = rm.build_plan(price=800.0, atr=5.0)        # 0 acciones (800 > 700)
    assert not plan.tradeable and plan.position_size_eur == 0.0


def test_whole_shares_below_minimum():
    rm = RiskManager(RiskRules(allow_fractional_shares=False))
    plan = rm.build_plan(price=260.0, atr=2.0)        # 2 acciones = 520 € OK
    assert plan.shares == 2 and plan.position_size_eur == pytest.approx(520.0)
    plan = rm.build_plan(price=360.0, atr=2.0)        # 1 acción = 360 € OK
    assert plan.shares == 1
    plan = rm.build_plan(price=290.0, atr=2.0, cash_available_eur=400)  # 1 acción = 290 € < 300
    assert not plan.tradeable


def test_fx_conversion(rm):
    plan = rm.build_plan(price=400.0, atr=8.0, fx_to_eur=0.9)
    assert plan.position_size_eur == pytest.approx(700.0)
    assert plan.shares == pytest.approx(700 / 360)
    assert plan.risk_eur == pytest.approx(plan.shares * 16 * 0.9)


@pytest.mark.parametrize("price", [0, -1, math.inf])
def test_invalid_price(rm, price):
    with pytest.raises(ValueError):
        rm.build_plan(price=price, atr=1.0)

import time

import pytest

import init_portfolio
from database import Database
from main import Orchestrator
from portfolio import PortfolioError, PortfolioManager, format_portfolio, format_trade
from risk_manager import RiskManager
from telegram_listener import TelegramBot
from tests.conftest import FakeLLM, make_ohlcv, make_snapshot

PRICES = {"SPPW.DE": 48.0, "O": 55.0, "NEW.DE": 10.0}
FX = {"USD": 0.9}


def manager(tmp_path, cash=3900.0, prices=PRICES):
    db = Database(str(tmp_path / "t.db"))
    db.reset_portfolio([
        {"ticker": "SPPW.DE", "nombre_activo": "SPDR MSCI World", "cantidad": 10, "precio_medio_compra": 40.0, "divisa": "EUR"},
        {"ticker": "O", "nombre_activo": "Realty Income", "cantidad": 2, "precio_medio_compra": 60.0, "divisa": "USD"},
    ], cash)
    return PortfolioManager(db, price_fn=prices.get, fx_fn=FX.get, meta_fn=lambda t: ("EUR", f"Nombre {t}"))


# ------------------------------------------------------------------ database
def test_reset_portfolio_replaces_positions_and_sets_cash(tmp_path):
    pm = manager(tmp_path)
    pm.db.reset_portfolio([{"ticker": "O", "cantidad": 1, "precio_medio_compra": 50.0, "divisa": "USD"}], 100.0)
    assert [p["ticker"] for p in pm.db.get_positions()] == ["O"]
    assert pm.db.get_cash() == 100.0


def test_uninitialized_portfolio(tmp_path):
    pm = PortfolioManager(Database(str(tmp_path / "t.db")), price_fn=PRICES.get, fx_fn=FX.get)
    assert pm.capital() is None
    with pytest.raises(PortfolioError, match="init_portfolio"):
        pm.buy("SPPW.DE", 1, 40)


# ----------------------------------------------------------------- valuation
def test_snapshot_values_and_pnl(tmp_path):
    snap = manager(tmp_path).snapshot()
    by = {p.ticker: p for p in snap.positions}
    assert by["SPPW.DE"].valor_eur == pytest.approx(480.0)
    assert by["SPPW.DE"].pnl_eur == pytest.approx(80.0)
    assert by["O"].valor_eur == pytest.approx(2 * 55 * 0.9)
    assert by["O"].pnl_pct == pytest.approx((55 / 60 - 1) * 100)
    assert snap.total_capital_eur == pytest.approx(3900 + 480 + 99)
    text = format_portfolio(snap)
    assert "Capital total" in text and "SPPW.DE" in text and "efecto del tipo de cambio" in text


def test_missing_price_falls_back_to_cost(tmp_path):
    snap = manager(tmp_path, prices={}).snapshot()
    assert all(not p.precio_disponible and p.pnl_eur == 0 for p in snap.positions)
    assert "Sin cotización" in format_portfolio(snap)


def test_prices_are_cached(tmp_path):
    calls = []
    pm = manager(tmp_path, prices={})
    pm.price_fn = lambda t: calls.append(t) or 1.0
    pm.snapshot()
    pm.snapshot()
    assert sorted(calls) == ["O", "SPPW.DE"]


# -------------------------------------------------------------------- trades
def test_buy_existing_updates_average_and_cash(tmp_path):
    pm = manager(tmp_path)
    t = pm.buy("sppw.de", 10, 50.0)
    pos = pm.db.get_position("SPPW.DE")
    assert pos["cantidad"] == 20 and pos["precio_medio_compra"] == pytest.approx(45.0)
    assert pm.db.get_cash() == pytest.approx(3400.0) == t.cash_eur
    assert "Compra registrada" in format_trade(t)


def test_buy_new_ticker_in_usd_converts_cash(tmp_path):
    pm = manager(tmp_path)
    pm.meta_fn = lambda t: ("USD", "Nueva")
    pm.buy("NEWUS", 3, 10.0)
    assert pm.db.get_cash() == pytest.approx(3900 - 27.0)
    assert pm.db.get_position("NEWUS")["divisa"] == "USD"


def test_buy_without_price_uses_market(tmp_path):
    pm = manager(tmp_path)
    t = pm.buy("NEW.DE", 5)
    assert t.precio == 10.0 and pm.db.get_position("NEW.DE")["nombre_activo"] == "Nombre NEW.DE"


def test_buy_rejects_insufficient_cash(tmp_path):
    pm = manager(tmp_path, cash=100.0)
    with pytest.raises(PortfolioError, match="Liquidez insuficiente"):
        pm.buy("SPPW.DE", 10, 50.0)
    assert pm.db.get_cash() == 100.0


def test_partial_sell_keeps_average_and_books_pnl(tmp_path):
    pm = manager(tmp_path)
    t = pm.sell("SPPW.DE", 4, 50.0)
    pos = pm.db.get_position("SPPW.DE")
    assert pos["cantidad"] == pytest.approx(6) and pos["precio_medio_compra"] == 40.0
    assert t.pnl_realizado_eur == pytest.approx(40.0)
    assert pm.db.get_cash() == pytest.approx(4100.0)


def test_full_sell_removes_position(tmp_path):
    pm = manager(tmp_path)
    t = pm.sell("O", 2, 70.0)
    assert pm.db.get_position("O") is None
    assert t.pnl_realizado_eur == pytest.approx(2 * 10 * 0.9)
    assert "Posición cerrada" in format_trade(t)
    assert [r["side"] for r in pm.db.trades()] [0] == "SELL"


@pytest.mark.parametrize("ticker,qty,msg", [("SPPW.DE", 11, "Solo tienes"), ("XYZ", 1, "No tienes"), ("O", -1, "positiva")])
def test_sell_rejections(tmp_path, ticker, qty, msg):
    with pytest.raises(PortfolioError, match=msg):
        manager(tmp_path).sell(ticker, qty, 10.0)


# --------------------------------------------------------------- RiskManager
def test_risk_limits_scale_with_total_capital():
    plan = RiskManager().build_plan(100.0, 2.0, capital_eur=5000.0)
    assert plan.max_position_eur == pytest.approx(500.0) and plan.max_risk_eur == pytest.approx(100.0)
    assert plan.position_size_eur == pytest.approx(500.0)


def test_risk_default_capital_unchanged():
    plan = RiskManager().build_plan(100.0, 2.0)
    assert plan.capital_eur == 7000.0 and plan.position_size_eur == pytest.approx(700.0)


def test_small_capital_makes_trade_untradeable():
    plan = RiskManager().build_plan(100.0, 2.0, capital_eur=2500.0)   # 10 % = 250 € < 300 € mínimo
    assert not plan.tradeable and plan.max_position_eur == pytest.approx(250.0)


def test_existing_position_counts_towards_10pct_limit():
    rm = RiskManager()
    partial = rm.build_plan(100.0, 2.0, capital_eur=6000.0, existing_position_eur=200.0)
    assert partial.position_size_eur == pytest.approx(400.0)
    full = rm.build_plan(100.0, 2.0, capital_eur=6000.0, existing_position_eur=1145.0)
    assert not full.tradeable and "ya tienes 1145.00 €" in full.reason


def test_position_value_eur(tmp_path):
    pm = manager(tmp_path)
    assert pm.position_value_eur("sppw.de") == pytest.approx(480.0)
    assert pm.position_value_eur("O") == pytest.approx(99.0)
    assert pm.position_value_eur("NONE") == 0.0


class StubFetcher:
    def fetch(self, ticker):
        return make_snapshot(make_ohlcv(drift=0.0015, vol=0.009))


def test_orchestrator_uses_portfolio_capital(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    orch = Orchestrator(fetcher=StubFetcher(), db=db, llm=FakeLLM(fail=True),
                        capital_fn=lambda: (6000.0, 3900.0), exposure_fn=lambda t: 250.0)
    plan = orch.analyze("TEST").plan
    assert plan.capital_eur == 6000.0 and plan.max_position_eur == pytest.approx(600.0)
    assert plan.position_size_eur == pytest.approx(350.0)


def test_orchestrator_falls_back_when_valuation_fails(tmp_path):
    def boom():
        raise RuntimeError("yfinance caído")

    orch = Orchestrator(fetcher=StubFetcher(), db=Database(str(tmp_path / "t.db")), llm=FakeLLM(fail=True), capital_fn=boom)
    assert orch.analyze("TEST").plan.capital_eur == 7000.0


# ------------------------------------------------------------ init_portfolio
def test_position_from_eur_amount():
    pos = init_portfolio.position_from_eur_amount(
        {"ticker": "PYPL", "nombre_activo": "PayPal", "importe_eur": 10.0, "divisa": "USD"},
        price_fn=lambda t: 50.0, fx_fn=lambda c: 0.8,
    )
    assert pos["cantidad"] == pytest.approx(0.25) and pos["precio_medio_compra"] == 50.0


def test_init_portfolio_data_is_consistent():
    tickers = [p["ticker"] for p in init_portfolio.POSITIONS] + [p["ticker"] for p in init_portfolio.EUR_AMOUNT_POSITIONS]
    assert tickers == ["SPPW.DE", "L0CK.DE", "IS3N.DE", "BTC-EUR", "O", "PYPL"]
    assert init_portfolio.INITIAL_CASH_EUR == 3900.0


# -------------------------------------------------------------- Telegram bot
def make_bot(tmp_path, analyze_fn=None):
    sent = []
    bot = TelegramBot(manager(tmp_path), analyze_fn or (lambda t: None), token="t", chat_id="42", send=sent.append)
    return bot, sent


def update(text, chat_id=42, age_s=0):
    return {"update_id": 1, "message": {"chat": {"id": chat_id}, "date": int(time.time() - age_s), "text": text}}


def test_bot_ignores_other_chats(tmp_path):
    bot, sent = make_bot(tmp_path)
    assert bot.process_update(update("/comprar SPPW.DE 1 40", chat_id=999)) is None
    assert sent == [] and bot.portfolio.db.get_cash() == 3900.0


def test_bot_ignores_stale_commands(tmp_path):
    bot, sent = make_bot(tmp_path)
    bot.process_update(update("/comprar SPPW.DE 1 40", age_s=3600))
    assert "Ignoro" in sent[0] and bot.portfolio.db.get_cash() == 3900.0


def test_bot_buy_with_decimal_commas_and_bot_suffix(tmp_path):
    bot, sent = make_bot(tmp_path)
    bot.process_update(update("/comprar@OmarFinanzas_bot sppw.de 2,5 40,0"))
    assert "Compra registrada" in sent[0]
    assert bot.portfolio.db.get_cash() == pytest.approx(3800.0)


@pytest.mark.parametrize("text,expected", [
    ("/vender SPPW.DE", "Uso: /vender"),
    ("/vender SPPW.DE dos", "no es un número"),
    ("/vender SPPW.DE 99 40", "Solo tienes"),
    ("/foo", "Comando desconocido"),
    ("hola", "Comandos"),
])
def test_bot_errors_are_friendly(tmp_path, text, expected):
    bot, _ = make_bot(tmp_path)
    assert expected in bot.handle_text(text)


def test_bot_cartera(tmp_path):
    bot, _ = make_bot(tmp_path)
    assert "Capital total" in bot.handle_text("/cartera")


def test_bot_analiza_runs_orchestrator(tmp_path):
    db = Database(str(tmp_path / "a.db"))
    orch = Orchestrator(fetcher=StubFetcher(), db=db, llm=FakeLLM(fail=True), capital_fn=lambda: (6000.0, 3900.0))
    bot, sent = make_bot(tmp_path, orch.analyze)
    reply = bot.handle_text("/analiza test")
    assert sent[0].startswith("⏳ Analizando TEST")
    assert "TEST" in reply and "Análisis" in reply and "Traceback" not in reply


def test_bot_analiza_reports_failures(tmp_path):
    def fail(t):
        raise ValueError("Sin datos de precio")

    bot, _ = make_bot(tmp_path, fail)
    assert "No se pudo analizar" in bot.handle_text("/analiza ZZZ")


def test_poll_once_advances_offset(tmp_path, monkeypatch):
    bot, sent = make_bot(tmp_path)
    monkeypatch.setattr(bot, "_call", lambda method, http_timeout, **p: [update("/ayuda") | {"update_id": 7}])
    assert bot.poll_once() == 1
    assert bot.offset == 8 and "Comandos" in sent[0]

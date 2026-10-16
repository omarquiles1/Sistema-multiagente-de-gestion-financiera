from datetime import datetime, timedelta, timezone

import pytest
import requests
import schedule

import notifier
from agents.corporate_watch import CorporateWatchAgent, NewsItem, classify, format_corporate_alert
from database import Database
from models import Action, CIODecision, Confidence
from performance_tracker import PerformanceTracker, format_report
from scheduler import MarketService, should_alert
from watchlist import CorporateTarget, Watchlist, load_watchlist
from tests.conftest import make_ohlcv

NOW = datetime(2026, 10, 16, 21, 0, tzinfo=timezone.utc)


def decision(score=70, action=Action.BUY, vetoes=()):
    return CIODecision(
        ticker="SAN.MC", action=action, final_score=score, confidence_level=Confidence.HIGH,
        justification="Momentum y fundamentales <sólidos>.", suggested_position_size_eur=700.0,
        stop_loss_price=4.5, take_profit_target=6.0, vetoes=list(vetoes),
    )


# ------------------------------------------------------------------ notifier
@pytest.mark.parametrize("action,emoji", [("BUY", "🟢"), ("SELL", "🔴"), ("REDUCE", "⚠️"), ("HOLD", "⏸️")])
def test_format_signal_emoji_per_action(action, emoji):
    d = decision(action=Action(action))
    text = notifier.format_signal(d.to_output_json())
    assert text.startswith(f"{emoji} <b>{action} · SAN.MC</b>")
    assert "&lt;sólidos&gt;" in text   # HTML escapado


def test_format_signal_shows_veto_and_strength():
    d = decision(score=-75, action=Action.SELL, vetoes=["Riesgo: Volatilidad extrema"])
    text = notifier.format_signal(d.to_output_json(), d.vetoes)
    assert "Fuerte Venta" in text and "Veto de riesgo activo" in text
    assert "Tamaño" not in text   # sin tamaño si no es BUY


def test_send_without_credentials_returns_false(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setattr(requests, "post", lambda *a, **k: pytest.fail("no debe llamar a la red"))
    assert notifier.send_telegram_alert(decision().to_output_json()) is False


def test_send_posts_to_telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "42")
    calls = []

    class Resp:
        status_code, text = 200, "ok"

    monkeypatch.setattr(requests, "post", lambda url, json, timeout: calls.append((url, json)) or Resp())
    assert notifier.send_telegram_alert(decision().to_output_json()) is True
    url, payload = calls[0]
    assert url == "https://api.telegram.org/bot123:abc/sendMessage"
    assert payload["chat_id"] == "42" and payload["parse_mode"] == "HTML"


def test_send_network_error_returns_false(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "c")

    def boom(*a, **k):
        raise requests.ConnectionError("down")

    monkeypatch.setattr(requests, "post", boom)
    assert notifier.send_telegram_message("hola") is False


# ------------------------------------------------------------- alert filter
@pytest.mark.parametrize("score,vetoes,expected", [
    (61, (), True), (60, (), False), (-61, (), True), (-60, (), False), (10, ("Técnico: caída",), True),
])
def test_should_alert(score, vetoes, expected):
    assert should_alert(decision(score=score, vetoes=vetoes), strong=60) is expected


# ---------------------------------------------------------------- watchlist
def test_default_watchlist():
    wl = load_watchlist()
    assert wl.tickers == [
        "URTH", "EEM", "XLE", "URNM", "XLK", "XLI", "XLV",
        "META", "NVDA", "GOOGL", "AMZN", "ASML", "TSM", "RDDT", "IBKR", "MELI", "SPCX",
        "OKLO", "VST", "CCJ", "PLTR", "CEG",
        "IBE.MC", "REP.MC", "BBVA.MC", "SAN.MC", "TSK.MC", "IDR.MC", "ANA.MC",
    ]
    assert wl.corporate_watch == ()   # TSK y SpaceX cotizan: sin rastreo de noticias


# ---------------------------------------------------------- corporate watch
# Objetivo de ejemplo para probar el mecanismo (la watchlist real ya no vigila noticias).
TSK = CorporateTarget(
    name="Grupo TSK",
    queries=("Grupo TSK",),
    must_mention=("tsk",),
    keywords={
        "IPO / salida a bolsa / colocación": ("salida a bolsa", "opv", "ops", "ipo", "colocación acelerada"),
        "Contrato de gran escala": ("contrato", "adjudica", "epc"),
        "Litigio / riesgo": ("justicia", "avales", "cae"),
    },
)


@pytest.mark.parametrize("title,category", [
    ("Grupo TSK prepara una colocación acelerada", "IPO / salida a bolsa / colocación"),
    ("TSK se adjudica un contrato de 300 millones en Arabia Saudí", "Contrato de gran escala"),
    ("TSK cae un 6% tras ordenar la Justicia británica el pago de 81 millones en avales", "Litigio / riesgo"),
])
def test_classify_relevant(title, category):
    cats, _ = classify(title, TSK)
    assert category in cats


@pytest.mark.parametrize("title", [
    "Grupo TSK patrocina un torneo de golf en Gijón",       # de TSK pero irrelevante
    "La cotización de TSK en millones de euros",             # sin evento: no basta 'millones' 
    "Iberdrola sale a bolsa en un nuevo mercado",            # relevante pero no es TSK
    "Los tops del tipo de interés",                          # 'ops'/'ipo' no casan dentro de palabras
])
def test_classify_irrelevant(title):
    assert classify(title, TSK) == ([], [])


def test_corporate_watch_dedupes_and_ignores_old(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    items = [
        NewsItem("Grupo TSK estudia una OPV", "https://n/1", NOW - timedelta(days=1), "Expansión"),
        NewsItem("Grupo TSK estudia una OPV", "https://n/1", NOW - timedelta(days=1), "Expansión"),
        NewsItem("TSK firma un contrato EPC", "https://n/2", NOW - timedelta(days=40)),
    ]
    agent = CorporateWatchAgent(db, fetch=lambda q: items)
    first = agent.scan(TSK, now=NOW)
    assert [a.item.link for a in first] == ["https://n/1"]
    assert agent.scan(TSK, now=NOW) == []          # ya notificada
    assert "Grupo TSK" in format_corporate_alert(first[0])


def test_corporate_watch_survives_fetch_errors(tmp_path):
    def fail(q):
        raise requests.Timeout("lento")

    assert CorporateWatchAgent(Database(str(tmp_path / "t.db")), fetch=fail).scan(TSK, now=NOW) == []


# ------------------------------------------------------- performance tracker
def seeded_db(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    ohlcv = make_ohlcv(n=60)
    ohlcv.index = [NOW.replace(tzinfo=None) - timedelta(days=59 - i) for i in range(60)]
    ohlcv["close"] = 100.0
    db.upsert_market_data("AAA", ohlcv)
    db.upsert_market_data("BBB", ohlcv)
    for ticker, action, days in [("AAA", "BUY", 8), ("BBB", "SELL", 9), ("AAA", "HOLD", 10),
                                 ("AAA", "BUY", 31), ("AAA", "BUY", 2)]:
        ts = (NOW - timedelta(days=days)).isoformat(timespec="seconds")
        db.log_signal(ticker, 70, 70, 20, 65, action, "HIGH", 95.0, 700.0, "x", timestamp=ts)
    return db


def test_performance_tracker_hits_and_windows(tmp_path):
    prices = {"AAA": 110.0, "BBB": 104.0}
    reports = PerformanceTracker(seeded_db(tmp_path), price_fn=prices.get).evaluate(now=NOW)
    week, month = reports
    assert week.horizon_days == 7 and len(week.outcomes) == 3        # la de hace 2 días no entra
    assert week.hit_rate == pytest.approx(50.0)                       # BUY acierta, SELL falla
    assert week.mean_edge_pct == pytest.approx((10.0 - 4.0) / 2)
    assert [o.action for o in month.outcomes] == ["BUY"]
    text = format_report(reports)
    assert "Acierto: <b>50%</b>" in text and "1 HOLD" in text


def test_performance_tracker_flags_breached_stop(tmp_path):
    reports = PerformanceTracker(seeded_db(tmp_path), price_fn=lambda t: 90.0).evaluate(now=NOW)
    buy = next(o for o in reports[0].outcomes if o.action == "BUY")
    assert buy.stop_breached and not buy.hit


def test_performance_tracker_skips_missing_prices(tmp_path):
    reports = PerformanceTracker(seeded_db(tmp_path), price_fn=lambda t: None).evaluate(now=NOW)
    assert reports[0].outcomes == [] and len(reports[0].skipped) == 3


# ---------------------------------------------------------------- scheduler
class StubOrchestrator:
    def __init__(self, decisions):
        self.decisions = decisions

    def analyze(self, ticker):
        d = self.decisions[ticker]
        if isinstance(d, Exception):
            raise d

        class R:
            decision = d

        return R()


class NoCorporate:
    def scan(self, target):
        return []


def test_daily_job_only_alerts_strong_signals(tmp_path):
    sent = []
    wl = Watchlist(groups={"g": ("AAA", "BBB", "CCC", "DDD")})
    orch = StubOrchestrator({
        "AAA": decision(score=75),
        "BBB": decision(score=20, action=Action.HOLD),
        "CCC": decision(score=0, action=Action.HOLD, vetoes=["Riesgo: Volatilidad extrema"]),
        "DDD": ValueError("sin datos"),
    })
    service = MarketService(watchlist=wl, db=Database(str(tmp_path / "t.db")), orchestrator=orch,
                            corporate=NoCorporate(), send=sent.append)
    summary = service.daily_job()
    assert summary.alerted == ["AAA", "CCC"]
    assert summary.failed == ["DDD"]
    assert len(sent) == 3 and "DDD" in sent[-1]


# ------------------------------------------------------- cooldown de vetos
VETO = ["Riesgo: Volatilidad extrema"]


class SequenceOrchestrator:
    """Devuelve la decisión que toque en cada pasada diaria."""

    def __init__(self):
        self.next = None

    def analyze(self, ticker):
        d = self.next

        class R:
            decision = d
            reports = {}

        return R()


def veto_service(tmp_path):
    sent = []
    orch = SequenceOrchestrator()
    service = MarketService(watchlist=Watchlist(groups={"g": ("SPCX",)}), db=Database(str(tmp_path / "t.db")),
                            orchestrator=orch, corporate=NoCorporate(), send=sent.append)

    def run(day, score=-26, vetoes=VETO):
        orch.next = decision(score=score, action=Action.HOLD, vetoes=vetoes)
        return service.daily_job(now=NOW + timedelta(days=day))

    return service, sent, run


def test_veto_alerts_once_then_silent_for_4_days(tmp_path):
    _, sent, run = veto_service(tmp_path)
    assert run(0).alerted == ["SPCX"]
    for day in (1, 2, 3):
        assert run(day).muted == ["SPCX"]
    assert run(4).alerted == ["SPCX"]          # pasados 4 días vuelve a avisar
    assert len(sent) == 2


def test_veto_rearms_when_it_disappears(tmp_path):
    _, sent, run = veto_service(tmp_path)
    run(0)
    assert run(1, vetoes=[]).alerted == []     # sin veto ni señal fuerte: nada que avisar
    assert run(2).alerted == ["SPCX"]          # el veto vuelve: aviso nuevo


def test_veto_change_alerts_again(tmp_path):
    _, _, run = veto_service(tmp_path)
    run(0)
    assert run(1, vetoes=VETO + ["Técnico: Caída libre por debajo de SMA200"]).alerted == ["SPCX"]


def test_strong_signal_is_never_muted_by_veto_cooldown(tmp_path):
    _, _, run = veto_service(tmp_path)
    run(0)
    assert run(1, score=-75).alerted == ["SPCX"]


def test_sniper_opportunity_rearms_veto_alert(tmp_path):
    service, _, run = veto_service(tmp_path)
    run(0)
    service.sniper.on_opportunity("SPCX")
    assert run(1).alerted == ["SPCX"]


def test_register_weekdays_at_madrid_close(tmp_path):
    service = MarketService(watchlist=Watchlist(groups={}), db=Database(str(tmp_path / "t.db")),
                            orchestrator=StubOrchestrator({}), corporate=NoCorporate(), send=lambda t: True)
    sch = schedule.Scheduler()
    service.register(sch)
    timed = [j for j in sch.jobs if j.at_time is not None]
    days = sorted(j.start_day for j in timed)
    assert days == sorted(["monday", "tuesday", "wednesday", "thursday", "friday", "friday"])
    assert {str(j.at_time) for j in timed} == {"22:30:00", "23:00:00"}
    assert all(str(j.at_time_zone) == "Europe/Madrid" for j in timed)
    sniper = [j for j in sch.jobs if j.at_time is None]
    assert len(sniper) == 1 and sniper[0].interval == 5 and sniper[0].unit == "minutes"


def test_safe_wrapper_swallows_job_errors():
    def boom():
        raise RuntimeError("x")

    MarketService._safe(boom)   # no lanza


# ------------------------------------------------------- formato humano
def analysis_result(notes=(), justification="Fundamentales sólidos y tendencia alcista.", action=Action.BUY):
    from models import AgentReport

    class R:
        decision = CIODecision("META", action, 42, Confidence.MEDIUM, justification, 600.0, 650.0, 800.0,
                               guardrail_notes=list(notes))
        reports = {
            "fundamental": AgentReport("fundamental", 72, (0, 100)),
            "technical": AgentReport("technical", 35, (0, 100), flags=["Tendencia bajista"], metrics={
                "price": 700.0, "soporte_clave": 680.0, "soporte_clave_tipo": "SMA200",
                "soporte_clave_distancia_pct": -2.86, "divisa": "USD"}),
            "sentiment": AgentReport("sentiment", -10, (-100, 100)),
            "risk": AgentReport("risk", 30, (0, 100), flags=["Volatilidad extrema"]),
        }

    return R()


def test_format_analysis_is_human_readable():
    text = notifier.format_analysis(analysis_result())
    for expected in [
        "🟢 <b>COMPRAR · META</b>", "confianza media", "💵 Precio: 700,00 USD",
        "🏢 Fundamental: <b>72/100</b> (Sólido)", "📈 Técnico: <b>35/100</b> (Bajista)",
        "📰 Sentimiento: <b>-10</b> (Neutral)", "🛡️ Riesgo: <b>30/100</b> (Volatilidad extrema)",
        "💶 Tamaño sugerido: <b>600,00 €</b>",
        "🛑 Stop-loss: <b>650,00 USD</b> (−7,1 %)", "✅ Take-profit: <b>800,00 USD</b> (+14,3 %)",
        "🛡️ Soporte clave cercano: <b>680,00 USD</b> · SMA200 (−2,9 %)",
        "💬 <i>Fundamentales sólidos y tendencia alcista.</i>",
    ]:
        assert expected in text, expected
    assert "Nota" not in text


def test_format_analysis_hides_llm_errors():
    raw = "CIO en modo determinista (LLM no disponible: Could not resolve authentication method. Expected one of api_key)"
    text = notifier.format_analysis(analysis_result(
        notes=[raw, "Ya se recomendó comprar hace 2 días: se mantiene la posición para no sobreoperar"],
        justification="Score ponderado +42.0 (F +4, T +80, S +0, R +80). Decisión por umbrales sin LLM.",
        action=Action.HOLD,
    ))
    assert "authentication" not in text and "Traceback" not in text and "Score ponderado" not in text
    assert notifier.LLM_OFFLINE_NOTE in text
    assert "• Ya se recomendó comprar hace 2 días" in text
    assert "Tamaño sugerido" not in text            # solo en compras


def test_format_signal_also_hides_llm_errors():
    text = notifier.format_signal(decision().to_output_json(), notes=["CIO en modo determinista (LLM no disponible: x)"])
    assert "LLM no disponible" not in text and notifier.LLM_OFFLINE_NOTE in text

# Sistema Multiagente de Gestión Financiera

Sistema de **enrutamiento y consenso** en Python para una cartera de **7.000 €** de un inversor
residente fiscal en España. Prioridades: preservar el capital, reducir comisiones (evitar el
overtrading) y decidir por consenso entre agentes especializados.

```
              ┌──────────────── Orquestador (main.py) ────────────────┐
 ticker ──▶   │ 1-2. DataFetcher: OHLCV 2a, fundamentales, 15 titulares, │
              │      divisa→EUR, tipos FED (FRED) y BCE (ECB API)        │
              └──────┬──────────┬──────────────┬──────────────┬─────────┘
          3. paralelo│          │              │              │
           ┌─────────▼──┐ ┌─────▼──────┐ ┌─────▼───────┐ ┌────▼──────┐
           │Fundamental │ │  Técnico   │ │Sentimiento  │ │  Riesgo   │
           │ Python     │ │ pandas-ta  │ │ + Macro LLM │ │  Python   │
           │ 0..100     │ │ 0..100     │ │ -100..100   │ │ 0..100    │
           └─────────┬──┘ └─────┬──────┘ └─────┬───────┘ └────┬──────┘
                     └──────────┴──────┬───────┴──────────────┘
                            4-5.       ▼
                 RiskManager (código duro) ──▶ Agente CIO (LLM + guardarraíles)
                                                   │ 6. JSON estricto
                                                   ▼
                                        7. SQLite (signals_log...)
```

## Estructura

| Fichero | Responsabilidad |
|---|---|
| `main.py` | Orquestador y CLI |
| `config.py` | Reglas de riesgo, pesos del consenso, umbrales, P/E sectorial |
| `risk_manager.py` | `RiskManager`: tamaño, stop-loss, take-profit (nunca delegado al LLM) |
| `database.py` | Esquema SQLite (`market_data`, `fundamentals`, `signals_log`) |
| `llm_client.py` | Cliente Claude con salida JSON garantizada por esquema |
| `data/market_data.py` | Descarga de datos (yfinance, FRED, BCE) |
| `agents/fundamental.py` | P/E vs sector, crecimiento EPS YoY, margen neto, deuda/equity, FCF |
| `agents/technical.py` | RSI(14), MACD(12,26,9), SMA50/200, Bollinger(20,2), ATR(14) |
| `agents/sentiment.py` | Sentimiento de titulares + entorno de tipos (LLM) |
| `agents/risk.py` | Volatilidad, drawdown, VaR 95 %, beta vs S&P 500, ATR % |
| `agents/cio.py` | Consenso 35/30/20/15, vetos y JSON final |
| `agents/corporate_watch.py` | Vigilancia de noticias de empresas concretas (Grupo TSK) vía Google News |
| `watchlist.json` / `watchlist.py` | Activos del servicio continuo y empresas vigiladas |
| `notifier.py` | Alertas por Telegram (🟢 BUY, 🔴 SELL, ⚠️ REDUCE, ⏸️ HOLD) |
| `scheduler.py` | Servicio continuo: pasada diaria L-V 22:30 y auditoría los viernes |
| `performance_tracker.py` | Acierto de las señales de hace 7 y 30 días frente al precio actual |

## Instalación y uso

```bash
pip install -r requirements.txt
cp .env.example .env                      # rellena ANTHROPIC_API_KEY y Telegram
python main.py MSFT                       # informe + JSON
python main.py MSFT SAN.MC ASML.AS --json-only --db cartera.db
python -m pytest -q                       # 66 tests, sin red
```

Las variables se leen del `.env` (con `python-dotenv`) o del entorno del sistema; las del
sistema tienen prioridad.

## Servicio continuo y alertas

```bash
python scheduler.py                       # bucle continuo (Ctrl+C para parar)
python scheduler.py --once --dry-run      # una pasada ya, imprimiendo los mensajes sin enviarlos
python scheduler.py --report-now          # auditoría semanal ya
python performance_tracker.py --send      # auditoría suelta, enviada por Telegram
```

| Trabajo | Cuándo (Europe/Madrid) | Qué hace |
|---|---|---|
| Pasada diaria | L-V 22:30 | Analiza toda la `watchlist.json`, guarda cada señal y vigila noticias de Grupo TSK |
| Auditoría | Viernes 23:00 | Compara las señales de hace 7 y 30 días con el precio actual |

**Filtro de alertas** (`config.AlertRules`): solo se envía una señal si `final_score > +60`
(Fuerte Compra), `final_score < −60` (Fuerte Venta) o hay un **veto de riesgo** activo. Las
demás se guardan en SQLite sin avisar. Si un ticker falla, llega un aviso con la lista.

**Vigilancia corporativa**: para cada empresa de `corporate_watch` se consulta el RSS de Google
News en español, se exige que el titular la nombre (`must_mention`) y se clasifica por palabras
clave completas (IPO/colocación, contrato de gran escala, operación corporativa, litigio). Cada
noticia se avisa una sola vez (tabla `corporate_news_seen`) y se ignoran las de más de 14 días.

**Auditoría**: el precio de referencia es el cierre guardado en `market_data` el día de la señal.
BUY acierta si el precio sube; SELL/REDUCE si baja; HOLD no cuenta en el % de acierto. La
"desviación media a favor" es el retorno medio visto desde la dirección de la señal. Se marca si
un BUY ha perforado su stop-loss.

**Telegram**: crea un bot con @BotFather, escríbele un mensaje y lee tu `chat_id` en
`https://api.telegram.org/bot<TOKEN>/getUpdates`. Sin token, el servicio sigue funcionando y
registra un aviso.

El bucle necesita que el equipo esté encendido a las 22:30. Como alternativa, el Programador de
tareas de Windows puede lanzar `scheduler.py --once` L-V a las 22:30 y `--report-now` los viernes.

Sin credenciales de Anthropic el sistema **sigue funcionando**: el agente de Sentimiento usa un
léxico de respaldo y el CIO decide por umbrales sobre el score ponderado. Cada caso queda anotado
en `guardrail_notes` y en `signals_log.llm_justification`.

## Reglas de riesgo (`RiskManager`)

| Regla | Valor |
|---|---|
| Capital base | 7.000 € |
| Tamaño máximo por posición | 10 % = 700 € |
| Tamaño mínimo por operación | 300 € (por debajo: operación **no ejecutable**) |
| Riesgo máximo (entrada → stop) | 2 % = 140 € |
| Stop-loss | `max(precio − 2·ATR14, precio·0,85)` → el más cercano al precio |
| Take-profit | `precio + 2·(precio − stop)` (ratio 1:2) |

Notas:
* Los precios se expresan en la divisa de cotización y los importes en euros con el tipo de cambio
  del día (`USDEUR=X`, etc.). Las cotizaciones en peniques (GBp) se convierten a GBP.
* Con el tope duro del −15 %, una posición de 700 € arriesga como máximo 105 €, así que la regla
  del 2 % solo actúa si se relaja `hard_stop_pct`. Está implementada y testeada igualmente.
* `allow_fractional_shares=True` por defecto (Trade Republic, Revolut, IBKR). Con `False` se
  redondea a acciones enteras y se reaplica el mínimo de 300 €.

## Consenso del CIO

1. Cada score se normaliza a −100..100 y se calcula
   `0,35·F + 0,30·T + 0,20·S + 0,15·R` (el score de Riesgo es alto cuando el riesgo es **bajo**).
2. Claude recibe todos los informes y el plan de riesgo y devuelve el JSON del esquema exigido
   (`output_config.format` con JSON Schema estricto).
3. Guardarraíles en código, que se aplican después del LLM:
   * El LLM solo puede ajustar el score ponderado ±15 puntos.
   * **Veto de compra** si el Técnico marca *"Caída libre por debajo de SMA200"* o Riesgo marca
     *"Volatilidad extrema"* (vol. 60 d ≥ 60 % anual o ATR ≥ 6 % del precio).
   * No hay BUY si el plan no es ejecutable o si `final_score < 30`.
   * **Anti-overtrading**: la misma señal no se repite en 7 días (pasa a HOLD).
   * **Aviso fiscal**: un BUY con SELL/REDUCE en los 61 días anteriores avisa de la regla de los dos
     meses (art. 33.5 LIRPF).
   * `execution_parameters` se **sobrescriben** siempre con los valores del `RiskManager`.

Umbrales (`config.Thresholds`): BUY ≥ 30, REDUCE < −10, SELL < −40.

## Modelo LLM

Por defecto `claude-opus-5-5` (variable `LLM_MODEL`). El Sentimiento se ejecuta con `effort="low"`
y el CIO con `effort="high"`. Está activado el *fallback* del servidor (`fallbacks="default"`): si un
clasificador de seguridad rechaza la petición, la API la reintenta en otro modelo. Si aun así hay
rechazo, error o respuesta truncada, se pasa al modo determinista.

## Limitaciones

* El P/E sectorial es una tabla estática editable (`config.SECTOR_PE`), porque yfinance no lo publica.
* yfinance es una fuente no oficial y puede fallar o limitar peticiones; los fallos de cada fuente
  se degradan a valores neutros con aviso.
* `signals_log` registra señales, no operaciones ejecutadas: el cooldown y el aviso fiscal son
  aproximaciones. Para P&L real habría que añadir una tabla de operaciones.
* Esto es una herramienta de análisis, no asesoramiento financiero.

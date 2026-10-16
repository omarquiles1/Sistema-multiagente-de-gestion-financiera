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
| `agents/technical.py` | RSI(14), MACD(12,26,9), SMA50/200, Bollinger(20,2), ATR(14) y soporte clave |
| `agents/sentiment.py` | Sentimiento de titulares + entorno de tipos (LLM) |
| `agents/risk.py` | Volatilidad, drawdown, VaR 95 %, beta vs S&P 500, ATR % |
| `agents/cio.py` | Consenso 35/30/20/15, vetos y JSON final |
| `agents/corporate_watch.py` | Vigilancia de noticias de empresas no cotizadas vía Google News (ahora sin objetivos) |
| `watchlist.json` / `watchlist.py` | Activos del servicio continuo y empresas vigiladas |
| `notifier.py` | Alertas por Telegram (🟢 BUY, 🔴 SELL, ⚠️ REDUCE, ⏸️ HOLD) |
| `scheduler.py` | Servicio continuo: pasada diaria L-V 22:30, auditoría los viernes y francotirador |
| `sniper.py` | Francotirador: caídas intradía con velas de 1 minuto y alerta de "Buy the Dip" |
| `performance_tracker.py` | Acierto de las señales de hace 7 y 30 días frente al precio actual |
| `portfolio.py` | Cartera real: valoración, P&L, compras/ventas y capital total |
| `init_portfolio.py` | Carga (o recarga) la cartera real y la liquidez inicial |
| `telegram_listener.py` | Bot de comandos: /cartera, /comprar, /vender, /analiza, /operaciones |

## Instalación y uso

```bash
pip install -r requirements.txt
cp .env.example .env                      # rellena ANTHROPIC_API_KEY y Telegram
python main.py MSFT                       # informe + JSON
python main.py MSFT SAN.MC ASML.AS --json-only --db cartera.db
python -m pytest -q                       # 141 tests, sin red
```

Las variables se leen del `.env` (con `python-dotenv`) o del entorno del sistema; las del
sistema tienen prioridad.

## Servicio continuo y alertas

```bash
python scheduler.py                       # bucle continuo (Ctrl+C para parar)
python scheduler.py --once --dry-run      # una pasada ya, imprimiendo los mensajes sin enviarlos
python scheduler.py --report-now          # auditoría semanal ya
python scheduler.py --sniper-now --dry-run   # un escaneo del francotirador ya, sin enviar
python performance_tracker.py --send      # auditoría suelta, enviada por Telegram
```

| Trabajo | Cuándo (Europe/Madrid) | Qué hace |
|---|---|---|
| Pasada diaria | L-V 22:30 | Analiza toda la `watchlist.json`, guarda cada señal y vigila noticias de Grupo TSK |
| Auditoría | Viernes 23:00 | Compara las señales de hace 7 y 30 días con el precio actual |
| Francotirador | Cada 5 min con mercado abierto | Velas de 1 min: caída ≥ 5 % o RSI cruzando 30 → análisis completo |

**Filtro de alertas** (`config.AlertRules`): solo se envía una señal si `final_score > +60`
(Fuerte Compra), `final_score < −60` (Fuerte Venta) o hay un **veto de riesgo** activo. Las
demás se guardan en SQLite sin avisar. Si un ticker falla, llega un aviso con la lista.

**Anti-spam de vetos** (tabla `veto_alerts`): un activo que alerta solo por veto avisa la
primera vez y calla 4 días (`veto_alert_cooldown_days`) mientras el veto siga igual. Se rearma si
el veto desaparece o cambia, o si el francotirador detecta una oportunidad en ese activo. Las
señales fuertes (|score| > 60) y las alertas del francotirador nunca se silencian.

**Watchlist** (29 activos): ETF globales y sectoriales; Big Tech & IA (META, NVDA, GOOGL, AMZN,
ASML, TSM); disruptivas (RDDT, IBKR, MELI, SPCX = SpaceX); energía y nuclear (OKLO, VST, URNM,
CCJ, PLTR, CEG); y mercado español (IBE, REP, BBVA, SAN, TSK, IDR, ANA).

**Soporte clave**: el Agente Técnico devuelve `soporte_clave` (+ `soporte_clave_tipo` y
`soporte_clave_distancia_pct`): el nivel más cercano **por debajo** del precio entre SMA50, SMA200,
banda inferior de Bollinger, mínimo de 30 sesiones y mínimo de 52 semanas (los mínimos excluyen la
sesión de hoy). Las alertas lo muestran como "🛡️ Soporte clave cercano: precio (tipo, distancia)".

**Francotirador** (`sniper.py`): cada 5 minutos, para los activos con su mercado abierto (España
9:00–17:30 hora de Madrid; EE. UU. 9:30–16:00 hora de Nueva York), descarga en lote velas de 1
minuto y el diario de 3 meses. Dispara si la caída frente al cierre de ayer es ≥ 5 % o si el
RSI(14) recalculado con el precio en vivo cruza por debajo de 30. Entonces ejecuta los 5 agentes
con el precio de 1 minuto y avisa:
* Fundamental ≥ 60/100 → "🚨 OPORTUNIDAD DE CAÍDA DETECTADA: X ha caído un N%. El fundamental
  sigue intacto." con la señal del CIO y el soporte clave.
* Si no (incluidos los ETF, sin fundamentales) → "📉 CAÍDA FUERTE", sin etiqueta de oportunidad.
* Una vez al día por activo y regla; si la caída se agrava, nuevo aviso en cada tramo de 5 %.
* **Modo Buy the Dip del CIO**: en una oportunidad (Fundamental ≥ 60, sin errores; los ETF no
  cuentan), el CIO ignora los vetos de "Caída libre bajo SMA200" y "Volatilidad extrema" y el
  umbral de score, y emite **BUY** aunque el LLM proponga otra cosa. Siguen mandando el
  RiskManager (10 % del capital descontando lo que ya tienes, liquidez, mínimo de 300 €) y el
  anti-overtrading de 7 días. Fuera del francotirador los vetos funcionan como siempre.
* Latencia: 1 minuto es la resolución más fina de Yahoo, pero Yahoo publica el mercado español con
  ~15 min de retraso; EE. UU. llega casi en tiempo real. Umbrales en `config.AlertRules`.

**Vigilancia corporativa**: mecanismo para empresas **no cotizadas** (RSS de Google News, palabras
clave por categoría, cada noticia una vez). La lista `corporate_watch` está vacía: Grupo TSK
(TSK.MC) y SpaceX (SPCX) cotizan y se analizan como el resto.

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

## Cartera real y bot de comandos

```bash
python init_portfolio.py                  # BORRA la tabla portfolio y carga la cartera + 3.900 € de liquidez
python scheduler.py                       # servicio continuo + bot de comandos en un hilo
python telegram_listener.py               # solo el bot, sin análisis programados
```

| Comando | Qué hace |
|---|---|
| `/cartera` | Valor de cada posición (yfinance), peso, P&L por activo y total, liquidez y capital total |
| `/comprar TICKER CANTIDAD [PRECIO]` | Suma a la posición (precio medio ponderado) y resta de la liquidez |
| `/vender TICKER CANTIDAD [PRECIO]` | Reduce o cierra la posición, suma a la liquidez y anota el P&L realizado |
| `/analiza TICKER` | Ejecuta los 5 agentes ahora y responde con la señal y el plan de riesgo |
| `/operaciones` | Últimas 10 operaciones (tabla `trades`) |

* El precio va en la **divisa del activo** (USD para PYPL u O); sin precio se usa la cotización
  actual. Se aceptan decimales con coma.
* No se puede comprar por encima de la liquidez ni vender más de lo que se tiene.
* **Seguridad**: el bot solo obedece al chat `TELEGRAM_CHAT_ID`, e ignora (avisando) los comandos
  con más de 15 minutos, enviados mientras estaba apagado.
* Funciona por *long polling* con `requests` en un hilo, sin `python-telegram-bot`: así convive con
  el bucle síncrono de `schedule` y no añade dependencias.
* El P&L en euros de activos en divisa usa el tipo de cambio actual para coste y valor (no
  refleja el efecto divisa desde la compra).

Tablas nuevas: `portfolio` (ticker, nombre_activo, cantidad, precio_medio_compra, divisa),
`capital` (liquidez en EUR) y `trades` (histórico de operaciones).

## Reglas de riesgo (`RiskManager`)

Los límites se calculan sobre el **Capital Total = liquidez + valor actual de la cartera**. Si la
cartera no está inicializada o no se puede valorar, se usa el capital base de 7.000 €.

| Regla | Valor |
|---|---|
| Capital | Capital Total (≈ 6.000 € al cargar la cartera) |
| Tamaño máximo por posición | 10 % del capital, **descontando lo que ya tienes de ese activo** |
| Tamaño mínimo por operación | 300 € (por debajo: operación **no ejecutable**) |
| Riesgo máximo (entrada → stop) | 2 % del capital |
| Liquidez | La compra sugerida nunca supera la liquidez disponible |
| Stop-loss | `max(precio − 2·ATR14, precio·0,85)` → el más cercano al precio |
| Take-profit | `precio + 2·(precio − stop)` (ratio 1:2) |

Notas:
* Los precios se expresan en la divisa de cotización y los importes en euros con el tipo de cambio
  del día (`USDEUR=X`, etc.). Las cotizaciones en peniques (GBp) se convierten a GBP.
* Con el tope duro del −15 %, una posición del 10 % arriesga como máximo el 1,5 % del capital, así
  que la regla del 2 % solo actúa si se relaja `hard_stop_pct`. Está implementada y testeada igualmente.
* Con menos de 3.000 € de capital, el 10 % queda por debajo del mínimo de 300 € y ninguna compra
  es ejecutable.
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

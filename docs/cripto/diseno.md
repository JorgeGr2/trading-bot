# Especificación final v1: análisis diario de cripto, backtest y cartera de prueba (Binance spot, solo simulación)

- **Repositorio:** `/home/user/trading-bot`, rama `claude/trusting-fermat-jangmb`, commit `2dadb36`. Leído en modo solo lectura y sin usar la red.
- **Origen:** combina los cuatro diseños (señal, riesgo, validez y producto) según las puntuaciones, las mejores ideas y las señales de alarma de los dos jueces.
- **Estado:** **aprobado** el 21-oct-2026. Decisiones del apartado 18: regla v1 tal cual; caída máxima para el verde < 25 %; parada diaria del 20 % con reanudación automática en la decisión siguiente; verde en vivo estricto (revisiones secuenciales); referencia BTC comprar y mantener; con la caída del 50 % se deja de comprar y las ventas siguen sus reglas; informe a las 00:10 UTC. Los parámetros se registran en `docs/cripto/preregistro-v1.md` antes del primer backtest.

---

## 0. Qué hace el bot y qué no puede hacer

- **Nadie puede saber de antemano qué moneda va a ganar más mañana.** El bot no lo promete en ningún sitio.
- **Qué hace cada día:**
  - Ordena todas las criptomonedas evaluables de Binance con una **regla fija, versionada y registrada antes de ver resultados** (la regla v1).
  - Dice cuál es **«la mejor puntuada hoy según la regla v1»**, o bien **«hoy ninguna: quédate en USDT»**.
  - Lo acompaña de lo que esa regla hizo en el pasado y en la prueba en vivo, siempre con costes y frente a BTC.
- **Resultado más probable,** según la literatura y los diseños:
  - en mercados alcistas, una rentabilidad parecida a la de BTC o menor;
  - en los bajistas, menos caída;
  - la **elección de moneda** quizá no aporte nada demostrable.
  
  El diseño lo mide directamente (comparación con elegir al azar y con «BTC con el mismo filtro») y el informe lo dice en la cabecera.
- **Todo es simulado.** Solo se usan datos públicos de mercado. La guarda de «solo simulación» (`tests/test_simulation_only_guard.py`) sigue en vigor y se amplía.

**La regla v1, en seis líneas:**
1. **Universo:** cada día, las 100 criptomonedas más líquidas con par USDT y al menos 5 M$ de volumen diario mediano, calculado con los datos de ese día.
2. **Puntuación:** momentum ajustado por volatilidad en 5 horizontes (7, 14, 30, 60 y 90 días), combinados con el mismo peso.
3. **Mercado:** si BTC cierra por debajo de su media de 100 días, todo pasa a USDT.
4. **Cartera:** 5 posiciones de 18 $ (con 100 $). Se entra en el puesto ≤ 5 y se sale en el puesto > 12, si se pierde la tendencia, con −25 % desde la entrada o si cambia el mercado.
5. **Costes y ejecución:** costes realistas de Binance. En el backtest se ejecuta a la apertura siguiente; en vivo, al bid o ask real.
6. **Medición:** frente a BTC comprar y mantener, BTC con el mismo filtro y 1.000 carteras al azar. Fuera de muestra de verdad solo cuenta la prueba en vivo desde hoy.

---

## 1. Punto de partida verificado en el repo

| Pieza | Estado | Qué cambia |
|---|---|---|
| `crypto/binance.py` | Cliente de solo lectura con espejo `data-api.binance.vision` y respaldo `api.binance.com`. Controla el peso con `x-mbx-used-weight-1m` y trata 418, 429, 451 y 5xx | `BinanceError.code`; filtros de `exchangeInfo`; `bidQty`/`askQty` en `BookTicker`; importar `RateLimiter` desde `copybot/ratelimit.py` y no desde `solana/rpc.py` |
| `crypto/sync.py` | Descarga pares y velas diarias a `market_symbols`/`market_bars`, incluidos los pares en `BREAK` | **Fallos verificados** (apartado 3.3): línea 152 (retirada tras la última descarga), línea 25 (`HISTORY_START` = 2019) y `binance.py:196` (un par desaparecido aborta toda la sincronización) |
| `crypto/panel.py` | Velas en memoria; `Series.at(day)` | Añadir `trades`, episodios y carga por ventana |
| `crypto/universe.py` (2dadb36) | Filtros de antigüedad, liquidez, calidad, estables, `PEGGED_OR_WRAPPED` y apalancados, con el motivo en castellano | Ventana de calidad de 90 → **100** días, antigüedad **por episodio** y `trades > 0`. Aún **no tiene tests** |
| `crypto/signals.py` (2dadb36) | Implementa **exactamente** la puntuación del apartado 5.1 y `trend_share` (SMA 20/50/100) | Sin cambios de fórmula. Aún **no tiene tests** |
| `sim/risk.py` | `BookState`, `entry_rejections` y `halt_reasons` | Se reutilizan con el símbolo en lugar del `mint` (cambio de nombre `open_mints` → `open_assets`) |
| `backtest/stats.py` | `trade_stats` (bootstrap i.i.d., semilla 7) y `max_drawdown_pct` | Añadir bootstrap estacionario, bootstrap por grupos, PSR, DSR, MinTRL y Spearman |
| Guarda de solo simulación | Ya cubre `/api/vN/order`, `/account`, `/myTrades`, `/sapi/`, `/fapi/`, `/dapi/`, `X-MBX-APIKEY`, `hmac`, `api_secret`, `ccxt`, `python-binance`, `binance-connector` y `binance-sdk-spot` | Añadir `listenKey` y `userDataStream`, más un test de las URL permitidas nuevas (apartado 15) |
| `.env.example` | **Límites antiguos** (10 $, 5 posiciones, 20 %, 10 % y 25 %) | Actualizar a los topes vigentes (25 $, 8, 10 %, 20 % y 50 %). Si no se actualiza, una copia a `.env` hará que la cartera use 10 $ por posición |
| `backtest_price_bars` | Tabla de memecoins | **No se usa.** Todo va en `market_bars` |

---

## 2. Qué necesita red de Binance y qué se hace sin red

| Pieza | Red | Dominios |
|---|---|---|
| `crypto sync` (pares, filtros y velas; histórico y diario) | **[RED]** | `data-api.binance.vision` (primero) y `api.binance.com` (respaldo) |
| Calibración de costes (`bookTicker`, `ticker/24hr`) | **[RED]** | Los mismos |
| Paso diario de la cartera en vivo (precio de ejecución con `bookTicker`) y `crypto daily`/`run` | **[RED]** | Los mismos |
| Fixtures grabadas y *smoke tests* `@pytest.mark.live` | **[RED]** una vez | Los mismos |
| Archivo de pares desaparecidos (`crypto archive`) | **[RED, opcional]** | `data.binance.vision` y `s3-ap-northeast-1.amazonaws.com` (listado). Hay que añadirlos a los dominios permitidos |
| Universo, señales, `decide()`, costes, motor de backtest, referencias, nulo, estadística, semáforo, informes, plantillas, cartera (con `respx`) y migraciones | **[SIN RED]** | Se programa y se prueba con datos sintéticos, fixtures y PostgreSQL de test |
| Backtest real, `analyze` de días pasados, `today`, `coin`, `history` y `semaforo` | **[SIN RED]** cuando las velas ya están en la base de datos | — |

Para habilitar la red en el entorno en la nube hay que añadir esos dominios en **Network access → Allowed domains**, como en el apartado 11 de la Fase 1.

---

## 3. Datos

### 3.1 Endpoints (solo públicos)

| Endpoint | Uso | Peso¹ | Paginación |
|---|---|---|---|
| `GET /api/v3/exchangeInfo?showPermissionSets=false` | Pares, `status`, `isSpotTradingAllowed`, filtros `PRICE_FILTER.tickSize`, `LOT_SIZE.stepSize`, `MARKET_LOT_SIZE.stepSize` (si > 0) y `NOTIONAL.minNotional` o `MIN_NOTIONAL.minNotional` | 20 | Una llamada |
| `GET /api/v3/klines?symbol=S&interval=1d&startTime&endTime&limit=1000` | Velas diarias | 2 | Cursor `startTime` = última apertura + 1 día; se termina cuando la página trae menos de 1.000 velas. Las velas con `close_time ≥ ahora` se descartan (ya lo hace `klines_range`) |
| `GET /api/v3/ticker/bookTicker` (sin símbolo) | bid, ask, bidQty y askQty: ejecución simulada, diferencial y calibración | 4 | Una llamada |
| `GET /api/v3/ticker/24hr` (sin símbolo) | Solo calibración y contexto. **Nunca** para decidir: es una ventana móvil de 24 h, no la vela diaria | 80 | Una al día como mucho |

¹ Los pesos hay que comprobarlos con red: Binance los cambia.

### 3.2 Límites y reintentos (ya implementados en `BinanceMarket._get`; se mantienen)

- Hasta 8 peticiones por segundo.
- Si el peso usado en el minuto llega a 5.000 de los 6.000 permitidos, se espera al minuto siguiente.
- **429:** se espera lo que diga `Retry-After`. **418:** se para sin insistir. **451:** se prueba la otra URL. **5xx:** reintentos con espera creciente.
- **Nuevo:** si la respuesta es un JSON `{"code": …}` con estado 400, se lanza `BinanceError(code=…)`. Solo `-1121` («Invalid symbol») se trata como «par desaparecido» y **no aborta** la sincronización; cualquier otro error sí la aborta, de forma visible.
- **Volumen estimado:**
  - Descarga completa: unos 700-900 pares USDT × unas 4 páginas ≈ 3.500 peticiones, unos 8 minutos y unos 960 de peso por minuto.
  - Cada día: unas 420 peticiones, alrededor de 1 minuto.

### 3.3 Correcciones obligatorias de `sync.py` antes de cualquier backtest (todas **[SIN RED]**, con tests `respx`)

| # | Fallo (verificado) | Arreglo | Test |
|---|---|---|---|
| S1 | `sync.py:152` salta los pares que no están en `TRADING` y ya tienen `bars_synced_to`. Un par retirado después de la última descarga **pierde las velas de su caída final** | Columna `final_sync_at`. Si un par deja de estar en `TRADING` y `final_sync_at` es nulo, se pide **una vez más** desde `bars_synced_to + 1 día` y después se congela | Par en `TRADING` el día 1 y en `BREAK` el día 5: las velas de los días 2 a 4 están |
| S2 | Un par que desaparece de `exchangeInfo` conserva `TRADING`. Sus `klines` responden 400 (-1121) y `binance.py:196` lanza `BinanceError`, lo que **aborta todo `sync_bars`** | `sync_symbols` marca `status='DESAPARECIDO'` y `status_changed_at` en los pares conocidos que ya no salen. Se intenta un último `klines`; si da -1121, se congela con `final_sync_at` y se sigue con el siguiente par | Un par ausente no aborta la sincronización y los demás se descargan |
| S3 | `HISTORY_START = 2019-01-01` (línea 25) deja fuera el mercado bajista de 2018 | `HISTORY_START = 2017-07-01` más **relleno hacia atrás**: columna `history_requested_from`. Si `--since` es anterior, se pide `[since, history_requested_from)`. Sin esto, cambiar la constante no afecta a los pares ya descargados | Un par con velas desde 2019 recibe las de 2018 sin duplicados |
| S4 | No se guardan los filtros ni el cambio de estado | Columnas `status_changed_at`, `tick_size`, `step_size`, `min_notional`, `filters_updated_at` y `listing_source` (`api` o `archivo`) | Parseo de los 4 tipos de filtro, incluido `MARKET_LOT_SIZE` con `stepSize = 0` |
| S5 | `binance.py` importa `RateLimiter` de `copybot.solana.rpc` | Moverlo a `copybot/ratelimit.py` y dejar en `solana/rpc.py` una importación de compatibilidad | Los tests existentes siguen pasando |

`PEGGED_OR_WRAPPED` (PAXG, XAUT, WBTC, WBETH, BETH, BNSOL, STETH, WSTETH, CBBTC y RENBTC) **ya está** en `universe.py` (2dadb36).

### 3.4 Pares desaparecidos: archivo público de Binance (**[RED, opcional]**, `crypto/archive.py`)

- **Listado:**
  - `GET https://s3-ap-northeast-1.amazonaws.com/data.binance.vision?delimiter=/&prefix=data/spot/monthly/klines/`, paginado con `marker`/`NextMarker` mientras `IsTruncated = true`.
  - Se extraen `<Prefix>` y `<NextMarker>` con una **expresión regular**, sin parser XML completo (datos no fiables y sin dependencias nuevas).
  - Se toman los que terminan en `USDT/`.
- **Para cada símbolo USDT** que no esté en `market_symbols`, o que esté en `DESAPARECIDO` o `BREAK` con historia incompleta:
  - se descarga `data/spot/monthly/klines/{S}/1d/{S}-1d-{AAAA}-{MM}.zip` y su `.CHECKSUM`;
  - se verifica con `hashlib.sha256`, **nunca `hmac`** (lo prohíbe la guarda); si no coincide, el fichero se rechaza.
- **Seguridad:**
  - el zip se abre en memoria, con un máximo de 2 MB y exactamente un miembro `.csv`;
  - no se extrae a disco;
  - cada fila se valida como `parse_kline`.
- **Formato:**
  - CSV de 12 columnas;
  - si la primera celda no es un número, es una cabecera y se salta;
  - **si `open_time > 10**14` son microsegundos** (archivo spot desde 2025): se divide entre 1.000.
- **Alta:** `status='DESAPARECIDO'`, `base = symbol[:-4]`, `listing_source='archivo'`.
- **Comprobación cruzada:** 20 pares activos y 20 retirados que estén en la API y en el archivo deben tener OHLCV idénticos. Las diferencias van al informe de calidad.
- **Sin archivo:** el backtest declara «sesgo de supervivencia no corregido del todo» y su luz no pasa de ÁMBAR (apartado 9).

### 3.5 Episodios, huecos, saltos, renombres y retiradas

- **Episodio:**
  - tramo continuo de velas de un par; un hueco de **más de 7 días** sin velas con `trades > 0` abre un episodio nuevo (por ejemplo, LUNA clásica y LUNA 2.0);
  - **ninguna rentabilidad ni media cruza un hueco**;
  - la antigüedad se cuenta desde el inicio del episodio.
- **Vela con `trades = 0`:** cuenta como día sin negociación, igual que un hueco.
- **Hueco global:** un día sin vela de BTCUSDT. No se cuenta como hueco de cada par; ese día no hay decisión (`sin_datos`) y se lista en el informe de calidad.
- **Saltos:** si |ln(Cₜ/Cₜ₋₁)| > ln 10, se listan en el informe de calidad **pero no se borran** (el desplome de LUNA es real). El universo ya excluye saltos de ×4 o ÷4 en su ventana de calidad.
- **Renombres** (`crypto/renames.py`, tabla manual con enlace al anuncio de Binance):
  - la posición se cierra al último cierre, sin penalización;
  - candidatos que hay que verificar con red: MATIC→POL, FTM→S, RNDR→RENDER;
  - un renombre que no esté en la tabla se trata como retirada, que es lo prudente.
- **Retiradas:** se tratan en el apartado 6.6.

### 3.6 Calidad (`crypto quality`, **[SIN RED]** con la base de datos cargada)

- Cobertura de velas por par y día dentro del universo: **≥ 98 %**.
- Huecos globales.
- Saltos.
- |U| por año.
- Pares que estuvieron alguna vez en el universo y hoy no cotizan, en número y porcentaje.
- Estado del archivo.
- Fecha de la primera vela de cada par.

**Mirar la calidad de los datos está permitido antes del preregistro. Mirar el rendimiento, no.**

---

## 4. Universo a fecha t, U(t), sin sesgo de supervivencia

Un par está en U(t) si cumple **todo** lo siguiente, usando **solo** velas con apertura ≤ t:

1. Cotiza contra USDT y su base no está en `STABLE_BASES` ni en `PEGGED_OR_WRAPPED`.
2. No es un token apalancado: la base termina en UP, DOWN, BULL o BEAR, es más larga que el sufijo más un carácter **y** el prefijo es otra base conocida. BTCUP se excluye; JUP y SYRUP no (hay test).
3. Tiene vela en t con `trades > 0`.
4. Su episodio dura ≥ **120** días.
5. Los **100** últimos días tienen vela con `trades > 0`, sin huecos (salvo los globales) y sin ningún cierre ×4 o ÷4 respecto al anterior. Son 100 días para cubrir la SMA100 y el retroceso de 91 días.
6. La volatilidad de 60 días es ≥ 0,5 % diario. Por debajo, la moneda está anclada o es estable.
7. La mediana del `quote_volume` de los últimos 30 días es ≥ **5.000.000 USDT**.
8. Está entre las **100** primeras por esa mediana (empates por símbolo).

**Lo que nunca se usa para el pasado:** el estado actual (`TRADING`, `BREAK` o `DESAPARECIDO`), `ticker/24hr`, los filtros actuales de `exchangeInfo` ni el diferencial actual.

**Filtros solo en vivo:** `status = TRADING`, `isSpotTradingAllowed` y un diferencial de `bookTicker` ≤ 0,5 %.
- **Solo bloquean compras.** No cambian U(t) ni el ranking, para que el ranking en vivo sea idéntico al del backtest.
- Se registran como «bloqueada en vivo: motivo».

**Medidas de supervivencia en cada informe de backtest:**
- pares que entraron alguna vez en U y ya no cotizan;
- porcentaje de días con posición en monedas hoy retiradas;
- una **ejecución solo con supervivientes** (los pares en `TRADING` hoy). La diferencia con la principal es el sesgo estimado; sin archivo, es una cota inferior.

Si U no ha contenido nunca un par que hoy no cotiza, el backtest queda en **GRIS** (datos incompletos).

---

## 5. La regla v1 (`STRATEGY_V1`)

### 5.1 Fórmulas

`C_i(t)` es el cierre diario de la moneda i (vela que cierra a las 00:00 UTC de t+1) y `r_i(t) = ln(C_i(t)/C_i(t−1))`.

```
Volatilidad    σ_i(t)   = max( pstdev(r_i(t−59) … r_i(t)), 0,01 )
Momentum       m_{i,L}  = ln( C_i(t−1) / C_i(t−1−L) )          L ∈ {7, 14, 30, 60, 90}; se salta el día t
Ajustado       a_{i,L}  = m_{i,L} / ( σ_i(t) · √L )
Percentil      p_{i,L}  = ( #{j: a_{j,L} < a_{i,L}} + ½·(#{j: a_{j,L} = a_{i,L}} − 1) ) / (n − 1)     (n = |U(t)|)
Puntuación     S_i(t)   = 100 · media_L p_{i,L}                 (0-100)
Orden          S desc → a_{i,30} desc → símbolo asc             (determinista)
Tendencia      T_i(t)   = ( [C>SMA20] + [C>SMA50] + [C>SMA100] ) / 3     ∈ {0, ⅓, ⅔, 1}
Mercado        favorable(t) = C_BTC(t) > SMA100_BTC(t)
```

Es lo que ya calculan `signals.coin_signal`, `signals.rank` y `CoinSignal.trend_share`. BTC forma parte del universo: si es la más fuerte ajustada por riesgo, se compra BTC.

### 5.2 Parámetros congelados

Viven en un `dataclass(frozen=True)` dentro de `crypto/strategy.py`, con versión `cripto-v1` y `params_hash()`. **No se pueden cambiar por variables de entorno.**

| Grupo | Parámetro | Valor |
|---|---|---|
| Universo | Antigüedad, ventana de calidad, salto máximo, volatilidad mínima | 120 días, 100 días, ×4, 0,5 % |
| | Liquidez | Mediana de 30 días ≥ 5 M USDT y puesto ≤ 100 |
| Señal | Horizontes, salto, volatilidad | {7, 14, 30, 60, 90}, 1 día, σ60 con suelo del 1 % |
| Mercado | Filtro | BTC > SMA100 de BTC; si no, todo a USDT |
| Tendencia | Entrada / salida | T ≥ ⅔ / T ≤ ⅓ (SMA 20, 50 y 100) |
| Cartera | Posiciones N, entrada, salida por puesto | 5; puesto ≤ 5; puesto > 12 o fuera de U(t) |
| | Stop de emergencia | Cierre ≤ 0,75 × precio de entrada |
| | Tamaño | Apartado 6.2 (18 $ con 100 $); sin reequilibrios parciales |
| Decisión | Calendario | Diaria, con la vela cerrada de t; ejecución en t+1 |
| Costes | Comisión y deslizamiento | 0,10 % por lado más el tramo del apartado 6.5 |
| Libro de señales | Entradas y horizonte | Las 5 primeras con T ≥ ⅔, 10 $ cada una, 7 días (apartado 7) |

### 5.3 Variantes de sensibilidad (solo dos, registradas; ninguna sustituye a la principal)

| Código | Qué cambia | Para qué |
|---|---|---|
| **V1 «La nº 1 cada día»** | N = 1, entrada en el puesto ≤ 1, salida en el puesto > 1 y 90 % del patrimonio (solo en la curva A) | Responde a la petición literal y enseña su coste: cambiar de moneda cuesta un 0,24-0,50 % por cambio, **hasta un 90-180 % al año** si rota a diario, además de la concentración. **No se opera nunca** |
| **V2 «Sin filtro de mercado»** | La principal sin la regla BTC > SMA100 | Mide qué aporta salir a USDT en los mercados bajistas |

K registrado = **3** (la principal, V1 y V2). Las pruebas de estrés (apartado 8.6), las referencias y el nulo **no son variantes**: no cambian la regla de decisión y no se pueden elegir.

### 5.4 Por qué así

Citas que hay que verificar con red antes del informe de cierre:
- **Momentum y tendencia de 1 semana a 3 meses en cripto:** Liu y Tsyvinski (2021), Liu, Tsyvinski y Wu (2022), Dobrynskaya (2023).
- **Evidencia en contra:** Grobys y Sapkota (2019); Li et al., que encuentran que las anomalías se concentran en monedas ilíquidas y desaparecen con costes.
- **Ajuste por volatilidad:** Moskowitz, Ooi y Pedersen (2012); Barroso y Santa-Clara (2015).
- **Histéresis contra la rotación:** Novy-Marx y Velikov (2016).
- **1/N:** DeMiguel, Garlappi y Uppal (2009).

Decisiones que se derivan:
- **Combinar horizontes** evita elegir «el mejor» con el backtest.
- **El ajuste por volatilidad** evita el fallo típico del momentum bruto: comprar la altcoin más errática justo después de un subidón.
- **Con N = 5 y 18 $,** que una moneda se vaya a cero en un día cuesta un **18 %**, por debajo de la parada diaria del 20 %. Así se aprovecha la idea del diseño de riesgo con un solo parámetro.
- **Se descartan:**
  - el régimen de 4 votos, los topes asimétricos, el control de volatilidad, los stops horarios y unos 30 parámetros del diseño de riesgo, por sobreajuste implícito y porque no se pueden atribuir;
  - la regla bruta de 28 días con un suelo de liquidez de 500 k$.

---

## 6. Cartera: `decide()`, la misma función para el backtest y la prueba en vivo

### 6.1 Algoritmo de cada día (función pura)

`decide(vista: DayView, libro: BookView, p: StrategyParams, limits: RiskLimits, modo: Sizing) -> Decision`

```
Entrada: velas con apertura ≤ t (cerradas), libro tras las ejecuciones del día anterior.
1. Valorar:  E_t = efectivo + Σ q·C(t)    (posición sin vela en t: último cierre × 0,90)
             W_t = efectivo + Σ q·Low(t)  (peor valor del día)
2. Paradas (6.4) con halt_reasons() sobre BookState(positions_value = W_t − efectivo,
   day_start = E_{t−1}, peak = máx E hasta t−1).
3. Ventas (se registra el primer motivo que se cumpla; en vivo y en el backtest igual):
   a. mercado desfavorable                 → «mercado bajista: BTC cerró por debajo de su media de 100 días»
   b. no está en U(t) o no tiene puesto    → «fuera del universo: <motivo de universe.py>»
   c. C(t) ≤ 0,75 × precio de entrada      → «stop de emergencia: −25 % desde la compra»
   d. T(t) ≤ ⅓                             → «perdió la tendencia (por debajo de 2 de sus 3 medias)»
   e. puesto > 12                          → «bajó al puesto k (se vende a partir del 13)»
4. Compras (solo si el mercado es favorable, no hay parada y el modo no es PAUSED):
   huecos = N − posiciones que quedan
   candidatas = puestos ≤ 5 con T ≥ ⅔, sin posición abierta, no vendidas hoy y no bloqueadas en vivo
   se compran las primeras «huecos» por orden de puesto, con el tamaño s (6.2), si
   entry_rejections() no da ningún motivo y s ≥ máx(5 $, minNotional)
5. Ejecución: primero las ventas y después las compras (6.5).
```

- **Sin recompra el mismo día.**
- **Sin reequilibrios parciales:** ni se compra más de las que suben ni se recorta a las ganadoras.
- Cada orden guarda en JSON:
  - el puesto, `S`, `a_L`, `p_L`, σ, T y el régimen;
  - el motivo;
  - `params_hash`.

### 6.2 Tamaño dentro de los topes duros

- **Modo dólares** (cartera de 100 $, en vivo y en la curva B):
  - `s = redondeo_abajo_céntimo( min(limits.max_position_usd, (E_t − R)/N) )`;
  - `R` = `min_cash_reserve_pct` × capital inicial = 10 $.
- **Modo porcentaje** (curva A del backtest): `s = (E_t − 0,10·E_t)/5 = 18 % de E_t`, sin tope en dólares. Así la cartera puede crecer durante 7 años y la comparación con BTC tiene sentido.

| Patrimonio | s (modo dólares) | Comentario |
|---|---|---|
| 100 $ | 18,00 $ | 5 × 18 = 90 $; quedan 10 $ de reserva |
| 140 $ | 25,00 $ | Tope `MAX_POSITION_USD` |
| 60 $ | 10,00 $ | — |
| 30 $ | 4,00 $ | < 5 $: no se compra |

**Cumplimiento de los topes:**
- 5 ≤ 8 posiciones;
- 18-25 $ ≤ 25 $;
- la reserva la comprueba `entry_rejections`;
- el tope de 25 $ se aplica al **coste de entrada**: una posición puede valer más si sube (como en la Fase 4).

Si `RiskLimits` se endurece (menos posiciones o menos tamaño), se usa `N_ef = min(5, max_open_positions)`. En ese caso el informe avisa de que los límites no son los registrados y la luz en vivo queda en GRIS.

### 6.3 Salidas

Son las del paso 3 de 6.1.
- **Todo se decide con velas diarias cerradas,** igual en el backtest y en vivo. No hay stop horario ni velas de 1 h, para que no haya asimetrías entre ambos.
- Una caída dentro del día se refleja en `W_t` y en las paradas (6.4).

### 6.4 Paradas por riesgo (con `halt_reasons` de `sim/risk.py`)

| Parada | Cálculo | Efecto | Reanudación |
|---|---|---|---|
| **Pérdida diaria del 20 %** | `1 − W_t / E_{t−1} ≥ 20 %` | `HALTED_DAY`: **sin compras** en esa decisión; las ventas siguen | **Propuesta:** automática en la siguiente decisión diaria. Ahora la reanudación es manual (Fase 4): **lo decides tú** (apartado 18) |
| **Caída del 50 %** | `1 − W_t / máx(E_0…E_{t−1}) ≥ 50 %` | `HALTED_RISK`: sin compras; **las ventas siguen** por sus reglas, sin liquidación forzada | Solo a mano (`crypto paper resume`). En la curva B del backtest, nunca: «el bot se habría parado aquí» |

En la curva A del backtest **no** se aplica la parada del 50 %. Se mide: si se alcanza, el backtest sale en ROJO (apartado 9).

### 6.5 Costes y ejecución

- **Comisión** `f`: 0,10 % por lado (*taker*, sin descuento con BNB).
- **Medio diferencial más deslizamiento** `c_i`, según la mediana de volumen de 30 días a fecha t:

| Mediana de 30 días | c_i por lado | Ida y vuelta (2·(f + c)) |
|---|---|---|
| ≥ 200 M$ | 0,02 % | 0,24 % |
| 50-200 M$ | 0,04 % | 0,28 % |
| 10-50 M$ | 0,08 % | 0,36 % |
| 5-10 M$ | 0,15 % | 0,50 % |

- **Calibración [RED], antes del preregistro:**
  1. Varias fotos de `bookTicker` + `ticker/24hr` de todos los pares USDT a distintas horas, guardadas en `crypto_cost_snapshots`.
  2. Para cada tramo, el percentil 75 de (ask − bid)/(2·mid) más un colchón del 0,02 %.
  3. Si un tramo medido supera al de la tabla, **se sube la tabla antes de registrar v1**. Si `askQty·ask < 25 $` es habitual en un tramo, ese tramo pasa al siguiente nivel de coste.
  4. Después, una calibración al mes, solo para vigilar: la deriva aparece en el informe.
- **Backtest:**
  - compra: `qty = s / (O(t+1)·(1 + c_i)·(1 + f))`;
  - venta: `efectivo += qty·O(t+1)·(1 − c_i)·(1 − f)`;
  - `O(t+1)` es la apertura de la vela siguiente, a las 00:00 UTC.
- **En vivo:**
  - se compra al ask y se vende al bid de un `bookTicker` de menos de 60 s, más la comisión;
  - `qty` se redondea a la baja al `stepSize` (el de `MARKET_LOT_SIZE` si es > 0; si no, el de `LOT_SIZE`);
  - la orden se rechaza si `qty·precio < max(minNotional, 5 $)`;
  - si `askQty·ask < s`, se aplica un 0,5 % extra y se anota.
- **Brecha de ejecución** (la lección de la Fase 5): en cada orden en vivo se guarda `ref_open_price = O(t+1)` y `exec_gap_pct` = diferencia entre el precio de ejecución y la apertura.

### 6.6 Retiradas y huecos

- **Posición sin vela:**
  - se valora al último cierre × 0,90 y no se puede vender;
  - si vuelve a cotizar en ≤ 7 días, se aplican las reglas normales en la primera apertura disponible.
- **Sin vela durante 7 días seguidos** (fin del episodio, conocido ese día):
  - cierre al último cierre × **0,90**;
  - con sensibilidad al 0 % y al 100 % de pérdida;
  - si el par está en la tabla de renombres, cierre al último cierre sin penalización.
- **En vivo:**
  - si el estado deja de ser `TRADING`, se vende en la siguiente ejecución con `bookTicker`;
  - si no hay `bookTicker`, la posición se marca `unsellable`, se valora al último cierre × 0,90 y se aplica la misma regla de los 7 días.

---

## 7. Libro de señales: la respuesta medible a «¿cuál es la mejor hoy?»

- **Cada día t, sin filtro de mercado:**
  - se toman las 5 primeras del ranking con T ≥ ⅔;
  - **10 $ virtuales** en cada una, entrada en `O(t+1)` y salida en `O(t+8)` (7 días), con los costes del apartado 6.5;
  - BTC se apunta en la misma ventana y con los mismos costes;
  - cada entrada lleva el régimen del día y su puesto;
  - no tiene límite de efectivo.
- **Resultado:** el exceso por operación, `ln(1 + r_op) − ln(1 + r_BTC misma ventana)`.
- **Inferencia:**
  - primero se **agrega por día de entrada** (media de sus 5 operaciones), porque no son independientes;
  - después, bootstrap estacionario por días con L = 7 y 28.
- **En vivo:** en 28 días se cierran 21 × 5 = **105 operaciones**. Cumple la letra del criterio 2 de la Fase 1, **pero se solapan y están correlacionadas**: equivalen a unas 4 semanas independientes. El informe no las presenta como 105 observaciones independientes.

---

## 8. Protocolo del backtest

### 8.1 Datos y periodos

- **Datos:** `market_bars` de 1 día desde 2017-07-01, incluidos los pares en `BREAK` y `DESAPARECIDO`.
- **Inicio de la evaluación:** el primer día con |U(t)| ≥ 20. El informe dice qué día es; en 2018 había pocos pares USDT con 5 M$, así que puede caer a finales de 2018 o en 2019.
- **Fin:** el último día cerrado.
- **Todo el histórico es pseudo-fuera de muestra.** Los parámetros no se ajustan a los datos, pero quienes los eligieron conocían el mercado de esos años, incluidos LUNA, FTX y el 10 de octubre de 2025. **Lo único fuera de muestra de verdad es la prueba en vivo desde la fecha del preregistro.** El informe lo dice con esas palabras.
- **No hay holdout «sellado»:** sería un periodo pasado y ya conocido. El cerrojo contra el ajuste es el preregistro más el registro de pruebas (8.7).
- **Subperiodos fijados de antemano:**
  - inicio-2020;
  - 2021-2022 (mercado bajista de −77 % en BTC);
  - 2023-hoy;
  - cada año natural;
  - las dos mitades.

### 8.2 Tres curvas

| Curva | Reglas | Uso |
|---|---|---|
| **A: método** | Tamaño en porcentaje (6.2), parada diaria aplicada y caída del 50 % medida pero no aplicada | Estadística y semáforo del backtest |
| **B: cartera de 100 $** | Modo dólares con los `RiskLimits` vigentes y todas las paradas | Lo que habría pasado con tu cartera |
| **C: arranques cada lunes** | Reglas de B, un libro nuevo de 100 $ cada lunes, a 28, 90 y 365 días | «Si hubieras lanzado una prueba de N días cualquier lunes…»: % de arranques con pérdida, mediana, percentiles 5 y 95, peor caso, % que baten a BTC, % que tocan la parada diaria y % que tocan la del 50 %. Descriptivo (las ventanas se solapan) |

### 8.3 Referencias (mismo capital, misma hora y mismos costes)

1. **USDT** (0 %).
2. **BTC comprar y mantener** (100 %). Es la referencia del criterio acordado.
3. **BTC con el mismo filtro:** 90 % en BTC cuando BTC > SMA100 y USDT si no, con la misma ejecución. Separa el valor de **salir a tiempo** del de **elegir moneda**.
4. **90 % BTC + 10 % USDT:** la misma exposición máxima que la cartera.
5. **Cesta de las 10 más líquidas** de U(t), con el mismo peso, reequilibrada cada 28 días, con costes y retiradas.
6. **Nulo aleatorio emparejado, 1.000 semillas (1 a 1.000):**
   - las mismas reglas que la curva A (universo, mercado, tendencia, stop, tamaño y costes);
   - cada compra se elige **al azar** entre las monedas de U(t) con T ≥ ⅔ que no están en cartera;
   - la salida por puesto se sustituye por una duración sacada al azar de las **duraciones de las salidas por puesto de la estrategia principal**, para igualar la rotación;
   - se publica el percentil de la estrategia (valor final y Sharpe).

### 8.4 Métricas

- Rentabilidad total y anual.
- Caída máxima (con `W_t`).
- Sharpe y Sortino (√365) y Calmar.
- Peor mes.
- % de días invertido y % de meses mejor que BTC.
- Operaciones, duración media, porcentaje de aciertos, rotación anual y costes pagados ($ y puntos porcentuales al año).
- Todo por subperiodo, por año y por régimen.

### 8.5 Estadística

Se añade a `backtest/stats.py`, en Python puro con `statistics.NormalDist` y `random.Random(semilla)`, sin dependencias nuevas.

- **Exceso diario logarítmico:** `e_t = ln(1 + r_A,t) − ln(1 + r_BTC,t)`, de modo que `Σ e_t = ln(V_A/V_BTC)`.
  - Se muestra como `exp(365·ē) − 1`, en % al año frente a BTC.
  - IR = `ē/sd(e)·√365`.
- **Bootstrap estacionario** (Politis y Romano, 1994):
  1. El primer índice es uniforme; en cada paso, con probabilidad `1/L` se salta a un índice nuevo uniforme y, si no, se toma el siguiente (`(i+1) mod T`).
  2. B = 5.000 remuestreos, semilla 7, **L ∈ {7, 28, 56}**.
  3. Intervalo por percentiles 2,5-97,5; **cuenta el más ancho de los tres**.
- **Bootstrap por grupos** (operaciones de la cartera agrupadas por semana ISO de entrada) para el criterio por operación.
- **Calibración por puesto:**
  - cada día, todas las monedas de U(t) por tramos: 1, 2-5, 6-12, 13-30 y 31 o más;
  - medida: rentabilidad neta de `O(t+1)` a `O(t+8)` menos la de BTC, y % de veces que bate a BTC;
  - se agrega por día y se aplica bootstrap estacionario con L = 28;
  - se calcula sobre el backtest y, aparte, sobre los análisis emitidos en vivo.
- **IC de rangos:** correlación de Spearman entre `S_i(t)` y la rentabilidad de `t+1` a `t+8`.
  - Se dan la media, el % de días con IC > 0 y el intervalo (L = 28).
  - El horizonte de 7 días es el registrado; los de 1 y 28 son diagnóstico.
  - También se dan los quintiles de la rentabilidad a 7 días (deberían crecer de Q1 a Q5).
- **Sharpe deflactado** (Bailey y López de Prado, 2014), sobre `e_t`:
  ```
  PSR(SR*) = Φ( (SR̂ − SR*)·√(T−1) / √(1 − γ₃·SR̂ + (γ₄−1)/4·SR̂²) )      γ₄ = curtosis (3 en la normal)
  SR₀      = √V · ( (1−γ)·Φ⁻¹(1−1/K) + γ·Φ⁻¹(1−1/(K·e)) ),   γ = 0,5772;   SR₀ = 0 si K = 1
  V        = max( varianza de los SR̂_k de las K configuraciones, 1/T )      (suelo conservador)
  DSR      = PSR(SR₀)        umbral ≥ 0,95
  ```
  El factor entre paréntesis vale ≈ 0,853 con K = 3 y ≈ 1,459 con K = 8 (hay test).
- **MinTRL** (días necesarios): `1 + (1 − γ₃·SR̂ + (γ₄−1)/4·SR̂²)·(z/SR̂)²`. Se muestra como «días necesarios para poder afirmarlo». Si SR̂ ≤ 0: «no se puede demostrar: en el pasado no ganó a BTC».

### 8.6 Pruebas de estrés de la principal (fijadas de antemano; no cuentan en K)

Se marcan como superada o no superada:
- costes ×2 (obligatoria) y ×3 (informativa);
- **ejecución con un día de retraso**, a `O(t+2)`;
- sin las 3 monedas que más aportaron (excluidas del universo);
- las dos mitades del periodo y 2023-hoy;
- solo supervivientes;
- penalización por retirada del 0 % y del 100 %.

### 8.7 Comparaciones múltiples y registro de pruebas

- **Registro obligatorio:** `crypto backtest` **siempre** escribe en `crypto_backtest_runs`, con el hash canónico de la configuración, su papel, el commit, el hash de los datos y las métricas. No hay forma de ejecutarlo sin registrar.
- **Cálculo de K:**
  - K = número de hashes distintos con papel `principal`, `variante` o `adhoc`. Repetir una configuración no suma.
  - K previsto = 3. Cualquier prueba ad hoc (`--adhoc clave=valor`) sube K, se ve en el informe y lleva el aviso «NO CUENTA».
- **Semáforo:** todos sus criterios deben cumplirse a la vez (contraste de intersección-unión), así que no inflan el error de tipo I. El DSR corrige por las K configuraciones.
- **Diagnósticos:** la calibración, el IC, los quintiles y los subperiodos son **descriptivos**. No se usan para elegir nada.
- **Una variante nunca sustituye a la principal.** Para cambiar la regla hace falta:
  1. versión `cripto-v2` con su preregistro;
  2. un K acumulado que no se reinicia;
  3. una luz en vivo que empieza de cero con datos **posteriores** a su registro.

### 8.8 Reproducibilidad

- **Huella del informe:** sha256 de los datos usados (símbolo, `open_time`, OHLC, `quote_volume` y `trades`, ordenados), `params_hash`, commit, semilla y hash de la tabla de costes.
- Los mismos datos y la misma semilla dan el mismo informe: hay test.
- **Rendimiento:**
  - las medias móviles, la mediana de volumen, σ, los episodios y los momentum se precalculan una vez por serie en `crypto/features.py`;
  - un test comprueba que coinciden con `universe()` y `ranked_day()`, que se mantienen como implementación de referencia.

---

## 9. Semáforo «¿Es rentable?» (`crypto/semaforo.py`, función pura)

Tiene dos luces y una conclusión. **Lo dudoso se resuelve en contra de la estrategia.**

### 9.1 Luz del backtest (estimada, pseudo-fuera de muestra, curva A)

- **GRIS:** menos de 3 años evaluables; |U| < 20 en más del 5 % de los días; cobertura < 98 %; ningún par retirado en U; o un hash del preregistro que no coincide.
- **ROJO** (con datos suficientes):
  - exceso anual frente a BTC ≤ 0 con los costes normales;
  - exceso ≤ 0 con costes ×2;
  - caída máxima ≥ 50 %: la cartera de 100 $ se habría parado.
- **VERDE** si se cumple todo:

| # | Criterio |
|---|---|
| B1 | El límite inferior del intervalo del exceso frente a BTC es > 0 con L = 7, 28 y 56 |
| B2 | DSR ≥ 0,95 con el K registrado |
| B3 | Exceso > 0 con costes ×2 y con un día de retraso |
| B4 | Exceso > 0 sin las 3 monedas que más aportaron, en las dos mitades y en 2023-hoy |
| B5 | El valor final supera el percentil 95 del nulo aleatorio: elegir moneda aporta algo |
| B6 | Libro de señales: exceso medio por operación frente a BTC > 0, con el intervalo (L = 28) por encima de 0 (criterio 3 de la Fase 1) |
| B7 | Caída máxima < **25 %** (criterio 5 de la Fase 1). **Propuesta, que tienes que confirmar:** entre el 25 y el 50 % queda en ÁMBAR |
| B8 | Archivo de retiradas importado. Sin él, como mucho ÁMBAR |

- **ÁMBAR** en el resto de casos.
- **Mensajes fijos:**
  - si se cumplen de B1 a B4 pero falla B5: «Elegir moneda no aporta: lo que funciona es salir a USDT a tiempo. La versión simple (BTC con filtro) es mejor».
  - si «BTC con filtro» supera a la estrategia: «La versión simple gana».

### 9.2 Luz en vivo (cartera continua de la versión; apartado 11)

- **GRIS:** < 28 días; < 100 operaciones cerradas en el libro de señales; < 30 en la cartera; o límites distintos de los registrados.
- **ROJO** (tiene prioridad sobre el gris):
  - parada del 50 %;
  - algún día con decisiones distintas de la réplica del backtest sin explicar (fidelidad);
  - brecha de ejecución media > 2 veces el coste modelado;
  - disponibilidad < 99 % a partir del día 28;
  - en una revisión, el exceso acumulado por debajo del percentil 5 de los arranques de la misma duración (curva C): la regla se ha roto.
- **VERDE** si se cumple todo:
  - backtest en VERDE;
  - muestras suficientes;
  - **exceso diario acumulado frente a BTC con el límite inferior > 0, con corrección secuencial** (ver abajo), en la cartera **y** por operación en el libro de señales;
  - caída < 25 %;
  - exceso > 0 sin las 3 mejores operaciones y en las dos mitades;
  - fidelidad del 100 %;
  - 0 órdenes duplicadas;
  - disponibilidad ≥ 99 %.
- **Revisiones secuenciales:**
  - el intervalo **solo se mira cada 28 días**, como mucho **13 veces**, alrededor de 1 año;
  - Bonferroni: α unilateral de 0,025/13 por revisión → **z = 2,89** (intervalo bilateral al 99,6 %; bootstrap estacionario con L = 7 y 14, B = 10.000, semilla 7);
  - las pruebas no se reinician: cuentan todos los días en vivo de la versión;
  - tras la revisión 13 sin verde: «ventaja no demostrada en un año». La versión sigue funcionando a modo informativo, pero su luz en vivo ya no puede ponerse verde.
- **ÁMBAR** en el resto de casos, con los «días necesarios» (MinTRL con z = 2,89 y el SR̂ del backtest).

### 9.3 Conclusión y salida para la interfaz

- **Conclusión:**
  - ROJO si alguna luz está en rojo;
  - si no, GRIS si alguna está en gris;
  - si no, VERDE si ambas lo están;
  - si no, ÁMBAR.
- Un verde **no garantiza beneficios futuros**. Solo permite **plantear** la Fase 9, con tu aprobación expresa y los criterios del apartado 12.2 de la Fase 1.
- **JSON** (`crypto semaforo --json`):

```json
{"semaforo":"ambar","luces":{"backtest":"ambar","en_vivo":"gris"},"version":"cripto-v1:<hash>","huella":"<sha256-16>",
 "criterios":[{"id":"B1","texto":"Gana a BTC por un margen que no parece suerte",
   "valor":"+9 %/año (margen de error: −6 % a +25 %)","umbral":"límite inferior > 0","cumple":false}],
 "dias_necesarios":1400,"revision":{"hechas":0,"maximo":13,"proxima":"2026-11-18"},
 "aviso":"Un semáforo en verde no garantiza beneficios futuros."}
```

---

## 10. El informe diario en castellano llano (`crypto/report.py` y `crypto/explain.py`)

### 10.1 Maqueta

Las cifras son **inventadas** y solo sirven para enseñar el formato.

```
ANÁLISIS DEL MERCADO — miércoles 21 de octubre de 2026
Datos de Binance hasta el cierre del martes 20 (02:00, hora de Madrid) · Regla v1 (registrada el 23-oct-2026)
84 monedas evaluadas (de 412 pares con USDT) · Datos completos

RESPUESTA CORTA
Mercado: FAVORABLE. Bitcoin (68.400 $) cerró por encima de su media de 100 días (61.200 $).
La mejor puntuada hoy según la regla v1: SOL (94 de 100).
La cartera de prueba tiene: SOL, BTC, XRP, SUI y LINK.
Nadie puede saber de antemano qué moneda ganará más: esto es lo que elige una regla fija.
Abajo tienes cómo le ha ido a esa regla.
Confianza en la regla: BAJA. En el pasado ganó a BTC de media, pero el margen de error incluye perder.

LAS 10 MEJOR PUNTUADAS
 #  Moneda Puntos 7 días 30 días 90 días Se mueve al día Tendencia Frente a BTC (30 d) Volumen/día Riesgo Cartera
 1  SOL     94    +6 %   +18 %   +41 %   ±3,9 %          3 de 3    +9 puntos            2.100 M$    Medio  compra
 2  BTC     91    +3 %   +9 %    +22 %   ±2,1 %          3 de 3    —                   18.300 M$    Bajo   mantiene
 …
 Puntos = posición frente a las demás (0-100) en subida ajustada por lo que suele moverse.
 No es la probabilidad de que suba.

POR QUÉ
SOL: sube un 18 % en 30 días, más que 97 de cada 100 monedas analizadas, y está por encima de sus
  medias de 20, 50 y 100 días. La subida está repartida: su mejor día aportó el 15 %. Se negocian
  2.100 M$ al día: comprar o vender 18 $ no mueve el precio. En el último año, 1 de cada 20 semanas
  cayó más de un 16 %.
SUI: … Aviso: más de la mitad de la subida vino de un solo día (14-oct, +21 %).

QUÉ HACE HOY LA CARTERA DE PRUEBA (100 $ ficticios)
Vende AVAX: bajó al puesto 15 (se vende a partir del 13). Resultado: +1,90 $ (+10,6 %).
Compra LINK por 18,00 $. Mantiene SOL, BTC, XRP y SUI.
Valor: 108,30 $ · Con BTC comprado el mismo día: 104,10 $ · Efectivo: 12,40 $.

¿ACERTÓ? (solo análisis emitidos en vivo)
Hace 7 días eligió SOL, AVAX y XRP: +6,2 %, +9,6 % y −1,4 %. BTC: +2,9 %.
De las últimas 30 nº 1, 17 ganaron a BTC en los 7 días siguientes. Muestra pequeña: no demuestra nada.

QUÉ HIZO ESTA REGLA EN EL PASADO (backtest con costes; pseudo-fuera de muestra*; actualizado el lunes 19)
                     Regla v1   BTC   BTC con filtro   10 más negociadas
 2018-2020  al año      …        …         …                …
 2021-2022  al año      …        …         …                …
 2023-hoy   al año      …        …         …                …
 Peor caída             …        …         …                …
Diferencia con BTC: … % al año (margen de error: … a …).
Frente a elegir al azar: mejor que el … % de 1.000 carteras al azar (hace falta ≥ 95 para decir que elegir aporta).
La nº 1 a 7 días ganó a BTC el … % de las veces (margen: …); las nº 2-5, el … %.
Si hubieras lanzado una prueba de 28 días cualquier lunes: perdías dinero el … % de las veces.
Versiones probadas: 3 (la regla y 2 variantes informativas). * Quien eligió la regla ya conocía esos años.
Lo pasado no garantiza lo futuro.

RIESGOS DE HOY
- Si mañana BTC y ETH cayeran un 30 % y el resto un 50 %, perderías unos 41,40 $ (38 %).
- Las 5 suelen moverse con BTC: repartir entre altcoins no protege en un desplome.
- Pérdida de hoy: 1,2 % (sin compras al 20 %). Caída desde el máximo: 4 % (la cartera se para al 50 %).
- Bloqueadas hoy en vivo: ninguna.

SEMÁFORO «¿ES RENTABLE?»: GRIS (falta muestra)
Backtest: ÁMBAR · En vivo: día 9 de 28 · 37 de 100 operaciones del libro de señales · 4 de 30 de la cartera.
Con la ventaja que mostró el pasado, harían falta unos 1.400 días para poder afirmarlo.

Esto no es una recomendación de inversión. Ninguna regla sabe qué moneda subirá.
```

**Mercado desfavorable.** La respuesta corta pasa a ser: **«Hoy ninguna: quédate en USDT.** BTC cerró por debajo de su media de 100 días (X $ frente a Y $). La cartera vende lo que tenga y espera.» La tabla se titula «Las más fuertes (solo informativo: hoy la regla no compra)».

**Cabecera en rojo** si el exceso medio del backtest frente a BTC es ≤ 0: «En el pasado, esta regla lo hizo peor que comprar BTC y esperar.»

### 10.2 Cómo se calcula cada parte (plantillas fijas en `explain.py`; solo informativas, no deciden nada)

| Elemento | Cálculo |
|---|---|
| 7 días, 30 días y 90 días | `C(t)/C(t−L) − 1` |
| «más que N de cada 100» | Percentil de `m_{i,30}` en U(t) |
| Se mueve al día | `σ_i(t)` |
| Tendencia | Número de medias (20, 50 y 100) por encima de las que cierra |
| Frente a BTC (30 d) | `m_{i,30} − m_{BTC,30}`, en puntos |
| Peso del mejor día | `máx r_i` de 30 días ÷ `ln(1 + R30)` si R30 > 0. Si > 0,6: aviso de «subida de un solo día» |
| Semana mala | Percentil 5 de las rentabilidades a 7 días del último año. Con menos de 365 velas: «tiene menos de un año en Binance» |
| Riesgo | **Alto** si σ > 6 %, episodio < 180 días, volumen < 20 M$, peso del mejor día > 0,6 o (en vivo) diferencial > 0,2 %. **Bajo** si σ < 3 % y no se cumple ninguna. **Medio** en el resto |
| Escenario de desplome | BTC y ETH −30 %, el resto −50 %, sobre las posiciones actuales. Solo informativo |
| Amplitud | % de U(t) por encima de su SMA50. Contexto, no decide |

**Confianza en la regla:** hay una sola por informe y **nunca** una probabilidad por moneda.

| Situación | Confianza |
|---|---|
| Aún no hay backtest de la versión | SIN DATOS |
| Exceso medio del backtest frente a BTC ≤ 0 | MUY BAJA |
| Exceso medio > 0, pero la luz del backtest no está en verde | BAJA |
| Backtest en VERDE y en vivo no | MEDIA |
| Las dos luces en VERDE | ALTA (y aun así sin garantía) |

### 10.3 Reglas de redacción, con test

- **Expresiones prohibidas** en plantillas y textos generados: «garantiz», «seguro que», «va a subir», «sin riesgo», «asegur», «la más rentable» y «beneficio asegurado».
- Formato español: coma decimal, punto de miles, signo y unidad.
- La cabecera va en hora de Madrid (`zoneinfo("Europe/Madrid")`); por dentro, todo en UTC.
- `crypto coin PEPE` explica la situación de una moneda, por ejemplo: «Fuera del universo: poco volumen, mediana de 1,2 M $ al día (mínimo 5 M $)», o «puesto 23 de 84; puntos 61; tendencia 2 de 3».

---

## 11. Cartera de prueba en vivo y «modo prueba»

- **Cartera continua** (`kind='continua'`):
  - empieza sola al registrar la versión y **nunca se reinicia**;
  - es la evidencia oficial de la luz en vivo;
  - lleva su cartera de 100 $ y su libro de señales.
- **Modo prueba** (`kind='prueba'`), el que lanzas tú, desde la consola o desde la interfaz de la Fase 6:
  - duración a elegir: 7, 14, **28**, 56, 90 días o la que quieras;
  - su propia cartera de 100 $, como mucho una activa a la vez, en paralelo con la continua;
  - al terminar se valora al bid menos la comisión y se **congela el veredicto** (JSONB), con su percentil dentro de los arranques del mismo plazo (curva C);
  - **con menos de 28 días, el semáforo de la prueba no puede pasar de gris,** y la pantalla lo dice antes de empezar.
- **Calendario (UTC):**

| Hora | Paso | Si falla |
|---|---|---|
| 00:10 | `sync` (`exchangeInfo` y velas nuevas) | Reintento cada 10 minutos hasta las 02:00; después, día `sin_datos`: sin operaciones, salvo vender pares que ya no están en `TRADING` |
| 00:12 | `analyze`: U(t), ranking y régimen → `crypto_analyses` (`computed_live = true`, inmutable) | — |
| 00:12 | `paper step` de cada prueba activa, con un `bookTicker` nuevo antes de cada orden | Sin precio: no hay orden y se anota el motivo |
| 00:14 | Informe guardado y latido en `service_heartbeats('crypto_daily')` | Sin latido durante más de 26 h: alerta |
| Lunes 00:40 | Backtest de la versión registrada con los datos ampliados (misma configuración: K no sube) | El informe muestra el anterior con su fecha |

  En Madrid son las 02:10 en verano y las 01:10 en invierno.
- **Idempotencia:**
  - `idempotency_key = sha256(run_id, book, day, symbol, side, purpose, strategy_version)`, con restricción UNIQUE;
  - las órdenes `pending` se recuperan al arrancar; si el día ya pasó, se marcan `failed`;
  - bloqueo `pg_advisory_lock` con un identificador propio (`CRYPTO_LOCK_ID`), como `RunLock`.
- **Días perdidos** (máquina apagada): **no se rellenan** con precios pasados. Quedan como `perdido` y cuentan para la disponibilidad. El análisis que falte se recalcula marcado «con retraso» (`computed_live = false`) y «¿Acertó?» no lo cuenta.
- **Fidelidad** (`crypto/fidelity.py`):
  - cada día se vuelve a pasar el motor del backtest sobre los días en vivo, con las mismas velas, el mismo estado inicial y los días `sin_datos`;
  - **las decisiones** (símbolo, lado y motivo) deben coincidir al 100 %;
  - las diferencias de tamaño por los precios de ejecución se explican con la brecha de ejecución, que se informa aparte;
  - una diferencia en las decisiones sin explicar es un **fallo de software** (ROJO) hasta que se anota con `crypto paper explain --day D --note "…"`.
- **Cuadre de caja:** `efectivo = inicial − Σ(compras + comisión) + Σ(ventas − comisión)`, con `CHECK (cash_usd >= 0)`.

---

## 12. Base de datos: una migración **aditiva** (no se recrea la base de datos)

**`market_symbols`** (existe). Columnas nuevas:
- `status_changed_at` timestamptz;
- `final_sync_at` timestamptz;
- `history_requested_from` timestamptz;
- `listing_source` varchar(10) por defecto `'api'`;
- `tick_size`, `step_size` y `min_notional` (`AMOUNT`, admiten nulo);
- `filters_updated_at` timestamptz.

El estado admite el valor `DESAPARECIDO`. `market_bars` no cambia.

| Tabla nueva | Columnas | Claves y restricciones |
|---|---|---|
| `crypto_cost_snapshots` | `taken_at`, `symbol`, `bid`, `ask`, `bid_qty`, `ask_qty`, `quote_volume_24h` | PK(`taken_at`, `symbol`) |
| `crypto_backtest_runs` | `id`, `created_at`, `strategy_version`, `config_hash`, `config` JSONB, `role` (principal, variante, estres, adhoc), `counts_for_k` bool, `period_from`, `period_to`, `data_hash`, `cost_table_hash`, `code_commit`, `seed`, `n_days`, `sr_daily`, `skew`, `kurt`, `metrics` JSONB, `report_md` TEXT | Índice (`strategy_version`, `config_hash`) |
| `crypto_analyses` | `id`, `day` DATE, `strategy_version`, `params_hash`, `data_until`, `computed_live` bool, `regime` JSONB (cierre de BTC, SMA100, favorable), `universe_size`, `status` (ok, datos_incompletos), `confidence`, `code_commit`, `report_md`, `report_json` JSONB, `created_at` | UNIQUE(`day`, `strategy_version`). Si `computed_live`, es inmutable (no se reescribe) |
| `crypto_analysis_rows` | `analysis_id`, `symbol`, `included`, `exclusion_reason`, `liquidity_rank`, `median_qv_30d`, `rank`, `score`, `components` JSONB (a_L, p_L, m_L, σ, SMA, T), `ret_7d`, `ret_30d`, `ret_90d`, `vol_daily`, `best_day_share`, `p5_week`, `risk_level`, `entry_eligible`, `live_block_reason`, `spread_pct`, `warnings` JSONB | PK(`analysis_id`, `symbol`) |
| `crypto_paper_runs` | `id`, `kind` (continua, prueba), `requested_days`, `start_day`, `end_day`, `status` (RUNNING, PAUSED, HALTED_DAY, HALTED_RISK, FINISHED, STOPPED), `status_reason`, `strategy_version`, `params_hash`, `limits` JSONB, `initial_usd`, `created_by` (cli, gui), `verdict` JSONB, `created_at`, `finished_at` | Índice único parcial: como mucho una activa por `kind` |
| `crypto_paper_books` | `run_id`, `book` (portfolio, signals), `initial_usd`, `cash_usd` (nulo en signals), `peak_equity_usd`, `prev_equity_usd`, `last_day` | PK(`run_id`, `book`); `CHECK cash_usd >= 0` |
| `crypto_paper_orders` | `id`, `run_id`, `book`, `day`, `idempotency_key`, `symbol`, `side`, `purpose` (entrada, regimen, fuera_universo, stop, tendencia, puesto, horizonte_7d, retirada, fin_prueba), `requested_usd`, `status` (pending, filled, rejected, failed), `reason`, `decision` JSONB, `qty`, `price`, `fee_usd`, `bid`, `ask`, `ask_qty`, `ref_open_price`, `exec_gap_pct`, `filled_at` | UNIQUE(`idempotency_key`) |
| `crypto_paper_positions` | `id`, `run_id`, `book`, `symbol`, `status` (open, closed), `qty`, `entry_price`, `cost_usd`, `opened_day`, `closed_day`, `proceeds_usd`, `realized_pnl_usd`, `exit_purpose`, `btc_entry_price`, `btc_exit_price`, `last_value_usd`, `unsellable` | Único parcial (`run_id`, `book`, `symbol`) si está abierta |
| `crypto_paper_days` | `run_id`, `day`, `status` (ok, perdido, sin_datos), `analysis_id`, `started_at`, `finished_at`, `fidelity_ok`, `notes` | PK(`run_id`, `day`) |
| `crypto_paper_snapshots` | `run_id`, `book`, `day`, `equity_usd` (al cierre), `worst_equity_usd` (W_t), `bid_equity_usd`, `cash_usd`, `positions_value_usd`, `open_positions`, `btc_benchmark_usd` | PK(`run_id`, `book`, `day`) |
| `crypto_paper_events` | `id`, `run_id`, `at`, `kind` (halt, resume, delist, perdido, fidelidad, explicacion…), `details` JSONB | — |

Se reutiliza `service_heartbeats`. `test_migrations` comprueba que la migración sube, baja y coincide con los modelos.

---

## 13. Módulos y ficheros

```
src/copybot/ratelimit.py           RateLimiter (sale de solana/rpc.py)
src/copybot/crypto/binance.py      + BinanceError.code, filtros en SymbolInfo, bid_qty/ask_qty
src/copybot/crypto/sync.py         + S1-S4, relleno hacia atrás, DESAPARECIDO, filtros
src/copybot/crypto/archive.py      archivo data.binance.vision (opcional)
src/copybot/crypto/panel.py        + trades, episode_start(i), load_panel(window_days=400 | None), arrays
src/copybot/crypto/universe.py     U(t) (ventana 100, edad por episodio, trades); live_entry_blocks()
src/copybot/crypto/signals.py      sin cambios de fórmula
src/copybot/crypto/features.py     precálculo por serie para el backtest (igual que universe/signals)
src/copybot/crypto/renames.py      tabla manual de renombres con enlace al anuncio
src/copybot/crypto/costs.py        tramos, cost_per_side(), round_qty(step), min_notional_ok(), calibrate()
src/copybot/crypto/strategy.py     STRATEGY_V1, VARIANTS, params_hash(), regime(), decide() (puro)
src/copybot/crypto/engine.py       bucle diario + BacktestExecutor; modos porcentaje/dólares; estrés;
                                   libro de señales; retiradas; arranques cada lunes
src/copybot/crypto/benchmarks.py   USDT, BTC, BTC con filtro, 90/10, cesta de 10, nulo (semillas)
src/copybot/crypto/registry.py     crypto_backtest_runs, K, comprobación del preregistro
src/copybot/crypto/quality.py      cobertura, huecos, saltos, supervivencia, |U| por año
src/copybot/crypto/paper.py        almacén, PaperExecutor (bookTicker), pruebas, recuperación, bloqueo
src/copybot/crypto/fidelity.py     réplica del backtest sobre los días en vivo
src/copybot/crypto/semaforo.py     evaluate(backtest, run) -> Semaforo (puro)
src/copybot/crypto/explain.py      plantillas de motivos, avisos y riesgo (puro)
src/copybot/crypto/report.py       informe diario (corto y completo), backtest, prueba; Markdown y JSON
src/copybot/crypto/daily.py        sync → analyze → paper step → informe; reintentos; latido; run
src/copybot/crypto_cli.py          órdenes del apartado 14
src/copybot/backtest/stats.py      + log_excess, stationary_bootstrap_ci, cluster_bootstrap_ci, psr,
                                   sr0, deflated_sharpe, min_trl, spearman
src/copybot/sim/risk.py            open_mints → open_assets (compatible)
src/copybot/config.py              + CryptoSettings (COPYBOT_CRYPTO_*): solo de operación
                                   (quote=USDT, run_at=00:10, retry_until=02:00, default_test_days=28);
                                   ningún parámetro de la regla
docs/cripto/diseno.md              este documento
docs/cripto/preregistro-v1.md      STRATEGY_V1, variantes, costes, periodos, semáforo y hash
```

---

## 14. Órdenes (CLI)

```
copybot crypto sync [--since 2017-07-01]          [RED]  pares, filtros, velas; relleno hacia atrás si --since es anterior
copybot crypto archive                            [RED opcional: data.binance.vision] pares desaparecidos
copybot crypto quality                            [SIN RED] calidad de datos (permitido antes del preregistro)
copybot crypto costs calibrate | show             [RED] / [SIN RED]
copybot crypto prereg check                       [SIN RED] el hash del documento coincide con el del código
copybot crypto backtest [--role principal|variantes|estres|todo] [--seed 7] [--out f.md]
                        [--adhoc clave=valor …]   [SIN RED] siempre registra; --adhoc sube K y marca «NO CUENTA»
copybot crypto analyze [--day AAAA-MM-DD] [--full] [--out f.md]
                                                  [SIN RED] días pasados (computed_live=false); hoy con
                                                  bloqueos en vivo: [RED]
copybot crypto today [--full]                     [SIN RED] último análisis guardado
copybot crypto coin SÍMBOLO [--day AAAA-MM-DD]    [SIN RED] por qué está o no, y en qué puesto
copybot crypto history [--days 30] [--all]        [SIN RED] «¿Acertó?» (solo lo emitido en vivo; --all
                                                  incluye lo recalculado)
copybot crypto paper start [--days N]             [SIN RED] modo prueba (la continua arranca sola)
copybot crypto paper step                         [RED] paso de hoy (idempotente)
copybot crypto paper status | report | pause | resume | stop | explain --day D --note "…"   [SIN RED]
copybot crypto semaforo [--json]                  [SIN RED]
copybot crypto daily                              [RED] una vez: sync → analyze → paper step → informe
copybot crypto run                                [RED] servicio: daily a las 00:10 UTC + backtest los lunes
```

---

## 15. Plan de tests

Todos **[SIN RED]** salvo los `@pytest.mark.live`. Los que llevan `@pytest.mark.postgres` necesitan una base de datos de test.

| Área | Tests |
|---|---|
| Cliente y sincronización (`respx`) | S1 (retirada entre dos descargas: están las velas de los días 2-4); S2 (par ausente → `DESAPARECIDO`, el -1121 no aborta y los demás se descargan); S3 (relleno hacia atrás sin duplicados); parseo de los filtros (incluido `MARKET_LOT_SIZE` con step 0); `bidQty`/`askQty`; `BinanceError.code`; descarte de velas abiertas; paginación de 1.000 en 1.000 |
| Archivo (`respx`) | Paginación con `NextMarker`; CHECKSUM incorrecto → rechazado; tiempos en µs; cabecera; zip con 2 miembros o > 2 MB → rechazado |
| Panel | Carga de `trades`; un hueco de > 7 días abre un episodio; `trades = 0` cuenta como hueco; huecos globales |
| Universo | Un caso a mano por filtro; JUP y SYRUP no son apalancados y BTCUP sí; estable nueva inventada detectada por σ; **el estado actual no filtra el pasado** (un par en `BREAK` hoy está en U en 2021); antigüedad por episodio; ventana de 100 días |
| Señales | 3 monedas con cierres conocidos: `a_L`, percentiles con empates, `S` y orden determinista; `features.py` coincide con `universe()`/`ranked_day()` en 200 fechas |
| `decide()` | El puesto 12 se mantiene y el 13 se vende; solo se compra en puestos ≤ 5 con T ≥ ⅔; T = ⅓ → venta; stop −25 %; mercado desfavorable → vende todo y no compra; parada diaria → sin compras pero con ventas; `HALTED_RISK` igual; sin recompra el mismo día; tamaños 100 → 18, 140 → 25, 60 → 10, 30 → no compra; nunca más de 8 posiciones ni efectivo por debajo de la reserva (10.000 casos aleatorios con semilla fija); `RiskLimits` endurecidos → N_ef |
| Regla congelada | `test_strategy_v1_is_frozen` (hash esperado); `test_prereg_hash_matches` (documento = código); `CryptoSettings` no acepta parámetros de la regla |
| Costes | Tramos; compraventa completa exacta; `stepSize`; `minNotional`; libro fino → +0,5 % |
| Motor | **Canario de futuro:** 50 fechas con semilla fija, multiplicando las velas posteriores a t por factores U(0,2-5) → decisiones y patrimonio idénticos hasta t. **Control negativo:** panel sintético sin señal → IC medio dentro de ±2 errores y percentil frente al nulo entre 5 y 95. **Control positivo:** monedas con deriva persistente → IC > 0. **Camino único:** `analyze` con la base de datos recortada en t = backtest en t. Retirada → último cierre −10 %; renombre sin penalización; hueco de ≤ 7 días → no vendible y valorada a ×0,90 |
| Referencias y nulo | BTC comprar y mantener con costes exactos; BTC con filtro; cesta de 10 con retiradas; nulo determinista por semilla y con duraciones emparejadas |
| Estadística | Bootstrap estacionario determinista; serie constante → intervalo degenerado; `Σ e_t = ln(V_A/V_BTC)`; PSR; DSR con K = 1 igual a PSR(0); factor con K = 3 ≈ 0,853 y con K = 8 ≈ 1,459; MinTRL; Spearman; bootstrap por grupos; misma semilla → mismo informe (huella) |
| Semáforo | Un test por color en cada luz; < 28 días → GRIS; revisión solo cada 28 días con z = 2,89 y como mucho 13; ROJO tiene prioridad; mensaje «elegir moneda no aporta» |
| Informe | `test_report_never_promises` (expresiones prohibidas en plantillas y en un informe renderizado); *snapshot* del informe con datos de ejemplo; «Hoy ninguna: quédate en USDT» con el mercado desfavorable; cabecera en rojo; formato de números en español |
| Cartera (PostgreSQL) | Idempotencia; `kill` a mitad del paso sin duplicados; día perdido no rellenado; `sin_datos`; retirada → no vendible → cancelación a los 7 días; cuadre de caja; `CHECK` de efectivo ≥ 0; fin de prueba con veredicto congelado; una continua y una prueba a la vez; fidelidad (una decisión distinta → ROJO; explicada → no) |
| Guarda | Añadir `listenKey` y `userDataStream`; comprobar que siguen permitidos `/api/v3/klines`, `exchangeInfo`, `ticker/24hr`, `ticker/bookTicker`, `data.binance.vision/data/spot/monthly/klines/...` y la URL del listado S3 |
| En vivo (`@pytest.mark.live`) **[RED]** | `exchangeInfo`, `klines` de BTCUSDT, `klines` de un par en `BREAK`, `bookTicker`, y un `crypto daily` completo contra una base de datos de test. Las respuestas se graban como fixtures en `tests/fixtures/binance/` |

---

## 16. Orden de implementación

1. **[SIN RED] Base.**
   - `ratelimit.py`; `BinanceError.code`; filtros y cantidades del `bookTicker`;
   - correcciones S1-S4; migración aditiva; guarda; `.env.example` con los topes vigentes.
2. **[SIN RED] Datos en memoria.** Panel con `trades` y episodios; cambios en el universo; `features.py`; tests de universo y señales (hoy no existen).
3. **[SIN RED] Regla y costes.** `strategy.py` (`STRATEGY_V1`, `decide()`), `costs.py` y sus tests.
4. **[SIN RED] Backtest y estadística.** Motor, referencias, nulo, registro, calidad, estadística e informe del backtest, **probados solo con paneles sintéticos** (canario y controles positivo y negativo).
5. **[RED] Datos reales.**
   - `crypto sync --since 2017-07-01` (relleno hacia atrás);
   - fixtures reales grabadas;
   - verificar que `klines` sirve los pares en `BREAK`, los pesos y los `minNotional`;
   - calibración de costes y ajuste de los tramos;
   - `crypto quality`.
6. **[RED opcional] Archivo.** `crypto archive`, si se permiten `data.binance.vision` y `s3-ap-northeast-1.amazonaws.com`.
7. **[SIN RED] Preregistro.**
   - `docs/cripto/preregistro-v1.md` con el hash;
   - **tu aprobación** de los parámetros y de las decisiones del apartado 18;
   - commit.
   - **Solo después**, el primer `crypto backtest --role todo`, que va al informe **tal como salga**, aunque pierda frente a BTC.
8. **[SIN RED] Informe diario.** `analyze`, `today`, `coin`, `history`, `explain` y `report`, con *snapshot* y palabras prohibidas.
9. **[SIN RED con `respx`; RED para el smoke test] Cartera en vivo.** `paper`, `fidelity`, `semaforo`, `daily` y `run`; tests en PostgreSQL y prueba de `kill`.
10. **[RED] Arranque.** Primer `crypto daily` real; arranque de la cartera continua; informe de cierre `docs/informes/cripto-v1.md` con los comandos, el commit, la salida de los tests y los problemas abiertos.
11. **Después, en un paso aparte.** Retirar el código de memecoins:
    - etiqueta `memecoins-final`;
    - borrar `solana/`, `ingest/`, `analysis/`, el resto de `sim/` y `backtest/`, `traders_cli.py`, `sim_cli.py`, `backtest_cli.py`, `monitor_runner.py`, `budget.py` y `db/repo.py`, con sus tests;
    - limpiar `config.py`; actualizar README y `pyproject` (`description`);
    - las tablas antiguas se retiran con otra migración aditiva, **nunca recreando la base de datos**.
    
    La interfaz es de la Fase 6. API prevista: `/api/hoy`, `/api/analisis/{dia}`, `/api/recomendaciones`, `/api/semaforo`, `/api/pruebas` (crear, pausar, reanudar, parar y curva) y `/api/backtest/ultimo`.
    
    La pantalla «¿Y si…?» **solo muestra** las 2 variantes registradas con su K. No permite probar parámetros de la regla ni ofrece «usar esta variante».

**No se construye ahora:** órdenes dentro del día, velas de 1 h, varias reglas a la vez, aprendizaje automático, predicción de precios, profundidad del libro de órdenes, futuros o apalancamiento, ni Telegram.

---

## 17. Expectativas realistas (para leerlas antes de ver resultados)

- **Potencia estadística.**
  - Para que el intervalo excluya el cero con T días hace falta un IR anual de al menos `1,96·√(365/T)`: **≈ 7 en 28 días**, 1,96 en un año, 1,13 en tres años y 0,74 en siete.
  - Con las revisiones secuenciales (z = 2,89), la luz en vivo necesita un IR ≥ 2,9 en un año.
  - **Es muy probable que el semáforo no se ponga verde en el primer año aunque la regla funcione.** Es honestidad estadística, no un fallo.
- **Ritmo de muestras.**
  - El libro de señales llega a 100 operaciones cerradas hacia el día 28, solapadas.
  - La cartera, con 5 posiciones e histéresis, probablemente llegue a 30 operaciones en **2-6 meses**, según cuánto tiempo pase el mercado en régimen favorable.
- **Resultado más probable:**
  - una rentabilidad parecida a la de BTC, o menor en los mercados alcistas (la cartera está invertida un 90 % como mucho y pasa temporadas en USDT);
  - menos caída en 2022;
  - poca o ninguna aportación demostrable de la elección de moneda.
  
  Si es así, el informe dirá que la versión simple (BTC con filtro) es mejor.
- **Riesgos de la propia regla:**
  - los backtests de momentum en cripto suelen depender de pocas operaciones de 2020-2021 (de ahí la prueba sin las 3 mejores monedas);
  - las altcoins pueden caer un 70-90 %;
  - en un desplome las correlaciones se acercan a 1;
  - los latigazos del filtro de mercado se pagan en costes.
- **Ninguna versión de esta regla predice qué moneda subirá.** Solo se mide si ordenar por ella ha servido.

---

## 18. Decisiones que te tocan antes del primer backtest, y comprobaciones pendientes

**Decisiones:**
1. **Aprobar la regla v1 tal cual** (apartado 5.2) y sus 2 variantes. Después de ver resultados ya no se puede cambiar sin crear una versión nueva que empieza de cero.
2. **Objetivo del semáforo:** exceso frente a **BTC comprar y mantener** (lo acordado). Esta referencia castiga a una estrategia que pasa tiempo en USDT; «BTC con filtro» y «90 % BTC» se muestran como diagnóstico. ¿Lo mantienes?
3. **Caída máxima para el verde:** **< 25 %** (criterio original de la Fase 1, propuesto) o < 50 % (tu límite de la simulación). Con la propuesta, entre el 25 y el 50 % queda en ámbar.
4. **Verde en vivo:** el intervalo excluye el cero **con datos en vivo** y revisiones secuenciales (propuesto; casi imposible en un año), o bien «backtest significativo y en vivo sin contradecirlo» (más fácil y más débil). Tiene que decidirse ahora.
5. **Parada diaria del 20 %:** reanudación automática en la decisión siguiente (propuesta, para un bot desatendido) o manual (como en la Fase 4).
6. **Caída del 50 %:** dejar de comprar y que las ventas sigan sus reglas (propuesta) o venderlo todo y parar.
7. **Red:** permitir `data-api.binance.vision` y `api.binance.com`, y si quieres corregir del todo el sesgo de supervivencia, también `data.binance.vision` y `s3-ap-northeast-1.amazonaws.com`.
8. **Hora del informe:** 02:10 en verano y 01:10 en invierno, hora de Madrid. ¿Te va bien?

**Comprobaciones pendientes [RED]:**
- Que `/api/v3/klines` devuelve la historia de los pares en `BREAK` (el diseño de riesgo dice que no; el código y los demás diseños suponen que sí).
- Que el archivo tiene los pares totalmente retirados (LUNA clásica, FTT, tokens apalancados…).
- Los pesos actuales de los endpoints y los `minNotional`.
- Los renombres de la tabla.
- Las citas de la literatura del apartado 5.4.

**Para un futuro con dinero real (Fase 9, fuera de este alcance):**
- **MiCA:** los pares en USDT pueden no estar disponibles para residentes del Espacio Económico Europeo. Habría que repetir el análisis con pares USDC o EUR, que tienen otra liquidez y otros listados, así que la prueba con USDT podría no representar el modo real.
- **Fiscalidad:** en España, cada permuta entre criptomonedas tributa en el IRPF. Consúltalo con un asesor.
- **Coste fijo:** con 100 $, la infraestructura puede costar más de lo que se gane (apartado 12 de la Fase 1).
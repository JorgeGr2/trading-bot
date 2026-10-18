# Informe de la Fase 3: descubrimiento y puntuación de traders

- **Fecha:** 18 de octubre de 2026
- **Estado:** aprobada por ti el 18-oct a falta de ampliar la búsqueda de traders (leaderboard de FOMO y traders que operan poco). Este informe ya incluye la ampliación; la parte de la leaderboard espera las direcciones (apartado 10).
- **Rama:** `claude/trusting-fermat-jangmb`, construida sobre `claude/upbeat-noether-027ixl` (Fase 2).

## 1. Resumen

- **Se han analizado 221 traders reales de FOMO** con su historial completo:
  - las **71 más activas** de una muestra de 6.259 traders;
  - **150 elegidas al azar entre las que operan poco** (0,7-15 movimientos al día).

  En total son más de 40.000 operaciones cerradas en 30 días.
- **El resultado es desfavorable para el copy-trading.** Contando también las posiciones que dejan sin vender:
  - la rentabilidad mediana por operación es **−13,6 %** en las muy activas y **−13,8 %** en las poco activas;
  - en conjunto pierden el 8,6 % de lo que compran (muy activas) y el 7,2 % (poco activas);
  - solo 17 de 221 ganan dinero en dólares.
- **Solo un trader supera los filtros con ventaja positiva** después del coste estimado de copiar: `A9aqAHUX…`, del grupo poco activo. Tiene 34 operaciones, un acierto del 68 % y +20,6 % de media por operación. Pero la muestra es pequeña, una cuarta parte de sus operaciones no se pueden valorar (pagadas en SOL), y entre 221 candidatos uno así puede salir por azar. **Es un candidato a vigilar, no una ventaja demostrada.**
- **El cálculo es fiable y sus errores están corregidos.** La atribución se ha verificado con 1.025 compraventas reales, sin errores (cota inferior del 99,6 %). Al revisar los resultados encontré y corregí un sesgo serio: las posiciones abandonadas no contaban. Ahora se valoran a lo que valdrían si se vendieran hoy (apartado 5.2).
- **Coste:** 89.142 créditos de Helius de 1 millón al mes (cifra real de su panel; apartado 7.6), y menos de una hora de ejecución en total.

## 2. Verificación de la atribución con Helius

La Fase 2 dejó la precisión con una cota inferior del 91,6 % (42 compraventas). Esta fase la repite con más de 1.000 compraventas, como se acordó, antes de puntuar a nadie.

### 2.1 Método

Cada transacción se clasifica con el método del sistema (**A**, cambios de saldo por propietario) y con un control independiente (**B**, reconstrucción a partir de las instrucciones de transferencia). Novedades respecto a la Fase 2:

- La muestra son **ventanas de 100 transacciones consecutivas del relayer**, que empiezan en instantes al azar de los últimos 7 días (con semilla, reproducible) y se descargan con `getTransactionsForAddress` de Helius.
- Cuando A y B no coinciden, el script consulta on-chain la **comisión de transferencia (Token-2022)** de cada token y recalcula B descontándola de cada transferencia. Una discrepancia solo se da por explicada si así coincide **al último decimal**.

Script: `scripts/spike_fomo_verification.py` (opciones `--helius-windows` y `--explain-transfer-fees`).

### 2.2 Resultados

| Muestra | Transacciones | Traders | Compraventas de confianza alta | Coinciden A = B | Explicadas por la comisión on-chain | Correctas |
|---|---|---|---|---|---|---|
| 1 (semilla 3) | 600 | 468 | 500 | 479 | 21 | **500 / 500** |
| 2 (semilla 4, tomada **después** de las correcciones) | 600 | 508 | 525 | 483 | 42 | **525 / 525** |
| **Total** | 1.200 | — | **1.025** | 962 | 63 | **1.025 / 1.025** |

- **Precisión de la confianza alta: 100 % en la muestra, con una cota inferior de Wilson al 95 % del 99,6 %** (el criterio era ≥ 95 %).
- **Todas las discrepancias** son tokens Token-2022 que cobran un **1 % o un 3 % en cada transferencia**. A refleja lo que el usuario recibe de verdad; B, lo que envían los pools. Son entre el 4 % y el 8 % de las compraventas: un coste real que la simulación tendrá que tener en cuenta.
- 174 transacciones no son compraventas de memecoins (sobre todo depósitos de USDC y 20 conversiones SOL↔USDC o USDT↔USDC, revisadas una a una).
- Una venta usa un programa de swap no identificado (`proVF4pM…`, instrucción `SwapTobV3`). Queda en confianza media y no se usa como señal.

### 2.3 Fallos del clasificador encontrados y corregidos

Los tres eran errores **prudentes**: una compra real quedaba como «ambigua» y se perdía la señal. Ninguno daba señales falsas.

| Caso real | Qué pasaba | Corrección | Afectaba a |
|---|---|---|---|
| Ruta USDC → token intermedio → memecoin que deja un resto del intermedio (0,7-0,9 %) | Cambian dos tokens: «ambigua» | Si un token **entra y sale** de la wallet en la misma transacción y queda como mucho un 2 % de lo que pasó por ella, es un resto de ruta | ~1 % de las operaciones de un trader activo |
| La instrucción `create` nombra a la wallet como pagadora de la cuenta de comisiones de **otra** wallet | Se le atribuía esa renta: «ambigua» | Solo se descuenta la renta de las cuentas **propias** | Casos sueltos |
| FOMO cobra 2.039.280 lamports por la cuenta nueva y devuelve 1.488.440 al cerrar una cuenta temporal: el usuario paga además −0,00055084 SOL | USDC y SOL a la vez: «ambigua» | Un movimiento de SOL de hasta 0,003 SOL junto a un pago en USDC/USDT es coste de cuentas, no precio | **1.795 compraventas** de la primera prueba en vivo |

La tercera corrección se verificó con **120 transacciones reales** que antes eran ambiguas. Se recuperan 91 compraventas y **todas** coinciden con el control B. Las 27 que siguen ambiguas son operaciones realmente mixtas o ventas de varios tokens a la vez. **Con el clasificador final, solo el 0,11 % de los 230.307 eventos importados queda como ambiguo** (antes, el 1 %).

Hay tests con cada una de estas transacciones reales (`tests/fixtures/tx/`).

## 3. Lo medido de Helius y Jupiter

| Hecho | Resultado |
|---|---|
| `getTransactionsForAddress` con la clave gratuita | **Funciona**, también con transacciones completas |
| Transacciones por petición | **Hasta 1.000** (algunas páginas de la documentación dicen 100) |
| Paginación | El token de paginación llega también en la última página; la siguiente vendría vacía. El cliente para cuando la página no está llena, para no pagarla |
| Filtros | Fecha, solo las que tuvieron éxito y `tokenAccounts: "balanceChanged"`, que añade lo recibido en sus cuentas de token sin que la wallet firme |
| Coste | Contrastado con tu panel (89.142 créditos al final, frente a ~92.000 con este modelo): **10 créditos por cada 100 transacciones devueltas** (una página llena cuesta 100), 10 por consulta de solo firmas, 1 por cada `getSignaturesForAddress` y nada por las peticiones rechazadas por exceso de velocidad |
| Fecha de creación de un token | Su primera transacción, con una consulta de solo firmas (0,4 s) |
| Jupiter Price API (gratis, sin clave) | Precio y **liquidez** actuales de cada token. Limita las peticiones desde la IP compartida del entorno; el cliente espera y reintenta |
| Precios históricos (Binance, Pyth, CoinGecko) | **Bloqueados** por la red del entorno |
| Ritmo del relayer | 1.000 transacciones cubren unos 107 s (~9 por segundo), con unos 640 traders distintos |

## 4. Qué se ha construido

| Fichero | Función |
|---|---|
| `src/copybot/solana/rpc.py` | `getTransactionsForAddress` con paginación y filtros; créditos por método y modo; **control de presupuesto antes de cada petición** |
| `src/copybot/budget.py` | Presupuesto mensual del análisis (por defecto el 60 % del plan Free: 600.000 créditos). Al llegar al límite para antes de pedir nada; el seguimiento en vivo no se ve afectado |
| `src/copybot/ingest/discovery.py` | Muestreo del relayer; **filtro de actividad** (cuánto opera una wallet con una consulta barata); fechas de creación de tokens |
| `src/copybot/ingest/history.py` | Importación del historial de cada wallet: tope de páginas, incremental e idempotente |
| `src/copybot/ingest/prices.py` | Precio y liquidez actuales de tokens (Jupiter), guardados con su hora |
| `src/copybot/analysis/positions.py` | Posiciones con lotes FIFO y cada operación completa (*round trip*) |
| `src/copybot/analysis/metrics.py` | Métricas por ventana (7, 30 o 90 días) |
| `src/copybot/analysis/scoring.py` | Filtros, puntuación explicada, avisos con su confianza y `params_hash` |
| `src/copybot/analysis/service.py` | Une base de datos, precios y analizador; guarda *snapshots* y puntuaciones |
| `src/copybot/traders_cli.py` | Órdenes `discover` (`sample`, `candidates`, `screen`, `add`, `import`, `run`), `analyze` y `traders` |
| `migrations/versions/` | Dos migraciones nuevas (apartado 4.2) |

### 4.1 Reglas que garantiza

1. **Nada posterior a `as_of`.** Ni eventos ni precios: el analizador descarta todo lo posterior a la fecha del análisis, aunque se le pase por error, y rechaza fechas futuras. Un test lo comprueba con 20 historiales aleatorios: añadir, cambiar o desordenar eventos posteriores no altera ni un decimal.
2. **Resultados inmutables y versionados.** Cada cálculo se guarda con su `as_of` y un `params_hash` (versión y parámetros). Repetirlo no lo sobrescribe, y resultados de versiones distintas nunca se mezclan.
3. **Periodo cubierto honesto.** Una importación solo marca como completo lo que de verdad ha descargado, y solo al terminar. Si se interrumpe, no queda nada «cubierto» a medias y al repetirla no se duplica nada (probado también en vivo, apartado 7.5).
4. **El historial no guarda el JSON completo** de cada transacción (~30 KB cada una, y se puede volver a descargar). La base de datos impide que una fila en vivo se quede sin él.
5. **El gasto nunca supera el presupuesto**: se comprueba antes de cada petición.

### 4.2 Cambios en la base de datos

| Tabla | Contenido |
|---|---|
| `raw_transactions`, `wallet_events` | Nuevo origen `history`; posición en el bloque; JSON opcional **solo** en filas de historial (restricción `CHECK`) |
| `tokens`, `token_prices` | Fecha de creación de cada token; precio y liquidez observados, con su hora |
| `discovery_windows`, `discovery_observations` | Ventanas muestreadas y compraventas vistas en ellas |
| `candidate_wallets` | Candidatas, su origen (`relayer`, `leaderboard` o `manual`) y su actividad medida |
| `history_imports` | Periodo importado completo de cada wallet |
| `wallet_metric_snapshots`, `wallet_scores` | Métricas y puntuaciones versionadas |

Volver a la versión anterior **borra el historial importado** (se puede volver a importar) y conserva lo detectado en vivo; un test lo comprueba.

## 5. Cómo se calculan las métricas y la puntuación

### 5.1 Operaciones y PnL

- Una **operación** empieza con la primera compra de un token y termina cuando el trader ha vendido todo, o cuando le queda menos del 1 % de lo que llegó a tener (ese resto se da por perdido).
- Las ventas se asignan a las compras en orden (FIFO). Solo cuentan para el PnL las partes con coste y cobro conocidos en USDC o USDT.
- Lo vendido sin haberlo comprado en el periodo importado cuenta como **venta sin coste conocido**. Los tokens recibidos por transferencia son **tokens gratis**: si se venden, se avisa.

### 5.2 Posiciones abandonadas (corrección importante)

La primera versión solo contaba las operaciones **cerradas**. Al revisar los resultados vi que eso premia a quien vende sus aciertos y se queda las pérdidas sin vender. Ejemplo real: una wallet ganaba +32 % por operación cerrada, pero tenía **289 posiciones sin vender**; contándolas, queda en −2,6 %.

Ahora, una posición sin compras ni ventas desde hace más de 72 horas cuenta como **operación terminada**. Lo que queda se valora a **lo que se obtendría vendiéndolo en el momento del análisis**:

- se usa el precio y la liquidez de Jupiter;
- la venta se calcula contra un pool de producto constante: cuanto mayor es la posición frente al pool, menos se obtiene;
- si no hay precio, cuenta como cero.

La valoración importa porque varía mucho de un trader a otro. En la prueba, lo abandonado por unos traders conservaba solo el 7 % de su coste, y lo de otros, el 63 %. Contarlo todo a cero habría sido injusto con los segundos. En las 221 wallets, lo abandonado costó 72.743 $ y hoy valdría unos 16.632 $ al venderlo (23 %).

### 5.3 Por qué importa la rentabilidad *por operación*

Nosotros copiaríamos con un **tamaño fijo** (10 $). Lo que importa es cuánto gana de media **cada operación**, no el PnL en dólares del trader, que lo deciden sus apuestas más grandes. La puntuación usa la rentabilidad media por operación (incluidas las abandonadas), con dos precauciones:

- **Media robusta:** cada operación cuenta como mucho +200 %.
- **Estimación prudente:** media − 1 × error típico (con pocas operaciones, la estimación baja).

### 5.4 Filtros de exclusión

| Motivo | Umbral por defecto |
|---|---|
| El historial no cubre todo el periodo o no está al día | — |
| Muestra insuficiente | < 20 operaciones cerradas con coste conocido |
| No copiable: vende demasiado pronto | Mediana < 60 s desde la compra hasta su primera venta |
| Compra en el lanzamiento (*sniping*) | > 30 % de entradas en los primeros 60 s de vida del token (con ≥ 10 comprobadas) |
| Datos incompletos | > 25 % de lo vendido sin compra conocida |
| Frecuencia imposible de seguir | > 300 compraventas al día |

### 5.5 Puntuación

**Puntuación = 100 × ventaja × calidad**

- **Ventaja** (0 a 1): estimación prudente de la rentabilidad media por operación, **menos un coste estimado de copiar del 3 %** por operación completa. Ese coste es un supuesto, que se calibrará en la Fase 4. La ventaja vale 1 con un +10 % neto y 0 si es negativa. **Sin ventaja, la puntuación es 0**, por bien que salga lo demás.
- **Calidad** (0 a 1), media ponderada de:
  - **consistencia** (25 %): en positivo en las dos mitades del periodo, en los últimos 7 días y sin sus 3 mejores operaciones;
  - **copiable** (35 %): tiempo hasta la primera venta (vale 1 desde 10 minutos), compraventas al día y entradas en el lanzamiento;
  - **riesgo** (25 %): caída máxima del PnL frente al capital invertido, y pérdida en posiciones abandonadas;
  - **datos** (15 %): ventas sin coste conocido y eventos ambiguos, de confianza media o no pagados en USDC/USDT.

Todo es configurable con variables `COPYBOT_ANALYSIS_*` y `COPYBOT_SCORING_*`; por ejemplo, `COPYBOT_SCORING_WINDOW_DAYS=90` para medir a 90 días.

**Cambios de diseño durante la fase, siempre en la dirección más prudente** (de ahí las versiones 3 del analizador y 2 de la puntuación):

1. La primera fórmula sumaba los componentes y dio 36,84/100 a un trader que pierde por operación. Ahora es multiplicativa.
2. Las posiciones abandonadas no contaban (apartado 5.2).
3. La prueba «sin sus 3 mejores operaciones» se hacía en dólares; ahora se hace por operación, porque copiaríamos con un tamaño fijo.

Ningún umbral ni peso se ha ajustado para que algún trader salga mejor.

### 5.6 Avisos (con su nivel de confianza)

`alta_frecuencia`, `salidas_rapidas`, `compras_en_lanzamiento`, `sniping_sin_comprobar`, `vende_tokens_recibidos` (posible vínculo con el creador; confianza media, nunca se etiqueta a nadie como *insider*), `misma_ranura` (compra y venta en el mismo bloque, típico de bots), `beneficio_concentrado`, `posiciones_abandonadas`, `ventas_sin_compra`, `muestra_pequeña` y `operaciones_no_usd`.

## 6. Pruebas ejecutadas

```
$ uv run pytest
196 passed

$ uv run ruff check src tests scripts migrations
All checks passed!
```

Los tests que usan base de datos se ejecutan contra PostgreSQL 16 real.

| Área | Qué se comprueba |
|---|---|
| Métricas calculadas a mano | Un escenario de 13 eventos (FIFO, resto abandonado, tokens gratis, venta sin coste, posición abandonada, caída máxima, capital máximo, *sniping* y un evento posterior a `as_of`), con cada cifra razonada en el test; valor de venta contra un pool |
| Sin información del futuro | 20 historiales aleatorios; de extremo a extremo con historial llegado después; **precios observados después de `as_of` ignorados**; fechas futuras rechazadas |
| Puntuación | Una puntuación calculada a mano (49,23); cada filtro; sin ventaja la puntuación es 0; quien acumula posiciones abandonadas no puntúa por sus aciertos; avisos; validación de parámetros; `params_hash`; determinismo |
| Importación | Paginación, periodo cubierto, incremental, truncado, **corte a mitad sin duplicados al repetir**, eventos de dos wallets en la misma transacción |
| Descubrimiento | Ventanas reproducibles; candidatas sin contar dos veces la misma operación; filtro de actividad (incluido el máximo de la consulta); selección al azar dentro de una banda; alta manual que conserva el origen más específico |
| Precios | Lotes de 50 tokens, reintento ante el límite de peticiones, tokens sin cotización, solo se piden los precios no recientes |
| Presupuesto | Bloquea **antes** de enviar la petición; solo cuenta el mes en curso |
| Base de datos | La restricción del JSON se cumple de verdad (guarda `NULL` de SQL); las migraciones coinciden con los modelos y se pueden deshacer con datos |
| Clasificador | 11 transacciones reales como *fixtures* (4 nuevas) y casos sintéticos nuevos |

## 7. Prueba en vivo con datos reales

Todo con el código final y una base de datos vacía, con `as_of` = 18-oct-2026 18:54 UTC (ventana de 30 días).

### 7.1 Muestreo y traders muy activos

`copybot discover run --windows 12 --days 7 --seed 1018 --top 70 --token-lookups 5`

- 12 ventanas del relayer: 12.000 transacciones, 9.748 compraventas y **6.259 traders distintos**. De ellos, 616 aparecen en dos o más ventanas.
- Historial (37 días) de las **70 más recurrentes**: 10 quedan truncadas (más de 10.000 transacciones) y se excluyen si el corte deja fuera parte del periodo.

### 7.2 Traders que operan poco

- `copybot discover screen --limit 2000 --max-windows 1`: se midió la actividad de **2.000 wallets vistas una sola vez**, con una consulta barata cada una (6,5 minutos):

  | Movimientos al día | Wallets |
  |---|---|
  | < 0,7 | 124 |
  | **0,7 - 15** | **1.020** |
  | > 15 | 260 |
  | ≥ 27 (máximo de la consulta) | 596 |

- `copybot discover import --activity 0.7:15 --sample 150 --days 97`: historial de **150 elegidas al azar** dentro de la banda 0,7-15, con 97 días para poder medirlas también a 90 días (3 minutos, ninguna truncada).

### 7.3 Resultados

`copybot analyze --prices --token-lookups 5`. Se consultó el precio de 4.647 tokens con posición abierta.

| | Muy activas | Poco activas |
|---|---|---|
| Wallets / puntuables | 71 / 61 | 150 / 114 |
| Operaciones cerradas / abandonadas | 33.064 / 4.002 | 7.115 / 1.043 |
| Movimientos al día (mediana) | 52,9 | 3,6 |
| Acierto (mediana) | 27,9 % | 33,3 % |
| Rentabilidad por operación cerrada (mediana) | −11,0 % | −7,7 % |
| **Rentabilidad prudente por operación (mediana)** | **−13,6 %** | **−13,8 %** |
| Con rentabilidad prudente > 0 / > 3 % (coste de copiar) | 1 / 0 | 8 / 4 |
| Ganan en dólares (contando lo abandonado) | 2 | 15 |
| PnL realizado del grupo / sobre lo comprado | −293.903 $ / 3,93 M$ | −29.336 $ / 0,58 M$ |
| PnL contando lo abandonado | **−338.085 $ (−8,6 %)** | **−41.266 $ (−7,2 %)** |
| Lo abandonado: coste / valor de venta hoy | 59.620 $ / 15.439 $ | 13.123 $ / 1.193 $ |
| Entradas en el lanzamiento (≤ 60 s) / comprobadas | 46 / 8.989 | 14 / 3.584 |
| **Con puntuación > 0** | **0** | **1** |

A 90 días (`COPYBOT_SCORING_WINDOW_DAYS=90`), el grupo poco activo da lo mismo: mediana prudente −14,3 %, y la única wallet con puntos vuelve a ser la misma.

Conclusiones:

- **Copiar a los traders de FOMO, en general, pierde dinero**, operen mucho o poco. La hipótesis de la Fase 1 se confirma con datos.
- **Operar poco no es sinónimo de operar bien:** la mediana es igual de mala. Pero en ese grupo hay más casos positivos (8 de 150, frente a 1 de 71), así que es donde más merece la pena seguir buscando.
- **El *sniping* casi no existe en FOMO** (0,5 % de las entradas comprobadas), así que no es lo que explica los malos resultados.
- **Las posiciones abandonadas importan mucho:** valen hoy el 26 % (muy activas) y el 9 % (poco activas) de lo que costaron.

### 7.4 El único candidato: `A9aqAHUXrJL99gpQHbTTdk5wypMH1ivVa5MkyWwaJaR9`

| Medida | 30 días | 90 días |
|---|---|---|
| Puntuación | 54,96 | 18,96 |
| Operaciones cerradas | 34 | 41 |
| Rentabilidad prudente por operación | +20,6 % | +15,0 % |
| Sin sus 3 mejores operaciones | +8,2 % | — |
| Acierto | 68 % | — |
| Mediana hasta la primera venta | 4,3 min | 4,1 min |
| Compras en 30 días | 262 $ (unos 8 $ por operación) | — |

Puntos a favor:

- positivo en las dos mitades del periodo y en los últimos 7 días;
- sin posiciones abandonadas relevantes;
- 1 sola entrada en el lanzamiento de 21 comprobadas.

Reservas:

- **la muestra es pequeña** (34 operaciones);
- **43 de sus compraventas (una cuarta parte) se pagaron en SOL** y no se pueden valorar en dólares, así que solo vemos parte de su actividad;
- una operación aporta el 63 % de su ganancia en dólares;
- **entre 221 candidatos, uno así puede salir por azar.**

Además hay 3 wallets poco activas con más de un 3 % de media pero sin puntuación:

- dos tienen menos de 20 operaciones;
- otra (`8uNAuCQe…`, +4,0 % en 115 operaciones) tiene tanta dispersión que su estimación prudente no supera el coste.

### 7.5 Prueba de recuperación en vivo

Se cortó con `kill -9` la importación de una wallet a los 6 segundos:

- quedaron guardados 698 eventos y **ningún periodo marcado como cubierto**;
- al repetirla se añadieron 1.464 eventos (2.162 en total, todos distintos) y se registró el periodo completo.

### 7.6 Coste y tiempos

| Paso | Peticiones | Créditos (modelo del apartado 3) | Tiempo |
|---|---|---|---|
| Verificación y exploración | ~200 | ~600 | — |
| Primera prueba en vivo (luego repetida desde cero) | ~490 | ~34.000 | 15 min |
| Muestreo (12 ventanas de 1.000) | 12 | 1.200 | 20 s |
| Historiales (221 wallets, 368.000 transacciones, más actualizaciones) | 561 páginas | ~39.600 | 13 min |
| Fechas de creación de tokens | 1.438 | 14.380 | ~6 min |
| Filtro de actividad (2.000 wallets) | 2.000 | 2.000 | 6,5 min |
| Precios de Jupiter | ~95 | 0 | ~5 min |
| **Total estimado** | | **~92.000** | |
| **Total real (panel de Helius)** | | **89.142** | |

El modelo se desvía un 3 % de la cifra real, así que el contador del programa ya usa estas tarifas. Con ellas, analizar 150 wallets poco activas cuesta unos 4.000 créditos: lo más caro son las wallets muy activas y las fechas de creación de los tokens.

La base de datos de la prueba ocupa 321 MB, con 230.307 eventos de 221 wallets.

## 8. Problemas encontrados y corregidos

| Problema | Cómo se detectó | Corrección |
|---|---|---|
| Tres errores prudentes del clasificador (apartado 2.3) | Verificación con dos métodos y prueba en vivo | Reglas nuevas con tests sobre las transacciones reales |
| **Las posiciones abandonadas no contaban** en la rentabilidad por operación | Revisión de los dos «mejores» traders de la prueba | Cuentan como operaciones terminadas, valoradas a su precio de venta (apartado 5.2) |
| Valorar todo lo abandonado a 0 era injusto con quien mantiene posiciones vivas | Comprobación con precios reales de Jupiter | Valor de venta con precio y liquidez, sin usar precios posteriores a `as_of` |
| La robustez «sin las 3 mejores» se medía en dólares | Misma revisión | Por operación |
| La puntuación aditiva daba 36,84/100 a un trader que pierde por operación | Prueba en vivo | Puntuación = ventaja × calidad |
| SQLAlchemy guardaba `None` como el JSON `null`, así que la restricción de la base de datos no protegía nada | Test de integración | `JSONB(none_as_null=True)`, con test en PostgreSQL |
| Se podía analizar con una fecha futura (y el resultado quedaría congelado con datos incompletos) | Revisión de la primera prueba | La línea de órdenes la rechaza |
| En PostgreSQL, dividir enteros redondea: la banda de actividad salía mal | Revisión del código | División decimal, con test |
| El test de configuración fallaba si había una clave de Helius en el entorno | Al empezar la fase | Test aislado del entorno |

## 9. Limitaciones y problemas abiertos

1. **Tarifa de Helius deducida, no publicada.** Cuadra con el panel con un error del 3 % (apartado 7.6); conviene volver a contrastarla de vez en cuando.
2. **Muchas comparaciones.** Con 221 candidatos, alguno saldrá bien por azar. Un trader solo debería darse por bueno si **sigue siendo positivo en las semanas siguientes**, fuera de la muestra en la que se eligió. Eso es justo lo que medirá la Fase 4.
3. **Sesgo de supervivencia y de mirar hacia atrás.** Elegimos traders activos hoy y miramos sus últimos 30-90 días. Para el backtest de la Fase 5, la selección tendrá que hacerse «a fecha de entonces».
4. **Sin precio histórico de SOL.** Las compraventas pagadas en SOL no se valoran en dólares (se cuentan y se avisa). En FOMO son raras, pero hay traders, como el candidato, que también operan fuera de FOMO.
5. **Valoración de lo abandonado.** Usa el precio y la liquidez de Jupiter en el momento del análisis, y una fórmula de pool sencilla. Para análisis de fechas pasadas no hay precio y lo abandonado cuenta como 0.
6. **Comisiones de FOMO incluidas.** La rentabilidad de cada trader es neta de lo que le cobra FOMO (mediana 1,2 % por lado). Nosotros no las pagaríamos, pero sí las nuestras: lo cubre el supuesto del 3 %, que se calibrará en la Fase 4. Aun sumando ese 2,4 %, la mediana seguiría muy negativa.
7. **Liquidez de salida (seguidores de FOMO).** Aún no se puede medir si un trader vende cuando le copian sus seguidores: haría falta todo el tráfico del relayer alrededor de cada operación.
8. **Vínculos con creadores de tokens.** Solo se detecta que alguien vende tokens recibidos por transferencia, con confianza media.
9. **Historiales truncados.** Las wallets con más de 10.000 transacciones en el periodo se cortan (10 de 221) y se excluyen si falta parte de la ventana.
10. **Ambiguos restantes** (0,11 % de los eventos) y un programa de swap sin identificar: no se usan como señal.
11. **Margen de 60 minutos.** El análisis exige historiales actualizados como mucho 60 minutos antes de `as_of`. `copybot discover import --refresh` los pone al día pidiendo solo lo nuevo.
12. **Todo se lanza a mano** (`copybot discover …`). Se automatizará con el despliegue (Fase 7).

## 10. Conclusión y siguiente paso

**Los datos no respaldan copiar a los traders de FOMO en general.** De 221 analizados, solo uno muestra una ventaja positiva después del coste estimado de copiar, y con reservas importantes. El sistema hace lo que la Fase 1 pedía: medir antes de arriesgar. La respuesta, por ahora, es mayoritariamente «no».

### Lo que necesito de ti

1. **Direcciones de la leaderboard de FOMO.** Según fuentes de terceros, FOMO no muestra las direcciones de las wallets en su web.
   - Si la app las muestra en el perfil de cada trader, cópialas (por ejemplo, las 20 primeras de la clasificación a 30 días) en un fichero de texto, una por línea: `dirección, @usuario puesto 3`. Yo las analizo con `copybot discover add --file … --source leaderboard`.
   - Si no las muestra, existen webs extraoficiales que dicen «revelarlas» a partir del nombre de usuario. En la Fase 1 decidimos no depender de ellas. Usarlas sería decisión tuya, revisando antes sus condiciones; yo no las integraría en el programa.
   - En ambos casos, el PnL de la leaderboard solo sirve de pista: lo mediremos con nuestro propio cálculo.
2. **Una decisión sobre la Fase 4.** Mi recomendación:
   - **construir el simulador** (es imprescindible para medir el coste real de copiar, que ahora es un supuesto del 3 %);
   - **ponerlo a prueba con una lista pequeña en observación**: `A9aqAHUX…`, los traders de la leaderboard que pasen los filtros y los mejores de nuevas búsquedas;
   - **repetir el descubrimiento de traders poco activos periódicamente** (barato: unos 30.000 créditos por cada 150 wallets), para tener cada vez más candidatos y comprobar si los buenos **siguen siendo buenos** en las semanas siguientes.

   Si tras unas semanas ningún candidato mantiene la ventaja fuera de muestra, la conclusión será que copiar a usuarios de FOMO no es rentable, y te propondré cerrar el proyecto o cambiar de enfoque antes de gastar más.

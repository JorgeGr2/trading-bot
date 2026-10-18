# Informe de la Fase 3: descubrimiento y puntuación de traders

- **Fecha:** 18 de octubre de 2026
- **Estado:** terminada a falta de tu aprobación. Quedan limitaciones conocidas (apartado 9); ninguna es un fallo crítico.
- **Rama:** `claude/trusting-fermat-jangmb`, construida sobre `claude/upbeat-noether-027ixl` (Fase 2).

## 1. Resumen

{{RESUMEN}}

## 2. Verificación de la atribución con Helius

La Fase 2 dejó la precisión con una cota inferior del 91,6 % (42 compraventas). Esta fase la repite con más de 1.000 compraventas, como se acordó, antes de puntuar a nadie.

### 2.1 Método

Igual que en la Fase 2: cada transacción se clasifica con el método del sistema (**A**, cambios de saldo por propietario) y con un control independiente (**B**, reconstrucción a partir de las instrucciones de transferencia). Novedades:

- La muestra son **ventanas de 100 transacciones consecutivas del relayer** que empiezan en instantes al azar de los últimos 7 días (con semilla, reproducible), descargadas con `getTransactionsForAddress` de Helius. Así no depende de la hora a la que se ejecuta.
- Cuando A y B no coinciden, el script consulta on-chain la **configuración de comisión de transferencia (Token-2022)** de cada token implicado y recalcula B descontando la comisión exacta de cada transferencia. Una discrepancia solo se da por explicada si entonces coincide **al último decimal**.

Script: `scripts/spike_fomo_verification.py` (opciones `--helius-windows` y `--explain-transfer-fees`).

### 2.2 Resultados

| Muestra | Transacciones | Traders | Compraventas de confianza alta | Coinciden A = B | Explicadas por la comisión on-chain | Correctas |
|---|---|---|---|---|---|---|
| 1 (semilla 3) | 600 | 468 | 500 | 479 | 21 | **500 / 500** |
| 2 (semilla 4, tomada **después** de las correcciones) | 600 | 508 | 525 | 483 | 42 | **525 / 525** |
| **Total** | 1.200 | — | **1.025** | 962 | 63 | **1.025 / 1.025** |

- **Precisión de la confianza alta: 100 % en la muestra; cota inferior de Wilson al 95 %: 99,6 %** (criterio: ≥ 95 %).
- **Todas las discrepancias** son compras o ventas de tokens Token-2022 que cobran un **1 % o un 3 % en cada transferencia**. El método A refleja lo que el usuario recibe de verdad; el control B, lo que envían los pools. Son entre el 4 % y el 8 % de las compraventas: un coste real que la simulación tendrá que tener en cuenta.
- Sin evento: 174 transacciones que no son compraventas de memecoins (sobre todo depósitos de USDC, `DepositToken`, y 20 conversiones SOL↔USDC o USDT↔USDC, revisadas una a una).
- 1 venta quedó en confianza media porque usa un programa de swap que no está en la lista (`proVF4pM…`, instrucción `SwapTobV3`). No lo he añadido sin saber qué es.
- Las ventanas de la muestra 2 no se usaron para diseñar ninguna corrección, así que sirven de prueba independiente.

### 2.3 Fallos del clasificador encontrados y corregidos

Los tres eran errores **prudentes** (una compra real quedaba como «ambigua» y se perdía la señal), nunca señales falsas.

| Caso real | Qué pasaba | Corrección | Afectaba a |
|---|---|---|---|
| Ruta USDC → token intermedio → memecoin que deja en la wallet un resto del intermedio (0,7-0,9 %) | Dos tokens cambian de saldo: «ambigua» | Si un token **entra y sale** de la wallet en la misma transacción y queda como mucho un 2 % de lo que pasó por ella, es resto de ruta. Se anota en los motivos | ~1 % de las operaciones de un trader activo |
| La instrucción `create` nombra a la wallet como pagadora de la cuenta de comisiones de **otra** wallet, aunque los lamports los pone el relayer | Se le atribuía esa renta: «ambigua» | Solo se descuenta la renta de cuentas **propias** de la wallet | Casos sueltos |
| FOMO cobra 2.039.280 lamports por la cuenta de token nueva y devuelve 1.488.440 al cerrar una cuenta temporal de wSOL: el usuario paga además −0,00055084 SOL | USDC y SOL a la vez: «ambigua» | Un movimiento de SOL de hasta 0,003 SOL junto a un pago en USDC/USDT es coste de cuentas, no precio. Se anota en los motivos | **1.795 compraventas** de la primera prueba en vivo (0,9 % de los eventos) |

La tercera corrección se verificó aparte con **120 transacciones reales** que antes quedaban ambiguas: se recuperan 91 compraventas, y **todas** coinciden con el control B (85 directamente, 6 al descontar la comisión del token). Las 27 que siguen siendo ambiguas son operaciones realmente mixtas (compras en las que parte del USDC se convierte en SOL para el usuario) o barridos que venden varios tokens a la vez. Es correcto no usarlas.

Hay tests con cada una de estas transacciones reales (`tests/fixtures/tx/`).

## 3. Lo que se ha medido de la API de Helius

La documentación de Helius no es accesible desde este entorno, así que lo he comprobado con llamadas reales:

| Hecho | Resultado |
|---|---|
| `getTransactionsForAddress` con la clave actual | **Funciona**, también en modo completo (transacciones enteras) |
| Transacciones por petición en modo completo | **Hasta 1.000** (con 1.001, el nodo responde «You can only request up to 1000 transactions at a time»). Algunas páginas de la documentación dicen 100 |
| Paginación | El nodo devuelve un `paginationToken` incluso en la última página; la siguiente llega vacía. El cliente para cuando la página no está llena, para no pagar esa petición vacía |
| Filtros | Por fecha (`blockTime`), por estado (solo las que tuvieron éxito) y `tokenAccounts: "balanceChanged"`, que añade lo recibido en sus cuentas de token sin que la wallet firme (+7 transacciones en un día de un trader) |
| Orden dentro del bloque | Cada transacción trae `transactionIndex`; se guarda para ordenar sin ambigüedad |
| Fecha de creación de un token | Su primera transacción, con una sola petición en orden ascendente (0,4 s) |
| Coste | **100 créditos por petición** según la documentación (sea de 1 o de 1.000 transacciones). **No lo he podido comprobar**: el consumo real solo se ve en el panel de Helius |
| Fuentes de precios históricos (Binance, Pyth, CoinGecko) | **Bloqueadas** por la red del entorno. Consecuencia: las operaciones pagadas en SOL no se valoran en dólares (apartado 9) |
| Ritmo del relayer | 1.000 transacciones cubren unos 107 segundos (~9 por segundo), con unos 640 traders distintos |

## 4. Qué se ha construido

| Fichero | Función |
|---|---|
| `src/copybot/solana/rpc.py` | `getTransactionsForAddress` con paginación y filtros; créditos por método; **control de presupuesto antes de cada petición** |
| `src/copybot/budget.py` | Presupuesto mensual de créditos para el análisis (por defecto, el 60 % del plan Free: 600.000). Al llegar al límite se para antes de pedir nada; el seguimiento en vivo no se ve afectado |
| `src/copybot/ingest/discovery.py` | Muestreo del relayer por ventanas al azar; fechas de creación de tokens (una consulta por token, guardada para siempre) |
| `src/copybot/ingest/history.py` | Importación del historial de cada wallet (37 días): de lo más reciente a lo más antiguo, con tope de páginas, incremental, idempotente |
| `src/copybot/analysis/positions.py` | Reconstrucción de posiciones con lotes FIFO y de cada operación completa (*round trip*) |
| `src/copybot/analysis/metrics.py` | Métricas por ventana (7 y 30 días) |
| `src/copybot/analysis/scoring.py` | Filtros de exclusión, puntuación explicada, avisos con su nivel de confianza y `params_hash` |
| `src/copybot/analysis/service.py` | Une base de datos y analizador; guarda *snapshots* y puntuaciones |
| `src/copybot/traders_cli.py` | Órdenes `discover`, `analyze` y `traders` |
| `migrations/versions/20261018_336866a1abcc_…` | Tablas nuevas y cambios (apartado 4.2) |

### 4.1 Reglas que garantiza

1. **Nada posterior a `as_of`.** El analizador descarta cualquier evento posterior a la fecha de análisis, aunque se le pase por error. Un test lo comprueba con 20 historiales aleatorios: añadir, quitar, cambiar o desordenar eventos posteriores no altera ni un decimal.
2. **Snapshots inmutables y versionados.** Cada cálculo se guarda con su `as_of` y un `params_hash` (versión del analizador + todos sus parámetros). Repetirlo no lo sobrescribe: se conserva el primero.
3. **Periodo cubierto honesto.** Una importación solo marca como completo el periodo que de verdad ha descargado, y solo al terminar. Si se interrumpe, no queda nada «cubierto» a medias; al repetirla no se duplica nada. Si una wallet tiene más transacciones del tope (10 páginas de 1.000), se conserva lo más reciente y se marca como truncada.
4. **El historial no guarda el JSON completo** de cada transacción (ocuparía ~30 KB por transacción y se puede volver a descargar). La base de datos impide que una fila en vivo o recuperada se quede sin él.
5. **El gasto nunca supera el presupuesto**: se comprueba antes de cada petición.

### 4.2 Cambios en la base de datos

| Tabla | Contenido |
|---|---|
| `raw_transactions`, `wallet_events` | Nuevo origen `history`; posición en el bloque (`tx_index`); JSON opcional **solo** en filas de historial (restricción `CHECK`) |
| `tokens` | Fecha y firma de creación de cada token consultado |
| `discovery_windows`, `discovery_observations` | Ventanas muestreadas y compraventas vistas en ellas (una vez por firma y wallet) |
| `history_imports` | Periodo importado completo de cada wallet |
| `wallet_metric_snapshots`, `wallet_scores` | Métricas y puntuaciones versionadas |

Volver a la versión anterior de la base de datos **borra el historial importado** (se puede volver a importar) y conserva lo detectado en vivo; hay un test que lo comprueba.

## 5. Cómo se calculan las métricas y la puntuación

### 5.1 Operaciones y PnL

- Una **operación** empieza con la primera compra de un token y termina cuando el trader ha vendido todo (o le queda menos del 1 % de lo que llegó a tener; ese resto se da por perdido).
- Las ventas se asignan a las compras en orden (FIFO). Solo cuentan para el PnL las partes con **coste y cobro conocidos en USDC o USDT**.
- Lo que se vende sin haberlo comprado en el periodo importado cuenta como **venta sin coste conocido** (dato incompleto). Los tokens recibidos por transferencia son **tokens gratis**: si se venden, se avisa.
- **PnL prudente**: el PnL realizado menos el coste de las posiciones compradas hace más de 72 horas y aún sin vender, valoradas a 0. Así no parece rentable quien vende sus aciertos y se queda con las pérdidas.

### 5.2 Por qué importa la rentabilidad *por operación*

Nosotros copiaríamos con un **tamaño fijo** (10 $). Lo que importa es cuánto gana de media **cada operación**, no el PnL en dólares del trader, que lo deciden sus apuestas más grandes. Por eso la puntuación usa la rentabilidad media por operación, con dos precauciones:

- **Media robusta:** cada operación cuenta como mucho +200 %, para que un único acierto enorme no lo decida todo.
- **Estimación prudente:** media − 1 × error típico (con pocas operaciones, la estimación baja).

### 5.3 Filtros de exclusión

Una wallet no se puntúa si:

| Motivo | Umbral por defecto |
|---|---|
| El historial importado no cubre los 30 días o no está al día | — |
| Muestra insuficiente | < 20 operaciones cerradas con coste conocido |
| No copiable: vende demasiado pronto | Mediana < 60 s desde la compra hasta su primera venta |
| Compra en el lanzamiento (*sniping*) | > 30 % de entradas en los primeros 60 s de vida del token (con ≥ 10 tokens comprobados) |
| Datos incompletos | > 25 % de lo vendido sin compra conocida |
| Frecuencia imposible de seguir | > 300 compraventas al día |

### 5.4 Puntuación

**Puntuación = 100 × ventaja × calidad**

- **Ventaja** (0 a 1): estimación prudente de la rentabilidad media por operación, **menos un coste estimado de copiar del 3 %** por operación completa (comisiones, slippage y llegar tarde; supuesto que se calibrará en la Fase 4). Vale 1 con un +10 % neto y 0 si es negativa. **Sin ventaja, la puntuación es 0**, por bien que salgan los demás componentes.
- **Calidad** (0 a 1), media ponderada de:
  - **consistencia** (25 %): en positivo en las dos mitades del periodo, en los últimos 7 días y sin sus 3 mejores operaciones;
  - **copiable** (35 %): tiempo hasta la primera venta (1 desde 10 minutos), compraventas al día y entradas en el lanzamiento;
  - **riesgo** (25 %): caída máxima del PnL frente al capital que llegó a tener invertido, y posiciones abandonadas;
  - **datos** (15 %): ventas sin coste conocido y eventos ambiguos, de confianza media o no pagados en USDC/USDT.

Todos los umbrales y pesos se pueden cambiar con variables `COPYBOT_ANALYSIS_*` y `COPYBOT_SCORING_*`; cada cambio produce otro `params_hash`, así que los resultados de configuraciones distintas nunca se mezclan.

**Cambio durante el desarrollo, antes de usarla para nada:** la primera versión sumaba los cinco componentes con pesos. En la primera prueba en vivo dio 36,84/100 a un trader que **pierde** por operación, porque sumaba por ser copiable y tener buenos datos. Era engañoso, así que pasé a la fórmula multiplicativa. Ningún otro parámetro se ha ajustado mirando resultados.

### 5.5 Avisos (con su nivel de confianza)

`alta_frecuencia`, `salidas_rapidas`, `compras_en_lanzamiento`, `sniping_sin_comprobar`, `vende_tokens_recibidos` (posible vínculo con el creador; confianza media, nunca se etiqueta a nadie como *insider*), `misma_ranura` (compra y venta en el mismo bloque, típico de bots), `beneficio_concentrado`, `posiciones_abandonadas`, `ventas_sin_compra`, `muestra_pequeña` y `operaciones_no_usd`.

## 6. Pruebas ejecutadas

```
$ uv run pytest
{{PYTEST}}

$ uv run ruff check src tests scripts migrations
All checks passed!
```

Los tests que usan base de datos se ejecutan contra PostgreSQL 16 real.

| Área | Qué se comprueba |
|---|---|
| Métricas calculadas a mano | Un escenario de 13 eventos con FIFO, resto abandonado, tokens gratis, venta sin coste, posiciones abandonadas, caída máxima, capital máximo, *sniping* y un evento posterior a `as_of`: cada cifra esperada está razonada en el test |
| Sin información del futuro | 20 historiales aleatorios: el resultado no cambia al añadir, cambiar o desordenar eventos posteriores a `as_of`; y de extremo a extremo, con historial nuevo llegado después |
| Puntuación | Una puntuación calculada a mano (49,23), cada filtro de exclusión, avisos, validación de parámetros, `params_hash` y determinismo |
| Importación | Paginación, periodo cubierto, importación incremental, truncado, **corte a mitad sin duplicados al repetir**, eventos de dos wallets en la misma transacción |
| Monitor y historial | Una transacción importada sin JSON se completa cuando la ve el monitor, sin duplicar eventos |
| Descubrimiento | Ventanas reproducibles, candidatas sin contar dos veces la misma operación, exclusión de las ya importadas |
| Fechas de creación | Se consultan como mucho N por wallet y nunca dos veces el mismo token |
| Presupuesto | Bloquea **antes** de enviar la petición; solo cuenta el mes en curso y el proveedor correcto |
| Base de datos | La restricción del JSON se cumple de verdad (guarda `NULL` de SQL, no el JSON `null`); las migraciones coinciden con los modelos y se pueden deshacer con datos |
| Clasificador | 11 transacciones reales como *fixtures* (4 nuevas en esta fase) y casos sintéticos nuevos (resto de ruta pequeño y grande, SOL pequeño y grande junto a USDC) |

## 7. Prueba en vivo con datos reales

{{EN_VIVO}}

## 8. Problemas encontrados y corregidos

| Problema | Cómo se detectó | Corrección |
|---|---|---|
| Tres errores prudentes del clasificador (apartado 2.3) | Verificación con dos métodos y prueba en vivo | Reglas nuevas con tests sobre las transacciones reales |
| SQLAlchemy guardaba `None` como el JSON `null` en vez de `NULL` de SQL, así que la restricción de la base de datos no habría protegido nada | Test de integración | `JSONB(none_as_null=True)` y test que lo comprueba en PostgreSQL |
| La puntuación aditiva daba 36,84/100 a un trader que pierde por operación | Prueba en vivo | Puntuación = ventaja × calidad |
| El test de configuración fallaba si había una clave de Helius en el entorno | Al empezar la fase | Test aislado del entorno |

## 9. Limitaciones y problemas abiertos

1. **Coste real en créditos sin confirmar.** El sistema cuenta 100 créditos por petición de `getTransactionsForAddress`, como dice la documentación de Helius, pero el consumo real solo se ve en su panel. Conviene que lo compruebes (apartado 10).
2. **Sesgo de selección.** El muestreo encuentra antes a quien opera mucho: las 70 wallets analizadas hacen una mediana de decenas de compraventas al día. Puede haber traders buenos que operan poco y casi nunca caen en las ventanas. Encontrarlos exige otro método de descubrimiento (apartado 10).
3. **Sesgo de supervivencia y de mirar hacia atrás.** Elegimos traders activos hoy y miramos sus últimos 30 días. Para el backtest de la Fase 5, la selección tendrá que hacerse también «a fecha de entonces».
4. **Sin precio histórico de SOL.** Las compraventas pagadas en SOL no se valoran en dólares y quedan fuera del PnL (se cuentan y se avisa). En FOMO son raras porque casi todo es en USDC.
5. **Comisiones de FOMO incluidas.** La rentabilidad de cada trader es neta de lo que le cobra FOMO (mediana 1,2 % por lado). Nosotros no pagaríamos esas comisiones, pero sí las nuestras; lo cubre el supuesto del 3 % de coste de copiar, que se calibrará en la Fase 4.
6. **Liquidez de salida (seguidores de FOMO).** Todavía no se puede medir si un trader vende justo cuando le copian sus seguidores: haría falta todo el tráfico del relayer alrededor de cada operación, y no cabe en el presupuesto.
7. **Vínculos con creadores de tokens.** Solo se detecta que alguien vende tokens que recibió por transferencia, con confianza media. No se rastrea quién financió cada wallet.
8. **Historiales truncados.** Las wallets con más de 10.000 transacciones en 37 días se cortan; si el corte deja fuera parte de los 30 días, se excluyen.
9. **Eventos ambiguos restantes.** Compras en las que parte del USDC se convierte también en SOL, y barridos que venden varios tokens a la vez. No se usan. Los barridos hacen que esas posiciones parezcan abiertas, lo que puede inflar un poco las «posiciones abandonadas».
10. **Un programa de swap sin identificar** (`proVF4pM…`): sus operaciones quedan en confianza media y no se usan como señal.
11. **Fecha de creación por muestreo.** Se consulta un número limitado de tokens por wallet (una petición cada uno). Con menos de 10 comprobados, se avisa de que el *sniping* no está comprobado.
12. **Análisis y descubrimiento a mano.** Todavía no hay tareas periódicas: se lanzan con `copybot discover run`. Se automatizará con el despliegue (Fase 7).
13. **Margen de 60 minutos.** El análisis exige que el historial esté actualizado como mucho 60 minutos antes de `as_of`; una importación de muchas wallets que tarde más tendría que analizarse en dos tandas.

## 10. Decisión y siguiente paso

{{DECISION}}

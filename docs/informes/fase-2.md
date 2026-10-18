# Informe de la Fase 2: conectores de datos y monitor de wallets

- **Fecha:** 18 de octubre de 2026
- **Estado:** terminada a falta de tu aprobación. Quedan limitaciones conocidas (apartado 7); ninguna es un fallo crítico.
- **Rama:** `claude/upbeat-noether-027ixl`

## 1. Resumen

- **Se pueden identificar los traders de FOMO con datos públicos de la blockchain.** Muestra: 49 transacciones reales de 39 traders distintos.
  - En el 100 % de la muestra, una wallet de FOMO firma y paga la transacción, y el trader aparece como **segundo firmante**.
  - El bot atribuye cada operación a quien cambia de saldo, no a quien paga, y acierta en todas las compraventas de la muestra.
- **El aviso de cada operación llega por WebSocket con un retraso mediano de 1,1–1,5 s** respecto al bloque. Pero con el RPC público gratuito, **descargar la transacción completa añade una mediana de 5,8 s (hasta 59 s)** por los límites de uso. Para seguir traders en serio hace falta Helius.
- **El monitor sigue wallets, guarda cada operación, se recupera de cortes y reinicios** y no duplica nada. Lo prueban 100 tests automáticos y tres pruebas en vivo con traders reales.
- **Hallazgos que afectan a la estrategia:**
  - Algunos tokens que compran los usuarios de FOMO cobran un 1 % en cada transferencia, y su creador puede subir esa comisión.
  - FOMO cobra a sus usuarios entre un 0,2 % y un 9,5 % por operación (mínimo de 0,10 $).
  - Hay traders que rotan el mismo dinero entre tokens en cuestión de segundos.
- **Para descubrir traders a escala hace falta Helius** (o un proveedor equivalente). El RPC público gratuito no da para ello desde este entorno: permitió unas 50 descargas de transacciones en casi una hora.

## 2. Verificación con datos reales

### 2.1 Método

1. Se descargaron las transacciones más recientes firmadas por la wallet de FOMO `AgmLJBMDCqWynYnQiPCuj9ewsNNsBJXyzoUhD9LJzN51`, en formato `jsonParsed` y desde el RPC público.
2. Cada transacción se clasificó con **dos métodos independientes**:
   - **A (el del sistema):** cambios de balance de token antes y después, agrupados por propietario.
   - **B (control):** reconstrucción a partir de las instrucciones de transferencia en las que interviene el usuario.
3. Las discrepancias se revisaron a mano.

Script reproducible: `scripts/spike_fomo_verification.py`.

### 2.2 Resultados

| Medida | Resultado |
|---|---|
| Transacciones analizadas | 49 (39 traders distintos) |
| El relayer de FOMO es quien paga y el primer firmante | 49 / 49 |
| Firmantes por transacción | 2 en las 49 (relayer + trader) |
| Cuenta marcadora `jitodontfront…TradeonFomo` presente | 49 / 49 |
| Compraventas de memecoins | 42 (24 compras y 18 ventas), todas con confianza **alta** |
| Transacciones que no son compraventas | 7 depósitos de USDC (`DepositToken`), correctamente sin evento |
| Moneda de pago | **USDC en el 100 % de las compraventas** |
| Coincidencia A = B | 40 / 42 |
| Discrepancias revisadas a mano | 2. **El método A es correcto en ambas**; el fallo está en el control B (ver abajo) |
| Atribuciones correctas de confianza alta | **42 / 42**. Intervalo de Wilson al 95 %: ≥ 91,6 % (la muestra todavía es pequeña) |
| Programas de swap | DFlow 39, PumpSwap 27, pump.fun 8, Raydium CLMM 7, Orca 6, Meteora DLMM 5, Jupiter 2, Raydium CPMM 1 |
| Comisión de FOMO al usuario | Mediana 1,26 %, rango 0,20–9,50 %, mínimo 0,10 USDC (n = 42) |

**Las dos discrepancias:**

1. **Token-2022 con comisión de transferencia** (`DEW9dSN6…`, «Super Inu»). Los pools enviaron 30.585,39 tokens, pero el usuario recibió 30.279,54: el token retiene un 1 % en cada transferencia. Su configuración on-chain muestra `transferFeeBasisPoints: 100` y una autoridad que **puede cambiar esa comisión**. El método A refleja lo que el usuario recibe realmente.
2. **Polvo de la ruta.** Al usuario le quedó 0,00000001 de un token intermedio. Al principio, el método A lo marcó como «ambiguo»: era un error conservador, sin señal falsa pero con una señal perdida. **Corregido:** los cambios de ≤ 100 unidades mínimas se tratan como polvo y se dejan registrados. Hay un test con esta transacción real.

### 2.3 Latencia (`scripts/spike_ws_latency.py`)

| Medida | Resultado |
|---|---|
| Transacciones del relayer recibidas | 499 en 90 s (**5,5 tx/s**, unos 14 millones al mes) |
| Latencia frente a `blockTime` (resolución ±1 s) | Mediana **1,09 s**, p95 1,57 s, mínimo 0,51 s, máximo 1,70 s (n = 436) |

A esto hay que sumar la descarga de la transacción completa, porque `logsSubscribe` solo trae la firma. Con el RPC público, esa descarga puede tardar desde segundos hasta minutos por los límites de uso.

### 2.4 Coste de datos (estimación con cifras medidas)

| Medida | Valor |
|---|---|
| Tamaño medio de una transacción `jsonParsed` | 30 KB |
| Transmitir en vivo **todas** las transacciones del relayer | ~446 GB/mes, unos 8,9 M de créditos de Helius (casi todo el plan Developer). **No es viable.** |
| `getTransaction` una a una | ~14 M de créditos/mes. **No es viable.** |
| `getTransactionsForAddress` por lotes de 100 | ~1,4 M de créditos/mes según la tarifa publicada. **Viable con Developer**; el precio está pendiente de comprobar con una clave real. |
| Seguir 10 wallets (recuperación cada 5 min + 1 `getTransaction` por operación) | ~0,1 M de créditos/mes. Cabe en el plan gratuito. |

**Conclusión de diseño:** el descubrimiento de traders se hará por lotes (no necesita velocidad) y el seguimiento en vivo, solo para las wallets elegidas.

### 2.5 Otros hechos verificados

- Ya existen transacciones **versión 1**. Hay que pedirlas con `maxSupportedTransactionVersion: 1`; con 0 el nodo devuelve el error `-32015`.
- La renta de una cuenta de token (165 bytes) es **1.488.440 lamports (0,00148844 SOL)**, no 0,00203928. Corregido en el informe de la Fase 1.
- FOMO paga la renta de las cuentas de token nuevas de sus usuarios.

### 2.6 Decisión

**Se cumple el criterio de la Fase 2:** ningún error de atribución en confianza alta, y las discrepancias quedan explicadas. Como la muestra es pequeña (cota inferior del 91,6 %), la Fase 3 **repetirá la verificación con ≥ 300 transacciones** usando Helius, antes de puntuar a ningún trader.

## 3. Qué se ha construido

| Fichero | Función |
|---|---|
| `src/copybot/config.py` | Configuración. Los límites de riesgo acordados están topados en el código y solo se pueden endurecer. Único modo posible: `simulation`. Presupuesto ≤ 50 $. |
| `src/copybot/solana/addresses.py` | Validación de direcciones Solana (base58, 32 bytes) |
| `src/copybot/solana/classifier.py` | Clasificador: compra, venta, transferencia o ambiguo, con confianza y motivos. Excluye la comisión de red y la renta del precio. Ignora el polvo. |
| `src/copybot/solana/rpc.py` | Cliente RPC de solo lectura: límite de peticiones por segundo, timeouts, reintentos que respetan `Retry-After` y contabilidad de uso |
| `src/copybot/solana/ws.py` | Suscripción WebSocket con reconexión automática; avisa de cada reconexión |
| `src/copybot/ingest/wallets.py` | Monitor de wallets en vivo, con recuperación de huecos e idempotencia |
| `src/copybot/db/` | Modelos PostgreSQL y repositorio con inserciones idempotentes |
| `migrations/` | Migración inicial de Alembic |
| `src/copybot/logging.py` | Logs JSON con ocultación de claves, contraseñas y tokens |
| `src/copybot/cli.py` | `copybot wallets add/list`, `copybot monitor` |
| `scripts/spike_*.py` | Verificación reproducible con datos reales |
| `tests/` | 100 tests, con 7 transacciones reales de FOMO como *fixtures* |

**Reglas que garantiza el monitor:**

1. Solo la recuperación avanza el *checkpoint* de cada wallet, así que nunca deja huecos.
2. Procesar dos veces la misma transacción no tiene efecto (clave única por firma, y por firma + wallet).
3. Las operaciones detectadas en vivo guardan su latencia. Las recuperadas se marcan como `backfill` y **no podrán abrir posiciones** en la simulación, porque la oportunidad ya pasó.
4. Un fallo del proveedor no detiene el monitor: se registra y se reintenta en la siguiente recuperación.

## 4. Pruebas ejecutadas

```
$ uv run pytest
100 passed

$ uv run ruff check src tests scripts migrations
All checks passed!
```

Los tests que usan base de datos se ejecutan contra **PostgreSQL 16 real**, no contra simulaciones. Cubren:

| Área | Tests |
|---|---|
| Clasificador | 7 transacciones reales (compras, ventas, venta total, ruta por Jupiter, Token-2022 con comisión, polvo, depósito) y casos sintéticos: transferencia entre wallets, swap token-token, transacción fallida, compra en SOL con renta y comisión, venta con cierre de cuentas wSOL, trader que no firma, ningún programa de swap conocido, marcador de FOMO, JSON mal formado |
| Duplicados | La misma notificación repetida; el mismo evento llegando en vivo y en la recuperación |
| Reconexión | Servidor WebSocket local que corta la conexión; servidor inaccesible |
| Huecos | Operaciones durante una desconexión, recuperadas al volver; historial paginado; hueco mayor que el límite, avisado |
| Reinicio | Una instancia nueva continúa desde el *checkpoint* |
| Proveedor caído | Respuestas 429 o 5xx, errores de red, transacción aún no disponible; un fallo en una wallet no bloquea las demás |
| Persistencia | El *checkpoint* nunca retrocede; las migraciones crean exactamente el esquema de los modelos y son reversibles |
| Seguridad | Los límites no se pueden relajar (ni desde variables de entorno); `MODE` distinto de `simulation` rechazado; secretos ocultos en la representación y en los logs; no hay código ni dependencias de firma; direcciones maliciosas rechazadas |

## 5. Pruebas en vivo con traders reales

Se siguieron 10 traders de la muestra con el monitor completo (RPC y WebSocket públicos, PostgreSQL local).

- **Primera ejecución:** uno de los traders operó a los pocos segundos. El RPC público respondió 429 cinco veces seguidas y **el monitor se detuvo**. Es un fallo real. Corregido: ahora el error se registra y la operación se recupera en la siguiente pasada. Hay tests nuevos que lo cubren.
- **Segunda ejecución:** al arrancar desde el *checkpoint* anterior, el monitor **recuperó automáticamente las 4 operaciones** que ese trader hizo mientras estaba parado, entre ellas la que provocó el fallo. Las 4 se clasificaron como compraventas de confianza alta en USDC y se marcaron como `backfill`.

- **Segunda ejecución, 10 minutos completos.** La detuvo el temporizador de la prueba (código 124), no un fallo.
  - 16 transacciones de 4 de los 10 traders: 11 detectadas en vivo y 5 recuperadas (las 4 del parón y 1 que el proveedor no dejó descargar en vivo).
  - 15 compraventas de confianza alta, todas en USDC.
  - 1 «transferencia recibida»: un reparto automático de pump.fun a los poseedores de un token (`DistributeFeeToHolders`). Revisada a mano: la clasificación es correcta y **no es una compra**.
  - El RPC público rechazó 3 descargas tras agotar los reintentos. El monitor siguió funcionando y las recogió después.
  - La latencia anotada (mediana 9,9 s, máximo 74 s) estaba **inflada por un error de medición** (apartado 6), ya corregido.
- **Tercera ejecución, con el código corregido, 8 minutos completos** (detenida por el temporizador).
  - 22 transacciones (20 en vivo y 2 recuperadas). 18 compraventas de confianza alta en USDC, de 4 traders.
  - 2 descargas rechazadas por el proveedor: gestionadas sin detener el monitor.

  | Medida (20 transacciones en vivo) | Mínimo | Mediana | p95 | Máximo |
  |---|---|---|---|---|
  | Del bloque al aviso por WebSocket | 0,58 s | 1,49 s | 3,02 s | 4,17 s |
  | Del aviso a tener la transacción completa (RPC público) | — | 5,8 s | — | 59 s |

  **Conclusión:** con el RPC público, el bot tardaría una mediana de unos 7 s en poder decidir, y hasta un minuto en los peores casos. Casi todo ese tiempo se va en esperar por los límites de uso. **Para seguir traders en serio hace falta un proveedor sin esa limitación (Helius)**, incluso con pocas wallets.
  - Se ven estilos de trading muy distintos. Un trader (`J517xwf6…`) entra y sale con 100–200 $ varias veces en pocos minutos; otro (`7ab7MBy9…`) rota unos 8 $ entre tokens en segundos.

## 6. Problemas encontrados y corregidos

| Problema | Cómo se detectó | Corrección |
|---|---|---|
| El monitor se detenía si el proveedor rechazaba peticiones de forma continuada | Prueba en vivo | Captura del error, recuperación posterior y tests |
| `httpx` escribía en los logs la URL completa del proveedor, que con Helius incluye la clave | Revisión de los logs de la prueba en vivo | Logs de `httpx` silenciados y filtro de secretos en todos los logs, con test |
| Las compras con polvo de la ruta quedaban marcadas como ambiguas | Verificación con dos métodos | Umbral de polvo, con test sobre la transacción real |
| Una latencia anómala podía desbordar la columna (`int32`) | Test | Columna `BIGINT` |
| Los avisos en vivo se leían solo cuando terminaba de procesarse el anterior, así que su hora de llegada se anotaba tarde y la latencia salía inflada (hasta 74 s) | Prueba en vivo | Lectura en una tarea aparte, con test. Además se guarda `fetched_at` (cuándo se tuvo la transacción completa) |
| Un JSON con forma inesperada podía lanzar un error no previsto y detener el monitor | Revisión del código | El clasificador solo lanza `MalformedTransactionError`, con tests de 5 deformaciones |
| Las peticiones con versión 0 fallaban en transacciones nuevas | Datos reales | `maxSupportedTransactionVersion: 1` |

## 7. Limitaciones y problemas abiertos

1. **RPC público:** desde este entorno, la IP compartida recibe muchas respuestas 429. Sirve para pruebas, no para el funcionamiento 24/7 ni para el descubrimiento. Para la Fase 3 hace falta una cuenta de **Helius**: empezar con el plan gratuito y pasar a Developer solo con tu aprobación.
2. **`logsSubscribe`:** una suscripción por wallet, y el WebSocket público admite unas 10. Para seguir más wallets hará falta `transactionSubscribe` de Helius, que además trae la transacción completa y ahorra la descarga posterior.
3. **Cambiar la lista de wallets exige reiniciar el monitor.** Se resolverá cuando exista el panel de control (Fase 6).
4. **Muestra de verificación pequeña** (42 compraventas). Se ampliará a ≥ 300 en la Fase 3.
5. **Swaps entre dos memecoins** quedan como «ambiguos» y no se usan como señal. En la muestra no apareció ninguno.
6. **Primer arranque:** el punto de partida de cada wallet es su operación más reciente; el historial anterior no se importa todavía. El análisis histórico es trabajo de la Fase 3.
7. **Todavía no hay estado global de «datos fiables / solo lectura»** ni alertas: Fases 4, 6 y 7.

## 8. Siguiente fase (3): descubrimiento y puntuación de traders

- Repetir la verificación con ≥ 300 transacciones usando Helius.
- Descubrir traders a partir del relayer de FOMO, por lotes y con presupuesto controlado.
- Calcular las métricas de cada trader (PnL realizado a 7 y 30 días, *win rate*, tiempo de *holding*, drawdown, concentración) a partir de sus operaciones on-chain.
- Detectar comportamientos no copiables: *sniping*, *holdings* de segundos, rotación rápida (como la del trader `7ab7MBy9…` de la prueba en vivo).
- Puntuación configurable con explicaciones y *snapshots* versionados.

**Lo que necesito de ti para empezar la Fase 3:**

- Crear una cuenta gratuita en Helius y guardar la clave como secreto del entorno, sin pegarla en el chat. Te guiaré paso a paso.
- Añadir `mainnet.helius-rpc.com` y `api.helius.xyz` a los dominios permitidos. Ya los añadiste.

# Informe de la Fase 4: motor de simulación, filtros de seguridad y gestión de riesgo

- **Fecha:** 19 de octubre de 2026
- **Estado:** pendiente de tu aprobación.
- **Rama:** `claude/trusting-fermat-jangmb` (sobre la Fase 3).

## 1. Resumen

- **El motor de simulación está terminado.**
  - Copia en vivo las compras y ventas de los traders seguidos con cotizaciones reales de Jupiter, latencia, slippage, MEV, comisiones y renta.
  - Lleva dos libros: la cartera de 100 $ con todos los límites de riesgo y un libro que copia cada señal con el mismo tamaño.
  - **Perfil de riesgo agresivo, por tu decisión del 20-oct:** 25 $ por posición, hasta 8 posiciones, 10 % de reserva, 20 % de pérdida diaria y 50 % de caída máxima (antes, 10 $, 5, 20 %, 10 % y 25 %). Las pruebas en vivo de este informe se hicieron con el perfil anterior, de 10 $.
  - Se para solo por pérdidas y pasa a solo lectura si el monitor deja de dar datos.
- **Cumple todos los criterios de aceptación de la Fase 4** fijados en la Fase 1. Lo prueban 276 tests en verde: PnL y costes exactos, slippage, tamaño, stops, caída máxima y pausa automática, rechazo por datos insuficientes, saldos nunca negativos, nunca vende más de lo que tiene, y no duplica nada tras matar el proceso a mitad de una orden.
- **Dos revisiones adversariales independientes** encontraron 45 problemas reales (34 + 11), todos corregidos; los que cambian el comportamiento, con su test. Los más graves: un descuadre del diario por redondeo, órdenes que se podían quedar a medias, el valor de una posición tras una venta parcial, y caminos para saltarse la parada por riesgo.
- **En vivo:**
  - detecta las operaciones en **1,0 s** (mediana), decide en unos **4 s** y la compra simulada termina a los **6 s** de la operación del trader;
  - con la configuración aprobada no copió ninguna de 40 compras en 2 horas (por los filtros);
  - con dos filtros relajados copió 5 y comprobó toda la cadena;
  - en el token más operado, comprar y vender a la vez cuesta un **6,4 %**.
- **Leaderboard y Kimchi:**
  - he analizado **308 traders** de la leaderboard de FOMO (fomoapi.io); como grupo pierden en los últimos 30 días: −20 % de mediana por operación, y −14 % entre los 57 que se pueden puntuar;
  - **5 de 230 traders puntuables** tienen ventaja después del coste de copiar: A9aqAHUX, @nekomayu, @OinkersRUs, @kecoseh y @Washedupke3d;
  - **Kimchi es un token**, no un trader: lo ganaron sobre todo quienes lo compraron hace meses; entre quienes lo han operado esta semana, los resultados son dispares.
- **Nada demuestra todavía que copiar sea rentable.** Eso solo lo dirán el backtester (Fase 5) y una prueba larga en vivo (Fase 8).

## 2. Qué se ha construido

| Fichero | Función |
|---|---|
| `src/copybot/sim/engine.py` | **Motor**: convierte cada compraventa de un trader seguido en una decisión (copiar o no, y por qué) en los dos libros; salidas, stop-loss, tiempo máximo, reintentos, valoración y paradas por riesgo |
| `src/copybot/sim/store.py` | **Almacén**: estado del sistema, señales, órdenes con clave de idempotencia, reserva de efectivo atómica, compras, ventas, diario de doble entrada y recuperación tras un reinicio |
| `src/copybot/sim/executor.py` | **Ejecutor simulado** (el único que existe): cotización t2 → latencia → t3, slippage, MEV, comisiones |
| `src/copybot/sim/costs.py` | Comisión de red y prioritaria, renta de la cuenta de token, recorte por MEV, comisión de transferencia Token-2022 |
| `src/copybot/sim/safety.py` | **Filtros de seguridad** del token (cuenta on-chain) y del mercado (Jupiter) |
| `src/copybot/sim/risk.py` | Límites de riesgo (funciones puras) |
| `src/copybot/sim/quotes.py`, `market.py` | Cotizaciones de Jupiter (solo lectura), precio de SOL, liquidez y cuenta del token |
| `src/copybot/sim/report.py` | **Informe**: resultados de los dos libros, rechazos y fallos agrupados, comparaciones y resultados por trader |
| `src/copybot/sim_cli.py` | `copybot sim run | status | report | pause | resume | stop` |
| `src/copybot/monitor_runner.py` | El monitor de la Fase 2, reutilizable dentro de `sim run`; su latido indica si el WebSocket está conectado |
| `migrations/versions/…_fase_4_simulacion.py` | Tablas nuevas (apartado 2.7) |

### 2.1 Cómo decide, paso a paso

Para cada **compra** de un trader seguido (detectada en vivo y clasificada con confianza alta):

1. **Frescura.** Si la compra es de hace más de 60 s (por su hora de bloque), la oportunidad ya pasó: se descarta. Así quedan fuera las recuperadas tras un corte.
2. **Datos coherentes.** Se rechaza si falta información esencial: cuenta del token ilegible, sin precio de SOL reciente (menos de 10 min), Jupiter sin datos.
3. **Token** (cuenta on-chain, en caché 30 min). Se rechaza si el creador puede emitir más tokens o congelar saldos, si tiene extensiones de Token-2022 que permiten bloquear o quitar los tokens (hook de transferencia, delegado permanente, intransferible, pausa, cuentas congeladas por defecto), si cobra más de un 1 % por transferencia o si el creador puede subir esa comisión.
4. **Estado del sistema.** La cartera solo compra en `RUNNING`. El libro de señales también en `HALTED_RISK`, para seguir midiendo.
5. **Mercado** (Jupiter). Liquidez ≥ 20.000 $, impacto de nuestra compra ≤ 3 % y pérdida de comprar y vender a la vez ≤ 10 % (detecta tokens que no se pueden vender).
6. **Riesgo y reserva** (solo la cartera), en **una sola transacción** con la caja bloqueada. Con el perfil agresivo actual (apartado 2.6) se exige:
   - pérdida del día < 20 %;
   - caída desde el máximo < 50 %;
   - como mucho 8 posiciones (las compras en curso cuentan) y una por token;
   - un 10 % del capital siempre en reserva.

   Si pasa, se crea la orden y se reservan los 25 $ (con el perfil anterior: 10 %, 25 %, 5 posiciones, 20 % y 10 $).
7. **Ejecución simulada** (apartado 2.2).

Para cada **venta** del trader: si tenemos su posición copiada, vendemos **la misma fracción** que él (calculada con los saldos de su propia transacción), y todo si deja menos de un 2 %. Además, cada minuto se valora cada posición con una cotización de venta real y se vende si **pierde un 50 %** o lleva **48 h** abierta. Las ventas que fallan se reintentan, cada vez más espaciadas. Las salidas funcionan también en pausa, en solo lectura y tras una parada por riesgo: reducen riesgo.

Cada decisión, también los rechazos, queda guardada con sus motivos y los datos con que se tomó.

### 2.2 Ejecución simulada (apartado 6.1 de la Fase 1)

1. **t2**: cotización de Jupiter para **nuestro** tamaño. El tamaño de la posición (25 $; 10 $ con el perfil anterior) lo incluye todo: se compra por ese importe menos la comisión de red y la renta de la cuenta de token.
2. **Latencia**: se **espera de verdad** 1,5 s (configurable).
3. **t3**: se vuelve a cotizar. Si t3 queda más de un 3 % por debajo de t2 (el mínimo que acepta la transacción), la orden **falla** como fallaría on-chain y **se paga la comisión**. Si no falla, se aplica un recorte supuesto del 0,5 % por MEV (*sandwich*), pero sin bajar nunca de ese mínimo: si el atacante se llevara más, la transacción fallaría y no ganaría nada. Si Jupiter no responde en t3, la transacción ya se habría enviado: falla y paga la comisión.
4. Se descuenta la comisión de transferencia del token, si la tiene.
5. **Costes registrados**: comisión base (5.000 lamports) y prioritaria (100.000), renta de la cuenta de token (≈1,49 M lamports, se recupera al cerrar) y el impacto y la comisión de ruta, que van dentro de la cotización.

Las ventas siguen el mismo proceso. Sin ruta de venta, la posición se marca **invendible**, se valora en **0** y se reintenta.

### 2.3 Dos libros y comparaciones

- **Cartera (100 $)**: lo que habría pasado de verdad, con todos los límites.
- **Libro de señales**: copia **cada** compra con el mismo tamaño que la cartera (25 $ por defecto), sin límite de capital ni de posiciones. Sirve para tener muestra suficiente con la que medir a cada trader aunque la cartera esté llena o parada.
- **Comparaciones** (`copybot sim report`): copia realista frente a **copia teórica** (las mismas operaciones al precio exacto del trader y sin costes, sobre las mismas posiciones), **no operar** y **mantener SOL**. Por trader, resultado real frente a teórico. La diferencia es lo que se come la ejecución.
- **Estadística**: resultado realizado y sin realizar, acierto, profit factor, comisiones por operación y **aviso de muestra insuficiente** (menos de 30 operaciones con resultado; aviso más suave por debajo de 100). Las posiciones invendibles cuentan como pérdida total, para no caer en el sesgo de supervivencia.

### 2.4 Estados del sistema

| Estado | Entradas de la cartera | Entradas del libro de señales | Salidas | Cómo se entra y se sale |
|---|---|---|---|---|
| `RECOVERING` | No | No | — | Al arrancar: las órdenes a medias se dan por no ejecutadas |
| `RUNNING` | Sí | Sí | Sí | En cuanto el monitor da datos, o con `copybot sim resume` |
| `PAUSED` | No | No | Sí | `copybot sim pause` |
| `READ_ONLY` | No | No | Sí | Al arrancar, hasta que el monitor da datos. Automático si deja de latir (30 s) o su WebSocket se desconecta. Vuelve a `RUNNING` solo |
| `HALTED_RISK` | No | Sí | Sí | Automático al llegar al límite de pérdida diaria o de caída máxima. Solo se sale con `copybot sim resume`, que se niega mientras se superen los límites, o con `stop` + `run`, que vuelve a `HALTED_RISK` si se siguen superando. Reiniciar sin más lo mantiene |
| `STOPPED` | — | — | — | `copybot sim stop`, o al terminar `sim run` (si estaba en `PAUSED` o `HALTED_RISK`, ese estado se conserva) |

### 2.5 Garantías

Las 1, 3 y 5 las impone la base de datos (claves únicas, restricciones `CHECK` y bloqueo); la 2, sus transacciones; la 4 y la 6, el código del almacén; la 7, un test.

1. **Sin duplicados.** Cada orden lleva una clave de idempotencia única (versión de la decisión, libro, señal, propósito). Repetir una decisión o reiniciar no crea otra orden ni reserva otra vez el dinero. Cada reintento de una venta fallida es, a propósito, una orden nueva con su propia clave.
2. **Atomicidad.** Estado del sistema (bloqueado para lectura), límites de riesgo, orden y reserva de efectivo se resuelven en la misma transacción, con la caja bloqueada. Dos señales simultáneas no pueden superar los límites (probado con 8 a la vez).
3. **Sin saldos negativos ni ventas de más.** Restricciones `CHECK`: efectivo y reserva ≥ 0, cantidades ≥ 0, una posición cerrada está vacía. Una venta nunca supera lo que hay.
4. **Doble entrada exacta.** Los apuntes de cada operación suman cero (el almacén se niega a guardar un asiento descuadrado), redondeados a la escala de las columnas antes de guardarse. Mientras una posición está abierta, sus apuntes suman su coste más la renta; al cerrarla, cero.
5. **Un solo motor a la vez** (bloqueo de PostgreSQL, comprobado en cada vuelta).
6. **Matar el proceso a mitad de una orden** no duplica nada: al arrancar, la orden se da por no ejecutada, se libera la reserva y la señal no se vuelve a decidir.
7. **Solo simulación.** No hay claves, firma ni envío. El test de «solo simulación» bloquea en el código las dependencias y los símbolos habituales para firmar o enviar transacciones, y los endpoints de Jupiter que las construyen.

### 2.6 Perfil de riesgo agresivo (decidido el 20-oct)

Pediste que el bot asuma **más riesgo si eso da más beneficio**, y elegiste subir ya los topes de la simulación. Los nuevos topes están fijados en el código y, como antes, desde la configuración solo se pueden endurecer:

| Límite | Fase 1 | Ahora (por defecto) |
|---|---|---|
| Capital virtual | 100 $ | 100 $ |
| Tamaño por posición | 10 $ | **25 $** (también en el libro de señales) |
| Posiciones abiertas a la vez | 5 | **8** |
| Reserva de efectivo | 20 % | **10 %** |
| Pérdida diaria que para la cartera | 10 % | **20 %** |
| Caída máxima que para la cartera | 25 % | **50 %** |

Con 100 $, posiciones de 25 $ y un 10 % de reserva, **caben como mucho 3 posiciones a la vez**: el límite de 8 solo cuenta con posiciones más pequeñas. Más tamaño por operación también significa más impacto en el precio y más coste al copiar en tokens con poca liquidez.

Ten en cuenta que **más riesgo amplifica el resultado en los dos sentidos**: con un trader con ventaja real, más ganancia; con uno sin ella, que son la gran mayoría según la Fase 3 y la leaderboard, se pierde más deprisa. El backtester (Fase 5) comparará este perfil con el anterior con los mismos traders y costes. El dinero real sigue exigiendo los criterios de la Fase 1, apartado 12; si se llega a la Fase 9, estos topes se revisarán con los resultados.

### 2.7 Cambios en la base de datos

| Tabla | Contenido |
|---|---|
| `control_state` | Estado del sistema (una sola fila), motivo, quién y cuándo |
| `signals` | Una por compraventa de un trader seguido; precio del trader y fracción vendida |
| `sim_orders` | Cada decisión: libro, propósito (entrada, copia de venta, stop-loss, tiempo máximo), estado, motivo y datos de la decisión; clave de idempotencia única |
| `sim_fills` | Ejecución simulada de cada orden (tokens, USD, comisiones, cotizaciones t2/t3 y ruta) |
| `sim_positions` | Posiciones de los dos libros, con su copia teórica y su última valoración |
| `book_cash` | Efectivo, reserva, máximo y patrimonio al empezar el día de la cartera |
| `ledger_entries` | Diario de doble entrada |
| `portfolio_snapshots`, `risk_events`, `token_safety_checks` | Historia del patrimonio (con el precio de SOL), paradas por riesgo y filtros de cada token |

## 3. Pruebas ejecutadas

```
$ uv run pytest
276 passed

$ uv run ruff check src tests scripts migrations
All checks passed!
```

Los tests que usan base de datos se ejecutan contra PostgreSQL 16 real. Los del motor usan un mercado de mentira con precios controlados, para poder comprobar cada cifra.

| Criterio de la Fase 1 | Tests |
|---|---|
| **PnL y costes** | Una compra con todos los costes calculados a mano (comisión de red y prioritaria, renta, impacto, MEV) y el efectivo que queda. Venta proporcional y cierre con la renta devuelta. Resultado realizado exacto. Diario cuadrado incluso con precios de SOL de muchos decimales |
| **Slippage** | Si el precio se mueve más de un 3 % durante la latencia, la orden falla y paga la comisión. El MEV se lleva como mucho hasta el mínimo de la transacción y por sí solo nunca la hace fallar. Si Jupiter no responde en t3, la compra o la venta falla y paga |
| **Tamaño de posición** | El tamaño incluye comisión y renta. Redondeo hacia abajo. Con el perfil agresivo, 25 $ por posición y como mucho 3 a la vez por la reserva del 10 % |
| **Stops** | Stop-loss al 50 %, venta a las 48 h. El stop-loss sigue vigilando aunque haya fallado una venta parcial copiada |
| **Caída máxima y pérdida diaria (pausa automática)** | Al superar el límite, `HALTED_RISK`: la cartera deja de comprar y el libro de señales sigue. Ni forzar `RUNNING`, ni `stop` + `run`, ni reiniciar se lo saltan. Probado con el perfil anterior y con el agresivo (una pérdida del 12 % ya no para; una del 45 % sí) |
| **Datos insuficientes o incoherentes** | Señal vieja, recuperada sin hora de bloque o en la que el trader no pagó nada; token ilegible; sin precio de SOL (o de hace más de 10 min); Jupiter caído o con respuestas raras: se rechaza con su motivo |
| **Saldos no negativos** | La base de datos rechaza un efectivo negativo (restricción `CHECK`) |
| **Sin ventas superiores a la posición** | Pedir vender 10 veces lo que hay solo vende lo que hay y cierra |
| ***Kill* a mitad de una orden** | El proceso muere durante la latencia con las dos órdenes en curso. Al reiniciar se dan por no ejecutadas, se libera la reserva y la señal no se vuelve a decidir: una orden por libro, sin duplicados |
| Idempotencia y concurrencia | Repetir una decisión no crea otra orden. 8 señales a la vez respetan las 5 posiciones. Un solo motor a la vez |
| Filtros de seguridad | 3 tokens reales (fixtures de la cuenta on-chain) y cada extensión peligrosa. Comisión del 0 % que el creador puede subir. Hook sin programa con autoridad |
| Estados | Pausa (sin entradas, con salidas), solo lectura sin latidos o con el WebSocket caído (y vuelta), token invendible valorado a 0 y reintentado |
| Integración con Jupiter | Respuestas reales grabadas el 19-oct (cotización de ida y vuelta de Kimchi, precio y liquidez) |
| Configuración | Límites y parámetros desde `.env`. Una errata en el nombre es un error. Desde `.env` tampoco se pueden relajar los topes |
| Informe | Copia realista frente a teórica sobre las mismas posiciones, aviso de muestra insuficiente |

## 4. Revisión adversarial del código

Antes de cerrar la fase pedí una revisión independiente con varios agentes. Cada uno revisó una dimensión: contabilidad, concurrencia y reinicios, realismo de la simulación, y límites de riesgo con la garantía de solo simulación. Después, otro agente intentó **refutar** cada hallazgo leyendo el código. Se confirmaron **34** (2 de gravedad alta, 13 media y 19 baja, según los verificadores) y se refutaron 3. Se corrigieron todos. Los más importantes:

| Gravedad | Problema | Corrección (con test) |
|---|---|---|
| Alta | Con precios de SOL con muchos decimales (como los de Jupiter), un cierre podía fallar por «apuntes descuadrados» o guardarse descuadrado, porque PostgreSQL redondea cada apunte por separado | Todos los importes se redondean a 18 decimales antes de apuntarlos y el resultado cuadra el asiento |
| Alta | Una respuesta rara de Jupiter (una página HTML de un cortafuegos, un campo que falta) dejaba la orden a medias con 10 $ reservados y podía parar el motor | Siempre es un error de cotización; ninguna excepción deja órdenes a medias y cada fase del bucle va protegida |
| Media | Tras una venta parcial, el valor de la posición seguía siendo el de antes: el patrimonio contaba dos veces lo vendido y podía inflar el máximo (y disparar después paradas falsas) | El valor se ajusta en proporción |
| Media | `resume` desde una pausa, o `stop` seguido de `run`, podían volver a comprar con la caída máxima superada | La pérdida diaria y la caída se comprueban dentro de la propia reserva: ningún camino se las salta |
| Media | El informe solo contaba las posiciones cerradas: las invendibles (las peores) no aparecían (sesgo de supervivencia); las comisiones de las órdenes fallidas no restaban; la copia teórica se comparaba con un conjunto distinto de posiciones | Invendibles como pérdida total, comisiones fallidas restadas, comparación sobre las mismas posiciones |
| Media | Un segundo `sim run` daba por fallidas las órdenes en curso del primero | Bloqueo para un solo motor |
| Media | El libro de señales, sin límite, podía retrasar la valoración (y el stop-loss) de la cartera | La cartera se valora primero |
| Media | Los límites endurecidos en `.env` (`COPYBOT_RISK_*`) se ignoraban | Se leen de `.env`; una errata en el nombre es un error |
| Media | Pasaba un token con comisión de transferencia del 0 % que el creador puede subir, y un hook de transferencia que el creador puede activar más tarde | Se rechazan |
| Baja | Varios detalles de realismo: el MEV no contaba para el límite de slippage, la venta sin respuesta de Jupiter en t3 no pagaba comisión, la fracción vendida por el trader podía calcularse con un historial incompleto, los reintentos vendían la copia teórica a nuestro precio… | Corregidos uno a uno |

**Segunda revisión.** Después repetí la revisión solo sobre esas correcciones, por si alguna había introducido un fallo nuevo. Se confirmaron **11** problemas (5 de gravedad media y 6 baja) y se refutaron 3. Todos están corregidos, con su test:

- el motor aguanta un corte breve de PostgreSQL y reintenta, cada vez más espaciado;
- las órdenes que un error deja a medias se recuperan en la vuelta siguiente, no solo al reiniciar;
- el bloqueo de un solo motor detecta si se ha perdido;
- al arrancar se espera en solo lectura hasta que el monitor da datos, sin un aviso falso;
- el MEV se lleva como mucho hasta el mínimo de la transacción: antes, con una tolerancia menor que el MEV, fallaban todas las órdenes;
- el redondeo ya no falla con tokens muy baratos;
- en el informe, una posición invendible que ya había ganado cuenta como acierto, y cada comisión fallida se atribuye a su posición;
- una variable `COPYBOT_*` desconocida, por ejemplo con una errata, impide arrancar.

Lo que queda abierto está en el apartado 8.

## 5. Prueba en vivo con datos reales

### 5.1 Con la configuración aprobada (19-oct, 18:28-20:28 UTC)

`copybot sim run --minutes 120`, con el código ya corregido, siguiendo 19 wallets:

- `A9aqAHUX…`, el candidato de la Fase 3, y otra wallet puntuada en esa fase;
- los cuatro traders de la leaderboard con ventaja (@nekomayu, @OinkersRUs, @kecoseh, @Washedupke3d);
- seis que tienen Kimchi;
- siete de los mejores de las últimas 24 h.

| Medida | Resultado |
|---|---|
| Compraventas detectadas en vivo | 145 transacciones; 53 señales (40 compras y 13 ventas) |
| **Latencia de detección** (bloque → señal) | **mediana 1,0 s, percentil 95 1,5 s** |
| Huecos recuperados / errores del proveedor | 2 transacciones recuperadas, 0 errores; 4 desconexiones del WebSocket, todas recuperadas solas |
| Compras copiadas | **0**: las 40 se rechazaron por los filtros (abajo) |
| Ventas | 13, ninguna con posición copiada |
| Estado | `RUNNING` todo el tiempo, salvo unos segundos en `READ_ONLY` al arrancar, mientras el WebSocket se conectaba (comportamiento esperado) |
| Registros del patrimonio | 119, uno por minuto, con el precio de SOL |

**¿Por qué no se copió nada?**

- **30 de las 40 compras eran del mismo token**: *Elon Coin* (`B2GESk…`), recomprado una y otra vez por @surgz (23), @bigbabba (4) e @Iri0o (3). Cobra un 1 % por transferencia y su creador conserva la autoridad para cambiarlo, así que el filtro lo rechaza.
- **De las otras 10**, 9 eran de tokens con 4.600-19.700 $ de liquidez (por debajo del mínimo de 20.000 $) y 1 cobraba un 3 % por transferencia.

Los filtros funcionaron como están definidos, pero con estos traders son lo que decide. Lo he comprobado en la cadena: en Token-2022, si el creador sube la comisión, el cambio no entra en vigor hasta **dos épocas después** (unos 4-5 días) y queda visible en la propia cuenta como «nueva comisión». Como nuestras posiciones duran como mucho 48 h y el filtro ya mira la comisión nueva programada, **la regla «el creador puede subir la comisión» se podría relajar sin riesgo real**. Lo dejo como decisión tuya (apartado 9).

**Interrupción.** La prueba duró sus 120 minutos, pero la sesión de trabajo se congeló al llegar al límite de uso justo al terminar. El proceso no llegó a marcar `STOPPED` y quedó en `RUNNING`. Es justo el caso que cubre la recuperación al arrancar. Una prueba de 1 minuto repetida después sale limpia en 62 s y deja `STOPPED`.

### 5.2 Prueba de ejecución con filtros relajados (20-oct, 07:05-08:05 UTC)

Su objetivo era ver funcionar la parte de compras, valoración y contabilidad con cotizaciones reales, que con los filtros estrictos no llegó a usarse. Se hizo en **una base de datos aparte** (`copybot_relajada`), con dos filtros relajados solo para esta prueba:

- liquidez mínima de 5.000 $;
- aceptar tokens cuya comisión de transferencia el creador puede cambiar.

La configuración aprobada no cambia. Se siguieron 30 traders (los 19 anteriores y los 11 más activos de las fichas públicas), a una hora de poca actividad.

| Medida | Resultado |
|---|---|
| Señales | 9 (5 compras y 4 ventas) en 60 minutos |
| Compras copiadas | **5 ejecuciones**: 2 en la cartera y 3 en el libro de señales |
| Rechazos | 2 compras por impacto > 3 % (3,16 % y 4,39 %); 1 por tener ya posición en el token; 4 ventas sin posición copiada |
| Tiempos desde el bloque del trader | Empieza a evaluar la señal a los **2,0-3,5 s** (media 2,5 s); **decide** (crea la orden) a los **3,9-4,4 s**; la compra simulada **termina a los 5,5-6,0 s**. Los filtros y las cotizaciones de Jupiter (limitadas a 0,9 por segundo) se llevan unos 2 s |
| De la cotización t2 a la t3 | 1,95-2,40 s (1,5 s de latencia simulada y el tiempo de las cotizaciones) |
| Precio conseguido | 0,001813 $ por token frente a 0,001820 $ del trader (*Elon Coin*): prácticamente el mismo |
| Coste de comprar y vender a la vez (ese token) | **6,4 %**: impacto, rutas de tres saltos y el 1 % de comisión de transferencia en cada sentido |
| Resultado al cerrar la prueba | Cartera 100,81 $ (+0,8 %, sin realizar); libro de señales +1,55 $ sin realizar. Mantener SOL: −0,63 $ |
| Cierre | Limpio: estado `STOPPED` a las 08:05:16 y salida con código 0 |

Las rutas reales de Jupiter fueron de tres saltos en *Elon Coin* (Kipseli/BisonFi → Meteora DLMM → Raydium CP) y de dos en el token de las 08:05 (Kipseli → Pump.fun Amm). Al cerrar la prueba quedan abiertas dos posiciones en cada libro: *Elon Coin* (de @surgz) y un token comprado a las 08:05 por la wallet `GYQ4…` de la Fase 3. Con una hora de prueba no hubo ventas copiadas. Las ventas, el stop-loss y el tiempo máximo están probados con tests, pero falta verlos en una prueba larga (Fase 8).

**Conclusión de la prueba:** la cadena completa funciona con datos reales:

1. detección en vivo;
2. señal;
3. filtros del token y del mercado;
4. límites de riesgo;
5. cotización t2, latencia y cotización t3;
6. compra con comisión y renta;
7. diario;
8. valoración cada minuto;
9. informe y parada ordenada.

Desde la operación del trader, decide en unos 4 s y termina la compra simulada a los 6 s. Se puede acelerar pidiendo las cotizaciones en paralelo; con el límite gratuito de Jupiter, de 1 por segundo, no mucho. **El coste real de copiar es alto en estos tokens**: un 6,4 % de ida y vuelta en el único token con varias operaciones. Cada operación copiada necesita subir más que eso para ganar dinero.

## 6. Leaderboard de FOMO y Kimchi

### 6.1 De dónde salen las wallets

Con tu permiso para usar páginas de terceros:

- **fomoapi.io**: su top 100 histórico de FOMO (99 con wallet de Solana) y **293 fichas públicas de traders** con su wallet, PnL, operaciones y posiciones principales. Las fichas se descargaron una vez, a una petición por segundo. En total, **308 wallets distintas**.
- **fomolens.app** permite buscar la wallet de un usuario por su nombre (hasta 50 búsquedas al día). No ha hecho falta.
- **Los clanes** que me enviaste no se pueden leer: la página solo funciona con la sesión de FOMO iniciada y su API (`prod-api.fomo.family`) no está entre los dominios permitidos. No voy a usar tu sesión. Si me mandas capturas de la lista de miembros, saco sus wallets.

Comprobación: las wallets de estas fichas son las que firman como trader en las transacciones del relayer de FOMO. Tres del top 100 ya aparecían en nuestro muestreo de la Fase 3.

### 6.2 Resultado del análisis (30 días, a 19-oct 18:36 UTC)

| Origen | Traders | Puntuables | Con ventaja tras el coste de copiar | Rentabilidad mediana por operación (prudente) | % en positivo | Resultado conjunto |
|---|---|---|---|---|---|---|
| Leaderboard (fomoapi.io) | 308 | 57 | **4** | −14,2 % (−20,1 % contando los 308) | 12,3 % (16,2 %) | −161.826 $ sobre 22,2 M$ (−3,59 M$ sobre 53,7 M$) |
| Relayer (Fase 3) | 220 | 173 | **1** | −13,8 % (−14,1 %) | 2,9 % (4,1 %) | −289.720 $ sobre 3,1 M$ (−395.941 $ sobre 4,56 M$) |

Las tres últimas columnas son de los puntuables; entre paréntesis, de todos.

- **La leaderboard tampoco gana como grupo en nuestro periodo.** El PnL «de todos los tiempos» que muestra FOMO viene sobre todo de meses anteriores y de posiciones aún sin vender. En los últimos 30 días, operación a operación, la mediana de los puntuables pierde un 14 %, y la de los 308 un 20 %.
- **251 no se pueden puntuar:**
  - 154 cierran menos de 20 operaciones en USD en 30 días (muchos operan en las cadenas EVM de FOMO, que no vemos, o mantienen posiciones largas);
  - 89 operan tanto que su historial supera el máximo de 10.000 transacciones;
  - 7 tienen datos incompletos;
  - 1 vende demasiado rápido para copiarle.
- **Coste:** 169.680 créditos de Helius para importar los 307 historiales y poner al día los de la Fase 3 (estimación con la tarifa contrastada en la Fase 3).

### 6.3 Los cinco con ventaja

| # | Trader | Puntos | Operaciones | Acierto | Rentabilidad mediana | Media prudente | Primera venta (mediana) | Resultado 30 días |
|---|---|---|---|---|---|---|---|---|
| 1 | `A9aqAHUX…` (relayer, Fase 3) | 77,2 | 33 | 70 % | +11,9 % | +23,7 % | 4,3 min | +61 $ (opera muy poco dinero) |
| 2 | **@nekomayu** `CZ7gnaUz…` | 70,2 | 145 | 74 % | +29,9 % | +54,5 % | 4,9 min | +38.549 $ |
| 3 | **@OinkersRUs** `DPAN2Vig…` | 62,9 | 118 | 80 % | +29,4 % | +40,8 % | 1,8 min | +30.388 $ |
| 4 | @kecoseh `sbsTwovW…` | 34,1 | 37 | 54 % | +4,1 % | +17,0 % | 12,9 min | +44.647 $ |
| 5 | @Washedupke3d `9pGcr4S4…` | 32,7 | 37 | 43 % | −4,8 % | +21,8 % | 14,0 min | +134.085 $ (79 operaciones en SOL fuera del cálculo) |

Con reservas:

- **A9aqAHUX sigue bien un día después** (de 55 a 77 puntos), pero opera muy poco dinero y una sola operación aporta el 63 % de lo ganado.
- **@nekomayu y @OinkersRUs** ganan mucho y muy deprisa: en 2-5 minutos, con aciertos del 74-80 %. En FOMO eso encaja con un patrón conocido: el trader compra, sus seguidores lo copian y suben el precio, y él vende sobre esas compras. **Quien copia con retraso compra más caro y sirve de salida.** Es justo lo que mide el simulador (copia realista frente a teórica), y por eso están en la lista de la prueba en vivo.
- Con 230 candidatos puntuables, que unos pocos salgan bien puede ser azar. **Solo cuenta si siguen siendo buenos en las próximas semanas, copiados de verdad** (Fase 8).

### 6.4 Kimchi

El enlace que me pasaste es la moneda **«Aura Final Boss» ($Kimchi)**, `Am8iBffq…pump`: creada en pump.fun el 27-jul-2026, con 4,1 M$ de capitalización, 124.000 $ de liquidez y 5.464 holders. Es decir, **es un token, no un trader**.

- **Quién gana con ella.** Seis fichas de la leaderboard la tienen entre sus posiciones principales, con ganancias sin realizar de +11.000 a +44.000 $: @oznek, @elle, @GeofferiChalmers, @bizzxbt, @macdegods y @OmakaseOnly. @pennylane la compró el 18-oct por unos 5.000 $. **Los que más ganan la compraron hace tiempo y la mantienen.** Por ejemplo, @oznek no la ha comprado en los últimos 23 días. Ese movimiento ya pasó y no se puede copiar ahora.
- **Quién la ha operado hace poco.** Entre el 15 y el 18 de octubre, 14 de las wallets importadas la compraron, algunas con 10.000-25.000 $. **Entre las que ya vendieron, los resultados son dispares**:
  - perdieron @rasmr (25.000 $ → 21.700 $) y @nekomayu (15.000 $ → 4.795 $);
  - ganaron @elle, que cobró 9.543 $ vendiendo una cuarta parte de lo que compró por 10.500 $, y @macdegods, que ha cobrado 9.575 $ de 11.500 $ y aún conserva un tercio.

  Otras siguen dentro, como @OmakaseOnly con 21.000 $.
- **¿Se podría copiar hoy?** Su mercado pasa nuestros filtros: con la cotización real grabada como fixture, comprar y vender 9,60 $ cuesta un 1,3 %. Así que, si un trader seguido la compra, el simulador la copiaría. Los traders que la tienen están en la lista de la prueba en vivo.

## 7. Coste

| Concepto | Créditos de Helius |
|---|---|
| Importar los 307 historiales de la leaderboard y poner al día los 220 de la Fase 3 | 169.680 |
| Prueba en vivo (monitor, cuentas de tokens) | ≈ 300 por hora con 15-19 wallets (837 en 2 h 42 min) |
| **Este mes en total** (real del panel hasta el 18-oct + estimación de hoy) | **≈ 260.000 de 1.000.000** |

Jupiter (cotizaciones y precios) y fomoapi.io no han costado nada. Si la simulación corriera todo el mes con unas 20 wallets, el monitor gastaría unos 200.000-240.000 créditos: cabe en el plan gratuito.

## 8. Limitaciones y problemas abiertos

1. **Los filtros deciden casi todo.** Con la configuración aprobada no se copió ninguna de 40 compras reales (apartado 5.1). Es correcto según las reglas, pero así no hay muestra. Decidido contigo: en la interfaz (Fase 6) podrás elegir la liquidez mínima (desde 5.000 $) y si se aceptan tokens con comisión cambiable. Los límites de riesgo seguirán fijos.
2. **Aún no hay ninguna operación simulada en vivo con resultado.** La ejecución está probada con tests sobre cotizaciones simuladas; la lectura de Jupiter y los filtros de mercado, con respuestas reales grabadas; y la cadena completa, en la prueba en vivo de una hora (apartado 5.2). Falta verla funcionar durante días con dinero simulado (Fase 8).
3. **La prueba en vivo depende de que el proceso esté encendido.** En este entorno de la nube se corta al terminar la sesión. Una prueba de una semana necesita un servidor o un ordenador siempre encendido (Fase 7).
4. **Valoración congelada si Jupiter falla mucho rato.** La posición conserva su última valoración buena, y el registro del patrimonio guarda su antigüedad. No se pasa a 0 automáticamente para no provocar paradas falsas durante una caída de Jupiter.
5. **La copia teórica solo existe para las compras que llegamos a ejecutar.** No mide lo que se pierde por las compras que fallan o se rechazan. Por eso, el «coste de llegar tarde» está algo subestimado.
6. **MEV fijo del 0,5 %.** Es un supuesto. Un *sandwich* real puede llevarse hasta toda la tolerancia de slippage (3 %). Conviene probar también con un supuesto más pesimista.
7. **Comisión prioritaria fija** (100.000 lamports). En momentos de congestión haría falta más para entrar a tiempo.
8. **Solo Solana.** FOMO también opera en cadenas EVM (Base, BSC, Robinhood Chain…) y muchos de sus mejores traders operan allí. No se ven.
9. **Ventas parciales por impacto.** Si una venta grande movería mucho el precio, ahora se vende entera o falla. No se trocea.
10. **Muchas comparaciones.** Entre 230 traders puntuables, que unos pocos salgan bien puede ser azar. Solo cuenta si siguen bien en las semanas siguientes.

## 9. Conclusión y siguiente paso

**El motor de simulación está terminado, probado y revisado.** Cumple los criterios de aceptación de la Fase 4 de la Fase 1: PnL, costes, slippage, tamaño, stops, caída máxima, pausa automática, rechazo de datos insuficientes, saldos no negativos, sin ventas de más y sin duplicados tras matar el proceso. Detecta en vivo las operaciones de los traders reales en algo más de un segundo y decide en unos 4 s.

**Sobre la rentabilidad todavía no se puede concluir nada**, y los datos que tenemos no invitan al optimismo:

- La gran mayoría de traders de FOMO pierde operación a operación, también los de la leaderboard en los últimos 30 días.
- Solo 5 de 230 tienen ventaja, y los dos más fuertes ganan en 2-5 minutos, justo el patrón en el que copiar con retraso suele salir caro.
- Con los filtros estrictos no se copió ninguna operación en dos horas.

### Lo que necesito de ti

1. **Tu aprobación de la Fase 4**, o los cambios que quieras.
2. **Siguiente fase:** seguimos el orden previsto, como has decidido. La **Fase 5 (backtester)** reproduce con los historiales ya importados qué habría pasado copiando a cada trader, con la latencia y los costes medidos aquí. Es barata (no gasta créditos) y responde en horas lo que la prueba en vivo tardaría semanas en decir: si la ventaja de @nekomayu, @OinkersRUs o A9aqAHUX sobrevive al retraso de copiar.
3. **Anotado para la Fase 6 (interfaz):**
   - un **modo prueba** que lances tú, con la duración que elijas (por ejemplo, 7 días);
   - los **filtros ajustables en pantalla**;
   - el **semáforo «¿Es rentable?»** con los criterios del apartado 12 de la Fase 1.

   Con una semana el semáforo casi seguro saldrá en gris (muestra insuficiente): para usar dinero real hacen falta al menos 28 días y 100 operaciones cerradas con ventaja demostrada.
4. **Más adelante: dónde funcionará el bot** las 24 h (un servidor de 5-10 €/mes o tu ordenador), antes de la Fase 7.
5. **Los clanes:** si quieres que los analice, mándame capturas de la lista de miembros de cada uno.

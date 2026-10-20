# Informe de la Fase 4: motor de simulación, filtros de seguridad y gestión de riesgo

- **Fecha:** 19 de octubre de 2026
- **Estado:** pendiente de tu aprobación.
- **Rama:** `claude/trusting-fermat-jangmb` (sobre la Fase 3).

## 1. Resumen

<!-- RESUMEN -->

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
| `migrations/versions/…_fase_4_simulacion.py` | Tablas nuevas (apartado 2.3) |

### 2.1 Cómo decide, paso a paso

Para cada **compra** de un trader seguido (detectada en vivo y clasificada con confianza alta):

1. **Frescura.** Si la compra es de hace más de 60 s (por su hora de bloque), la oportunidad ya pasó: se descarta. Así quedan fuera las recuperadas tras un corte.
2. **Datos coherentes.** Se rechaza si falta información esencial: cuenta del token ilegible, sin precio de SOL reciente (menos de 10 min), Jupiter sin datos.
3. **Token** (cuenta on-chain, en caché 30 min). Se rechaza si el creador puede emitir más tokens o congelar saldos, si tiene extensiones de Token-2022 que permiten bloquear o quitar los tokens (hook de transferencia, delegado permanente, intransferible, pausa, cuentas congeladas por defecto), si cobra más de un 1 % por transferencia o si el creador puede subir esa comisión.
4. **Estado del sistema.** La cartera solo compra en `RUNNING`. El libro de señales también en `HALTED_RISK`, para seguir midiendo.
5. **Mercado** (Jupiter). Liquidez ≥ 20.000 $, impacto de nuestra compra ≤ 3 % y pérdida de comprar y vender a la vez ≤ 10 % (detecta tokens que no se pueden vender).
6. **Riesgo y reserva** (solo la cartera), en **una sola transacción** con la caja bloqueada: pérdida del día < 10 %, caída desde el máximo < 25 %, como mucho 5 posiciones (las compras en curso cuentan), una por token y un 20 % del capital siempre en reserva. Si pasa, se crea la orden y se reservan los 10 $.
7. **Ejecución simulada** (apartado 2.2).

Para cada **venta** del trader: si tenemos su posición copiada, vendemos **la misma fracción** que él (calculada con los saldos de su propia transacción), y todo si deja menos de un 2 %. Además, cada minuto se valora cada posición con una cotización de venta real y se vende si **pierde un 50 %** o lleva **48 h** abierta. Las ventas que fallan se reintentan, cada vez más espaciadas. Las salidas funcionan también en pausa, en solo lectura y tras una parada por riesgo: reducen riesgo.

Cada decisión, también los rechazos, queda guardada con sus motivos y los datos con que se tomó.

### 2.2 Ejecución simulada (apartado 6.1 de la Fase 1)

1. **t2**: cotización de Jupiter para **nuestro** tamaño. Los 10 $ lo incluyen todo: se compra por 10 $ menos la comisión de red y la renta de la cuenta de token.
2. **Latencia**: se **espera de verdad** 1,5 s (configurable).
3. **t3**: se vuelve a cotizar y se aplica un recorte supuesto del 0,5 % por MEV (*sandwich*). Si lo recibido queda más de un 3 % por debajo de t2, la orden **falla** como fallaría on-chain y **se paga la comisión**. Si Jupiter no responde en t3, también falla y paga.
4. Se descuenta la comisión de transferencia del token, si la tiene.
5. **Costes registrados**: comisión base (5.000 lamports) y prioritaria (100.000), renta de la cuenta de token (≈1,49 M lamports, se recupera al cerrar) y el impacto y la comisión de ruta, que van dentro de la cotización.

Las ventas siguen el mismo proceso. Sin ruta de venta, la posición se marca **invendible**, se valora en **0** y se reintenta.

### 2.3 Dos libros y comparaciones

- **Cartera (100 $)**: lo que habría pasado de verdad, con todos los límites.
- **Libro de señales**: copia **cada** compra con 10 $, sin límite de capital ni de posiciones. Sirve para tener muestra suficiente con la que medir a cada trader aunque la cartera esté llena o parada.
- **Comparaciones** (`copybot sim report`): copia realista frente a **copia teórica** (las mismas operaciones al precio exacto del trader y sin costes, sobre las mismas posiciones), **no operar** y **mantener SOL**. Por trader, resultado real frente a teórico. La diferencia es lo que se come la ejecución.
- **Estadística**: resultado realizado y sin realizar, acierto, profit factor, comisiones por operación y **aviso de muestra insuficiente** (menos de 30 operaciones con resultado; aviso más suave por debajo de 100). Las posiciones invendibles cuentan como pérdida total, para no caer en el sesgo de supervivencia.

### 2.4 Estados del sistema

| Estado | Entradas de la cartera | Entradas del libro de señales | Salidas | Cómo se entra y se sale |
|---|---|---|---|---|
| `RECOVERING` | No | No | — | Al arrancar: las órdenes a medias se dan por no ejecutadas |
| `RUNNING` | Sí | Sí | Sí | Tras recuperar, o con `copybot sim resume` |
| `PAUSED` | No | No | Sí | `copybot sim pause` |
| `READ_ONLY` | No | No | Sí | Automático si el monitor deja de latir (30 s) o su WebSocket se desconecta; vuelve solo |
| `HALTED_RISK` | No | Sí | Sí | Automático al superar la pérdida diaria o la caída máxima. `resume` (o reiniciar) solo vuelve a `RUNNING` si ya no se superan |
| `STOPPED` | — | — | — | `copybot sim stop` o al terminar `sim run` |

### 2.5 Garantías (en la base de datos, no solo en el código)

1. **Sin duplicados.** Cada orden lleva una clave de idempotencia única (versión de la decisión, libro, señal, propósito). Repetir una decisión, reiniciar o reintentar no crea otra orden ni reserva otra vez el dinero.
2. **Atomicidad.** Estado del sistema (bloqueado para lectura), límites de riesgo, orden y reserva de efectivo se resuelven en la misma transacción, con la caja bloqueada. Dos señales simultáneas no pueden superar los límites (probado con 8 a la vez).
3. **Sin saldos negativos ni ventas de más.** Restricciones `CHECK`: efectivo y reserva ≥ 0, cantidades ≥ 0, una posición cerrada está vacía. Una venta nunca supera lo que hay.
4. **Doble entrada exacta.** Los apuntes de cada operación suman cero, redondeados a la escala de las columnas antes de guardarse. Mientras una posición está abierta, sus apuntes suman su coste más la renta; al cerrarla, cero.
5. **Un solo motor a la vez** (bloqueo de PostgreSQL).
6. **Matar el proceso a mitad de una orden** no duplica nada: al arrancar, la orden se da por no ejecutada, se libera la reserva y la señal no se vuelve a decidir.
7. **Solo simulación.** No hay claves, firma ni envío. El test de «solo simulación» prohíbe en el código cualquier forma de construir o firmar transacciones (incluidos los endpoints de Jupiter que las construyen).

### 2.6 Cambios en la base de datos

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
261 passed

$ uv run ruff check src tests scripts migrations
All checks passed!
```

Los tests que usan base de datos se ejecutan contra PostgreSQL 16 real. Los del motor usan un mercado de mentira con precios controlados, para poder comprobar cada cifra.

| Criterio de la Fase 1 | Tests |
|---|---|
| **PnL y costes** | Una compra con todos los costes calculados a mano (comisión de red y prioritaria, renta, impacto, MEV) y el efectivo que queda. Venta proporcional y cierre con la renta devuelta. Resultado realizado exacto. Diario cuadrado incluso con precios de SOL de muchos decimales |
| **Slippage** | Si el precio se mueve más de un 3 % durante la latencia, la orden falla y paga la comisión. El MEV cuenta para ese límite. Si Jupiter no responde en t3, también falla y paga |
| **Tamaño de posición** | Los 10 $ incluyen comisión y renta. Redondeo hacia abajo |
| **Stops** | Stop-loss al 50 %, venta a las 48 h. El stop-loss sigue vigilando aunque haya fallado una venta parcial copiada |
| **Caída máxima y pérdida diaria (pausa automática)** | Al caer más de un 25 %, `HALTED_RISK`: la cartera deja de comprar y el libro de señales sigue. Ni forzar `RUNNING`, ni `stop` + `run`, ni reiniciar se lo saltan |
| **Datos insuficientes o incoherentes** | Señal vieja o recuperada sin hora de bloque, token ilegible, sin precio de SOL (o de hace más de 10 min), Jupiter caído o con respuestas raras: se rechaza con su motivo |
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

Antes de cerrar la fase pedí una revisión independiente con varios agentes. Cada uno revisó una dimensión: contabilidad, concurrencia y reinicios, realismo de la simulación, y límites de riesgo con la garantía de solo simulación. Después, otro agente intentó **refutar** cada hallazgo leyendo el código. Se confirmaron **34** (2 de gravedad alta, 13 media y 19 baja) y se refutaron 3. Se corrigieron todos los que afectaban al resultado. Los más importantes:

| Gravedad | Problema | Corrección (con test) |
|---|---|---|
| Alta | Con precios de SOL con muchos decimales (como los de Jupiter), un cierre podía fallar por «apuntes descuadrados» o guardarse descuadrado, porque PostgreSQL redondea cada apunte por separado | Todos los importes se redondean a 18 decimales antes de apuntarlos y el resultado cuadra el asiento |
| Alta | Una respuesta rara de Jupiter (una página HTML de un cortafuegos, un campo que falta) dejaba la orden a medias con 10 $ reservados, o paraba el motor | Siempre es un error de cotización; ninguna excepción deja órdenes a medias y cada fase del bucle va protegida |
| Alta | Tras una venta parcial, el valor de la posición seguía siendo el de antes: el patrimonio contaba dos veces lo vendido y podía inflar el máximo (y disparar después paradas falsas) | El valor se ajusta en proporción |
| Alta | `resume` desde una pausa, o `stop` seguido de `run`, podían volver a comprar con la caída máxima superada | La pérdida diaria y la caída se comprueban dentro de la propia reserva: ningún camino se las salta |
| Media | El informe solo contaba las posiciones cerradas: las invendibles (las peores) no aparecían (sesgo de supervivencia); las comisiones de las órdenes fallidas no restaban; la copia teórica se comparaba con un conjunto distinto de posiciones | Invendibles como pérdida total, comisiones fallidas restadas, comparación sobre las mismas posiciones |
| Media | Un segundo `sim run` daba por fallidas las órdenes en curso del primero | Bloqueo para un solo motor |
| Media | El libro de señales, sin límite, podía retrasar la valoración (y el stop-loss) de la cartera | La cartera se valora primero |
| Media | Los límites endurecidos en `.env` (`COPYBOT_RISK_*`) se ignoraban | Se leen de `.env`; una errata en el nombre es un error |
| Media | Pasaba un token con comisión de transferencia del 0 % que el creador puede subir, y un hook de transferencia que el creador puede activar más tarde | Se rechazan |
| Baja | Varios detalles de realismo: el MEV no contaba para el límite de slippage, la venta sin respuesta de Jupiter en t3 no pagaba comisión, la fracción vendida por el trader podía calcularse con un historial incompleto, los reintentos vendían la copia teórica a nuestro precio… | Corregidos uno a uno |

Lo que queda abierto está en el apartado 8.

## 5. Prueba en vivo con datos reales

### 5.1 Con la configuración aprobada (19-oct, 18:28-20:28 UTC)

`copybot sim run --minutes 120`, con el código ya corregido, siguiendo 19 wallets:

- `A9aqAHUX…`, el candidato de la Fase 3, y otra wallet puntuada en esa fase;
- los cuatro traders puntuables de la leaderboard (@nekomayu, @OinkersRUs, @kecoseh, @Washedupke3d);
- seis que tienen Kimchi;
- siete de los mejores de las últimas 24 h.

| Medida | Resultado |
|---|---|
| Compraventas detectadas en vivo | 143 transacciones; 67 señales (53 compras y 14 ventas) |
| **Latencia de detección** (bloque → señal) | **mediana 1,0 s, percentil 95 1,5 s** |
| Huecos recuperados / errores del proveedor | 2 transacciones recuperadas, 0 errores; 4 desconexiones del WebSocket, todas recuperadas solas |
| Compras copiadas | **0**: las 53 se rechazaron por los filtros (abajo) |
| Ventas | 14, ninguna con posición copiada |
| Estado | `RUNNING` todo el tiempo, salvo unos segundos en `READ_ONLY` al arrancar, mientras el WebSocket se conectaba (comportamiento esperado) |
| Registros del patrimonio | 161, uno por minuto, con el precio de SOL |

**¿Por qué no se copió nada?**

- **42 de las 53 compras eran del mismo token**: *Elon Coin* (`B2GESk…`), recomprado una y otra vez por @surgz (35), @bigbabba (4) e @Iri0o (3). Cobra un 1 % por transferencia y su creador conserva la autoridad para cambiarlo, así que el filtro lo rechaza.
- **Las otras 11 eran de tokens con 4.000-19.700 $ de liquidez**, por debajo del mínimo de 20.000 $. Uno además cobraba un 3 % por transferencia.

Los filtros funcionaron como están definidos, pero con estos traders son lo que decide. Lo he comprobado en la cadena: en Token-2022, si el creador sube la comisión, el cambio no entra en vigor hasta **dos épocas después** (unos 4-5 días) y queda visible en la propia cuenta como «nueva comisión». Como nuestras posiciones duran como mucho 48 h y el filtro ya mira la comisión nueva programada, **la regla «el creador puede subir la comisión» se podría relajar sin riesgo real**. Lo dejo como decisión tuya (apartado 9).

**Interrupción.** La prueba duró sus 120 minutos, pero la sesión de trabajo se congeló al llegar al límite de uso justo al terminar. El proceso no llegó a marcar `STOPPED` y quedó en `RUNNING`. Es justo el caso que cubre la recuperación al arrancar. Una prueba de 1 minuto repetida después sale limpia en 62 s y deja `STOPPED`.

<!-- RELAJADA -->

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
| Leaderboard (fomoapi.io) | 308 | 57 | **4** | −14,2 % | 12,3 % | −161.826 $ sobre 22,2 M$ comprados |
| Relayer (Fase 3) | 220 | 173 | **1** | −13,8 % | 2,9 % | −289.720 $ sobre 3,1 M$ |

- **La leaderboard tampoco gana como grupo en nuestro periodo.** El PnL «de todos los tiempos» que muestra FOMO viene sobre todo de meses anteriores y de posiciones aún sin vender. En los últimos 30 días, operación a operación, la mediana pierde un 14 %.
- **251 no se pueden puntuar:**
  - 154 cierran menos de 20 operaciones en USD en 30 días (muchos operan en las cadenas EVM de FOMO, que no vemos, o mantienen posiciones largas);
  - 89 operan tanto que su historial supera el máximo de 10.000 transacciones;
  - 7 tienen datos incompletos;
  - 1 vende demasiado rápido para copiarle.
- **Coste:** 169.680 créditos de Helius para importar y poner al día 307 historiales (estimación con la tarifa contrastada en la Fase 3).

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

- **Quién gana con ella.** Siete fichas de la leaderboard la tienen entre sus posiciones principales, con ganancias sin realizar de +11.000 a +44.000 $: @oznek, @elle, @GeofferiChalmers, @bizzxbt, @macdegods, @OmakaseOnly y @pennylane. **Los que más ganan la compraron hace tiempo y la mantienen.** Por ejemplo, @oznek no la ha comprado en los últimos 23 días. Ese movimiento ya pasó y no se puede copiar ahora.
- **Quién la ha operado hace poco.** Entre el 15 y el 18 de octubre, 15 de las wallets importadas la compraron, algunas con 10.000-25.000 $. **Las que ya vendieron perdieron dinero**:
  - @rasmr: 25.000 $ → 21.700 $;
  - @macdegods: 11.500 $ → 9.575 $ (le queda una parte);
  - @nekomayu: 15.000 $ → 4.795 $.

  Otras siguen dentro, como @OmakaseOnly con 21.000 $.
- **¿Se podría copiar hoy?** Su mercado pasa nuestros filtros: con la cotización real grabada como fixture, comprar y vender 9,60 $ cuesta un 1,3 %. Así que, si un trader seguido la compra, el simulador la copiaría. Los traders que la tienen están en la lista de la prueba en vivo.

## 7. Coste

| Concepto | Créditos de Helius |
|---|---|
| Importar y poner al día 307 historiales de la leaderboard (más la puesta al día de los 221 de la Fase 3) | ≈ 174.500 |
| Prueba en vivo (monitor, cuentas de tokens) | ≈ 200 por hora con 15-19 wallets |
| **Este mes en total** (real del panel hasta el 18-oct + estimación de hoy) | **≈ 260.000 de 1.000.000** |

Jupiter (cotizaciones y precios) y fomoapi.io no han costado nada. Si la simulación corriera todo el mes con unas 20 wallets, el monitor gastaría unos 150.000 créditos: cabe en el plan gratuito.

## 8. Limitaciones y problemas abiertos

<!-- LIMITACIONES -->

## 9. Conclusión y siguiente paso

<!-- CONCLUSION -->

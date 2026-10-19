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

<!-- PRUEBAS -->

## 4. Revisión adversarial del código

Antes de cerrar la fase pedí una revisión independiente con varios agentes. Cada uno revisó una dimensión: contabilidad, concurrencia y reinicios, realismo de la simulación, y límites de riesgo con la garantía de solo simulación. Después, otro agente intentó **refutar** cada hallazgo leyendo el código. Se confirmaron **<!-- N_REAL -->** y se corrigieron todos los que afectaban al resultado. Los más importantes:

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

<!-- EN VIVO -->

## 6. Leaderboard de FOMO y Kimchi

<!-- LEADERBOARD -->

## 7. Coste

<!-- COSTE -->

## 8. Limitaciones y problemas abiertos

<!-- LIMITACIONES -->

## 9. Conclusión y siguiente paso

<!-- CONCLUSION -->

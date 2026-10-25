# Preregistro de la regla `cripto-v1`

- **Fecha:** 22 de octubre de 2026.
- **Qué fija:** la regla, sus 2 variantes, la tabla de costes, el protocolo del backtest y el
  semáforo, **antes** de ejecutar ningún backtest de esta regla con datos reales.
- **Qué se ha mirado antes:** solo la calidad de los datos (velas descargadas, monedas
  evaluables por día) y el libro de órdenes para calibrar los costes. Ningún resultado de la
  regla.
- **Compromiso:** el primer `copybot crypto backtest` se publica tal como salga, aunque pierda
  frente a BTC. Cambiar algo de lo que hay aquí exige una versión nueva (`cripto-v2`) con su
  propio preregistro, un K acumulado que no se reinicia y una luz en vivo que empieza de cero.
- **Diseño completo:** [`diseno.md`](diseno.md), aprobado el 21-oct-2026.

## 1. La regla

| Grupo | Parámetro | Valor |
|---|---|---|
| Universo | Pares | Binance spot contra USDT, también los ya retirados mientras cotizaban |
| | Exclusiones | Estables y divisas, versiones envueltas o con staking, oro tokenizado y tokens apalancados |
| | Antigüedad del episodio | ≥ 120 días (un hueco de más de 7 días sin negociación abre otro episodio) |
| | Calidad | Los 100 últimos días con negociación, sin huecos (salvo los globales) ni cierres ×4 o ÷4 |
| | Volatilidad mínima | σ de 60 días ≥ 0,5 % diario |
| | Liquidez | Mediana de 30 días del volumen ≥ 5 M USDT y entre las 100 primeras |
| Señal | Momentum ajustado | `ln(C(t−1)/C(t−1−L)) / (σ60 · √L)`, L = 7, 14, 30, 60 y 90 días; σ con suelo del 1 % |
| | Puntuación | Media de los percentiles por horizonte, de 0 a 100 |
| | Orden | Puntos ↓, momentum ajustado a 30 días ↓, símbolo ↑ |
| Tendencia | Medias | Cierre frente a sus medias de 20, 50 y 100 días: se entra con 2 o 3 de 3 y se sale con 1 o 0 |
| Mercado | Filtro | BTC por encima de su media de 100 días; si no, todo a USDT |
| Cartera | Posiciones | 5; se compra en los puestos 1-5 y se vende a partir del puesto 13 o fuera del universo |
| | Stop | Cierre ≤ 0,75 × precio de entrada |
| | Tamaño | min(25 $, (patrimonio − 10 $)/5): 18 $ con 100 $; sin reequilibrios parciales |
| | Paradas | Pérdida del día del 20 %: sin compras ese día (se reanuda sola). Caída del 50 %: sin compras hasta que se reanude a mano; las ventas siguen |
| Ejecución | Calendario | Decisión con la vela diaria cerrada (00:00 UTC); en el backtest, en la apertura siguiente; en vivo, al ask/bid de un `bookTicker` reciente |
| Libro de señales | Medición | Cada día, las 5 primeras con tendencia, 10 $ cada una durante 7 días, frente a BTC en la misma ventana |

## 2. Variantes (solo informativas; ninguna sustituye a la regla)

| Código | Cambio |
|---|---|
| V1 «La nº 1 cada día» | 1 posición; se compra el puesto 1 y se vende en cuanto deja de serlo; 90 % del patrimonio |
| V2 «Sin filtro de mercado» | La regla sin la condición de BTC sobre su media de 100 días |

K registrado = 3 (la regla y las 2 variantes). Cualquier prueba ad hoc sube K y se marca «NO CUENTA».

## 3. Costes

Comisión del 0,10 % por lado (*taker*, sin descuento con BNB) más el medio diferencial y el
deslizamiento según la mediana de volumen de 30 días:

| Mediana de 30 días | Coste por lado | Ida y vuelta (2·(0,10 % + c)) |
|---|---|---|
| ≥ 200 M$ | 0,03 % | 0,26 % |
| 50-200 M$ | 0,04 % | 0,28 % |
| 10-50 M$ | 0,08 % | 0,36 % |
| 5-10 M$ (y por debajo) | 0,15 % | 0,50 % |

Calibración con fotos del libro (`copybot crypto costs show`) tomadas el 22-oct-2026 antes de
este registro: 4 fotos (10:01, 10:11, 10:21 y 10:31 UTC, unas 2.000 observaciones). Medido (percentil 75 del medio diferencial más un colchón del 0,02 %): ≥ 200 M$ 0,025 %; 50-200 M$ 0,026 %; 10-50 M$ 0,045 %; 5-10 M$ 0,043 %. El tramo más líquido superaba la tabla del diseño (0,02 %) y se subió al 0,03 %; los demás tramos quedan por encima de lo medido. Libro fino (< 25 $ al mejor precio): 2 % de las fotos de 10-50 M$ y 9 % de las de 5-10 M$. Limitación: todas las fotos son de una misma mañana; cada mes se repetirá la calibración solo para vigilar la deriva.

Pruebas de estrés fijadas: costes ×2 (obligatoria) y ×3, ejecución con un día de retraso, sin
las 3 monedas que más aportaron, solo supervivientes, retiradas al 0 % y al 100 %.

## 4. Protocolo y semáforo

- Periodo: desde el primer día con al menos 20 monedas evaluables hasta el último día cerrado.
  Todo el histórico es **pseudo-fuera de muestra**; lo único fuera de muestra de verdad es la
  prueba en vivo desde este registro.
- Referencia del semáforo: **BTC comprar y mantener**. Diagnósticos: BTC con el mismo filtro,
  90 % BTC, cesta de las 10 más negociadas, USDT y 1.000 carteras al azar (semillas 1-1.000).
- Estadística: exceso diario logarítmico frente a BTC; bootstrap estacionario con bloques de
  7, 28 y 56 días (cuenta el intervalo más ancho); Sharpe deflactado con el K registrado.
- Semáforo (apartado 9 del diseño) con las decisiones del propietario: caída máxima < 25 %
  para el verde (25-50 %: ámbar; ≥ 50 %: rojo); verde en vivo estricto (revisión cada 28 días,
  como mucho 13, z = 2,89); parada diaria con reanudación automática; con la caída del 50 % se
  deja de comprar y las ventas siguen sus reglas.

## 5. Huellas (las comprueba `copybot crypto prereg check`)

- Huella regla: `44caa3a5252aef701d4d3d9106f084de3b64d0a25a51882b9da7bf310fc8cf39`
- Huella costes: `2c9ee6e81e6a843e090c018b4ae5790a38e525ba85cd1dd8a43fb55c2b5d557f`
- Huella variante V1: `5944c4b01b0dfc91026ee2749f378876be0a59e28c5812c0521a8709839a8f68`
- Huella variante V2: `fc3757f4353cfa241ab134334e975797bd8e67a6df4870a4aa9f849b18f01425`

## 6. Correcciones del código antes de la primera ejecución (25-oct-2026)

Una revisión adversaria del código (sin mirar ningún resultado con datos reales) encontró
fallos de implementación, no de la regla. Se corrigieron antes del primer backtest oficial;
las huellas de arriba no cambian porque la regla, las variantes y los costes son los mismos:

- La caída máxima toma el pico del patrimonio al cierre y el valle del peor momento del día.
- La prueba con un día de retraso ya no abre más de 5 posiciones ni gasta la reserva.
- El libro de señales y la calibración cierran con la regla de los 7 días sin velas, sin mirar
  datos posteriores; las velas sin operaciones cuentan como días sin negociación.
- La curva A y las 1.000 carteras al azar usan la misma reserva (10 % del patrimonio).
- El backtest termina en la última vela de BTC; las compras sin efectivo suficiente se omiten.
- Subperiodos y meses cuentan su primer día; la cesta de 10 se reequilibra por diferencia.
- Bootstrap con 5.000 remuestreos; se publican también el percentil del Sharpe en el nulo y el
  intervalo por semanas de las operaciones.
- Cartera en vivo: filtros solo en vivo, retiradas, posiciones por debajo del mínimo, plazo
  hasta las 02:00, reintentos con datos incompletos y una cartera continua que no se reinicia.

# Informe de la Fase 5: backtest del copy-trading (cierre de la línea de memecoins)

- **Fecha:** 21 de octubre de 2026
- **Estado:** cerrada. Por tu decisión del 21-oct, el proyecto pasa a ser un bot de cripto en
  Binance con un análisis diario del mercado. El código de memecoins queda en git.
- **Rama:** `claude/trusting-fermat-jangmb`.

## 1. Resumen

- **Copiar a los traders de FOMO con 4-8 s de retraso pierde dinero, incluso copiando a los
  que ganaron.**
  - Con los traders de la leaderboard (elegidos sabiendo que habían ganado, la cota más
    optimista posible), el libro que copia cada señal con 25 $ pierde entre **−735 $ y
    −1.229 $ en 162 operaciones** (media de **−16 % a −25 % por operación**; intervalo de
    confianza siempre por debajo de cero).
  - Las mismas operaciones a los precios del propio trader habrían ganado **+735 $ a +945 $**.
    La diferencia es el coste de llegar tarde: entramos de mediana un **+21 % a +24 %** por
    encima del trader.
- **Con la selección justa** (220 traders descubiertos por actividad, elegidos cada semana solo
  con datos anteriores) casi nunca hay a quién copiar. El 5-oct ninguno tenía ventaja
  demostrable; el 12-oct, uno. Ese trader perdió en la semana siguiente: −63 $ en 21
  operaciones, muestra insuficiente.
- En el mismo periodo (5-19 oct) todo bajó: BTC −5,9 %, ETH −12,6 %, SOL −14,9 % y el
  S&P 500 tokenizado −1,0 % comprando y manteniendo.
- **Verificación:** cuatro agentes independientes reprodujeron las cifras desde la caché,
  volvieron a descargar 50 ventanas de precio de Helius (coinciden en las 50) y auditaron 15
  posiciones a mano. El resultado se mantiene; los fallos que encontraron (apartado 4) cambian
  la magnitud, no el signo.

## 2. Qué se construyó

| Fichero | Función |
|---|---|
| `src/copybot/backtest/copy.py` | Motor del backtest: dos libros, costes, stop-loss, venta a las 48 h, paradas por riesgo |
| `src/copybot/backtest/delayed.py` | Precio real 4-8 s después de cada operación, a partir de los swaps de otros usuarios (Helius), con caché |
| `src/copybot/backtest/poolprice.py` | Precio horario de SOL, BTC, ETH y SPYx a partir de swaps reales en pools contra USDC |
| `src/copybot/backtest/references.py` | Comprar y mantener, compra semanal y seguimiento de tendencia |
| `src/copybot/backtest/stats.py`, `report.py` | *Bootstrap* con semilla fija, avisos de muestra e informe reproducible |
| `src/copybot/backtest_cli.py` | `copybot backtest copy` (selección semanal fuera de muestra) |

Criterios de la Fase 1 para esta fase: el mismo informe sale de la misma semilla y los mismos
datos (probado), avisa de muestra insuficiente en cada fila y documenta sus limitaciones.

## 3. Resultados (5-oct 00:00 → 19-oct 18:00 UTC)

| Universo | Operaciones (señales) | Realista | Teórica (precios del trader) | Entrada frente al trader |
|---|---|---|---|---|
| Leaderboard (cota optimista) | 162 | −1.229 $ (−735 $ con salidas más benévolas) | +735 $ (+945 $ corregida) | +23,7 % de mediana |
| Descubiertos por actividad | 21 | −63 $ | −72 $ (−54 $ corregida) | −4,8 % (el trader paga ~4 % de comisiones en FOMO) |

| Referencia (100 $) | BTC | ETH | SOL | SPYx |
|---|---|---|---|---|
| Comprar y mantener | −5,9 % | −12,6 % | −14,9 % | −1,0 % |
| Compra semanal | −3,2 % | −6,7 % | −7,6 % | −1,3 % |
| Tendencia (media de 24 h) | −16,8 % | −20,7 % | −22,0 % | −7,4 % |

La tendencia pierde sobre todo por costes (0,3 % por operación con 26-48 operaciones). Sin
costes habría superado a comprar y mantener en los cuatro activos. Es un periodo de dos
semanas y bajista: no dice nada sobre el largo plazo.

## 4. Fallos encontrados en la verificación (no corregidos al cerrar la línea)

1. **Posiciones dadas por perdidas demasiado pronto.** Si nadie operaba al menos 0,05 SOL en
   los 10 minutos siguientes a las 48 h, la posición valía 0 $, aunque el token siguiera
   cotizando. Fueron 21 posiciones (−629 $). Valoradas a su último precio, el total pasa de
   −1.229 $ a −735 $.
2. **La copia teórica a veces no usaba el precio del trader** (en las posiciones dadas por
   perdidas). Corregida, sale mejor: +945 $ en lugar de +735 $. La brecha real es mayor.
3. **Doble cómputo pequeño del impacto.** El precio retrasado ya incluye el impacto de otros
   compradores y además se le suma el nuestro. Supone un +1,3 % a +2,1 % en la entrada (como
   mucho +4,6 %).
4. **Ventanas con muchos bots** podían quedarse sin precio al agotar las páginas y contarse
   como «nadie operó».

## 5. Coste

Unos 26.000 créditos de Helius en total (precios retrasados, precios horarios de 4 activos y
570 de la verificación). Jupiter, gratis.

## 6. Conclusión

Copiar memecoins con retraso no es rentable con los datos disponibles: el retraso se come
toda la ventaja de los traders y algo más. El proyecto cambia de rumbo: un bot de cripto en
Binance (cualquier moneda) que cada día analiza el mercado y dice en qué invertir, con su
propio backtest frente a comprar BTC y mantener antes de arriesgar dinero.

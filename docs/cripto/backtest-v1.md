# Backtest de la regla cripto-v1

- Periodo: 20-02-2020 → 24-10-2026 (2439 días). Empieza el primer día con al menos 20 monedas evaluables.
- Monedas evaluables por día: mediana 87 (de 13 a 100).
- **Pseudo-fuera de muestra:** los parámetros no se ajustaron a estos datos, pero quien los eligió ya conocía esos años. Lo único fuera de muestra de verdad será la prueba en vivo desde el preregistro.
- Configuraciones probadas (K): 3. Semilla: 7. Commit: `29b2ecb1414e`.
- Huellas: datos `7907432f50fbdb5d`, regla `44caa3a5252aef70`, costes `2c9ee6e81e6a843e`.
- Costes: 0,10 % de comisión por lado más el diferencial y deslizamiento de su tramo de liquidez. Decisión con la vela cerrada; ejecución en la apertura siguiente.

## Resultado frente a BTC

| Estrategia | 100 $ se convierten en | Al año | Caída máxima | Sharpe | Peor mes |
|---|---|---|---|---|---|
| **Regla v1** (método, tamaño en %) | 1.068,65 $ | +42,6 % | 74,2 % | 0,86 | -28,1 % |
| Tu cartera de 100 $ (topes vigentes) | 800,80 $ | +36,5 % | 44,4 % | 1,05 | -25,0 % |
| BTC comprar y mantener | 841,74 $ | +37,6 % | 76,6 % | 0,84 | -37,3 % |
| BTC con el mismo filtro | 974,87 $ | +40,6 % | 36,0 % | 1,07 | -17,1 % |
| 90 % BTC + 10 % USDT | 767,56 $ | +35,7 % | 75,4 % | 0,82 | -36,1 % |
| Cesta de las 10 más negociadas | 178,21 $ | +9,0 % | 89,0 % | 0,52 | -39,8 % |
| Dejarlo en USDT | 100,00 $ | 0,0 % | 0,0 % | — | 0,0 % |

- **Diferencia con BTC:** +3,6 % (margen de error: -37,5 % a +75,9 %) al año. El margen es el más ancho de tres bootstrap estacionarios (bloques de 7, 28 y 56 días).
- Ratio de información: 0,05. Sharpe deflactado con K = 3: 0,235 (hace falta ≥ 0,95).
- Días necesarios para poder afirmarlo: unos 530.374.
- El método llegó a caer un 50 % o más desde un máximo.

## Por periodos (al año)

| Periodo | Regla v1 | BTC | BTC con filtro | 10 más negociadas | Diferencia con BTC |
|---|---|---|---|---|---|
| inicio-2020 (02/2020-12/2020) | +68,9 % | +258,5 % | +158,1 % | +63,5 % | -52,9 % |
| 2021-2022 (01/2021-12/2022) | +91,8 % | -24,4 % | +18,7 % | -24,9 % | +153,6 % |
| 2023-hoy (01/2023-10/2026) | +17,4 % | +51,6 % | +34,0 % | +20,9 % | -22,5 % |
| primera mitad (02/2020-06/2023) | +56,8 % | +41,6 % | +55,7 % | +2,8 % | +10,8 % |
| segunda mitad (06/2023-10/2026) | +29,6 % | +33,7 % | +27,0 % | +15,7 % | -3,0 % |
| 2020 (02/2020-12/2020) | +68,9 % | +258,5 % | +158,1 % | +63,5 % | -52,9 % |
| 2021 (01/2021-12/2021) | +463,5 % | +59,8 % | +73,9 % | +227,0 % | +252,7 % |
| 2022 (01/2022-12/2022) | -34,7 % | -64,2 % | -19,0 % | -82,7 % | +82,4 % |
| 2023 (01/2023-12/2023) | -29,5 % | +155,6 % | +73,0 % | +63,2 % | -72,4 % |
| 2024 (01/2024-12/2024) | +176,5 % | +120,8 % | +59,1 % | +70,2 % | +25,2 % |
| 2025 (01/2025-12/2025) | -9,2 % | -6,3 % | +4,0 % | -22,8 % | -3,1 % |
| 2026 (01/2026-10/2026) | +5,1 % | -9,4 % | +8,1 % | -4,6 % | +15,9 % |

## Variantes registradas (informativas; K = 3)

| Variante | 100 $ se convierten en | Diferencia con BTC al año |
|---|---|---|
| La nº 1 cada día (rota a la primera del ranking) | 6,87 $ | -51,3 % (margen de error: -76,8 % a +1,7 %) |
| Sin filtro de mercado (nunca pasa a USDT) | 145,90 $ | -23,1 % (margen de error: -55,6 % a +37,1 %) |

Ninguna variante sustituye a la regla: para cambiarla hace falta una versión nueva que empieza de cero en vivo.

## Pruebas de estrés (fijadas de antemano)

| Prueba | 100 $ se convierten en | Diferencia con BTC al año | ¿Supera? |
|---|---|---|---|
| Costes ×2 (obligatoria) | 668,05 $ | -3,4 % | no |
| Costes ×3 (informativa) | 420,65 $ | -9,9 % | no |
| Ejecución con un día de retraso | 908,65 $ | +1,2 % | sí |
| Retiradas sin pérdida | 1.068,65 $ | +3,6 % | sí |
| Retiradas con pérdida total | 1.068,65 $ | +3,6 % | sí |
| Sin las 3 monedas que más aportaron | 230,55 $ | -17,6 % | no |
| Solo monedas que hoy siguen cotizando | 1.282,97 $ | +6,5 % | sí |

Las 3 que más aportaron: ZEC, CHZ, ARKM.

## ¿Elegir moneda aporta algo?

Con las mismas reglas pero eligiendo las monedas **al azar** (1000 carteras), la regla queda mejor que el 95 % de ellas (hace falta ≥ 95). Mediana al azar: 315,48 $; percentil 95: 1.054,32 $; regla: 1.068,65 $.

Por Sharpe (rentabilidad por unidad de riesgo), la regla queda mejor que el 93 % de las carteras al azar. Sharpe de la regla: 0,86; mediana al azar: 0,59.

## Libro de señales (las 5 primeras cada día, 7 días, 10 $ cada una)

- 11528 operaciones en 2400 días. Ganaron a BTC el 40 % de las veces.
- Exceso medio por operación frente a BTC (logarítmico): -2,15 %; margen con bloques de 7 días -3,12 % a -1,15 %, de 28 días -3,24 % a -1,03 %.
- Las operaciones de días seguidos se solapan: no son independientes.

## Calibración por puesto (7 días, frente a BTC, con costes)

| Puesto | Días | Exceso medio | Margen (bloques de 28 días) | Bate a BTC |
|---|---|---|---|---|
| 1 | 2431 | -3,38 % | -5,38 % a -1,52 % | 39 % |
| 2-5 | 2431 | -1,99 % | -3,03 % a -0,92 % | 39 % |
| 6-12 | 2431 | -1,82 % | -2,74 % a -0,87 % | 38 % |
| 13-30 | 2431 | -1,58 % | -2,35 % a -0,78 % | 38 % |
| 31+ | 2167 | -1,53 % | -2,32 % a -0,72 % | 39 % |

IC de rangos (Spearman entre puntos y rentabilidad a 7 días): media -0,023 (margen -0,044 a 0,000); positivo el 45 % de los días.
Rentabilidad media a 7 días por quintil de puntos (de peor a mejor): -0,17 % · +0,18 % · +0,09 % · +0,21 % · +0,46 %. Solo describe: no se usa para elegir nada.

## Si hubieras lanzado una prueba cualquier lunes (cartera de 100 $)

| Duración | Pruebas | Pierden dinero | Mediana | Peor | P5-P95 | Baten a BTC | Parada diaria | Parada del 50 % |
|---|---|---|---|---|---|---|---|---|
| 28 días | 344 | 39 % | 0,0 % | -33,8 % | -20,1 % a +45,3 % | 43 % | 8 % | 0 % |
| 90 días | 335 | 46 % | 0,0 % | -41,6 % | -29,6 % a +139,1 % | 51 % | 19 % | 3 % |
| 365 días | 296 | 43 % | +17,8 % | -51,5 % | -40,1 % a +549,1 % | 57 % | 44 % | 27 % |

Las pruebas se solapan: describe, no demuestra.

## Operaciones (método)

- 722 cerradas (108 al año); duración mediana 6 días; aciertos 38 %.
- Rentabilidad media por operación: +3,40 % (margen de error +0,69 % a +6,71 %, bootstrap por grupos de las 201 semanas de entrada: las de una misma semana no son independientes).
- Motivos de salida: baja del puesto 12 437, mercado bajista 190, pierde la tendencia 49, stop del −25 % 33, fuera del universo 13.
- Días con alguna posición: 57 %.
- Costes pagados: 312,33 $ (sobre un patrimonio que va creciendo).

## Monedas retiradas

- Monedas que estuvieron alguna vez en el universo: 443; de ellas, hoy ya no cotizan 129.
- Días con alguna posición en una moneda hoy retirada: 29 %.

## Semáforo «¿Es rentable?»: ROJO

- Backtest: **ROJO** · En vivo: GRIS.

| Criterio | Qué mide | Valor | Hace falta | ¿Cumple? |
|---|---|---|---|---|
| BG1 | Años de historia que se pueden evaluar | 6,7 años | ≥ 3 años | sí |
| BG2 | Días con menos de 20 monedas que se puedan evaluar | 3,8 % de los días | ≤ 5 % de los días | sí |
| BG3 | Datos completos (velas que están de las que deberían estar) | 100 % | ≥ 98 % | sí |
| BG4 | Incluye monedas que Binance retiró después (no solo las que sobrevivieron) | 129 pares retirados | al menos 1 | sí |
| BG5 | La regla es la registrada antes de ver resultados | el hash coincide | el hash coincide | sí |
| BR1 | Gana a BTC con los costes normales | +3,6 %/año | > 0 (si no, ROJO) | sí |
| BR2 | Gana a BTC con el doble de costes | −3,4 %/año | > 0 (si no, ROJO) | no |
| BR3 | La cartera de 100 $ no se habría parado por una caída del 50 % | caída máxima del 74,2 % | < 50 % (si no, ROJO) | no |
| B1 | Gana a BTC por un margen que no parece suerte | +3,6 %/año (margen de error desde −37,5 %) | límite inferior > 0 con L = 7, 28 y 56 | no |
| B2 | La ventaja resiste haber probado varias versiones (Sharpe deflactado) | 0,24 | ≥ 0,95 | no |
| B3 | Sigue ganando a BTC con el doble de costes y con un día de retraso | costes ×2: −3,4 %/año; un día de retraso: +1,2 %/año | > 0 en los dos casos | no |
| B4 | No depende de 3 monedas ni de una sola época | sin las 3 monedas que más aportaron: −17,6 %/año; 1.ª mitad: +10,8 %/año; 2.ª mitad: −3 %/año; 2023-hoy: −22,5 %/año | > 0 en los cuatro casos | no |
| B5 | Elegir moneda aporta algo frente a elegir al azar | mejor que el 95 % de las carteras al azar | ≥ 95 % (supera el percentil 95 del azar) | sí |
| B6 | Libro de señales: las elegidas ganan a BTC en los 7 días siguientes | −2,2 % por operación (margen de error desde −3,2 %) | media > 0 y límite inferior (L = 28) > 0 | no |
| B7 | La caída máxima es moderada | 74,2 % | < 25 % (entre 25 y 50 %: ÁMBAR) | no |
| B8 | Incluye el archivo de pares que Binance retiró | no importado | importado (sin él, como mucho ÁMBAR) | no |
| VG0 | Hay datos de la cartera de prueba en vivo | no | sí | no |

- Un semáforo en verde no garantiza beneficios futuros.

## Limitaciones

- Velas diarias: no se ve lo que pasa dentro del día; una caída y su rebote en horas no disparan nada.
- Los costes son de la tabla calibrada con fotos recientes del libro; en los desplomes los diferenciales se abren más.
- Los pares retirados que Binance ya no muestra no están; eso favorece a la estrategia (sesgo de supervivencia no corregido del todo).
- Lo pasado no garantiza lo futuro.

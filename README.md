# trading-bot

Bot de trading de **cripto en Binance**, **simulado**: cada día analiza el mercado,
ordena las monedas con una regla fija y lleva una cartera de prueba con dinero ficticio.

> **Estado:** regla `cripto-v1` programada y probada; pendiente del primer backtest con
> datos reales (se publicará tal como salga) y del arranque de la cartera de prueba.
> El proyecto solo simula: usa datos públicos de mercado, no maneja claves de exchange,
> no firma ni envía órdenes. Un test impide añadir ese tipo de código. Usar dinero real
> solo se planteará si la regla supera los criterios fijados de antemano.
>
> **Nadie puede saber qué moneda subirá más mañana.** El bot dice cuál es la mejor
> puntuada **según su regla**, y enseña cómo le fue a esa regla antes y en vivo.

## Qué hace

- **Descarga las velas diarias** de todos los pares contra USDT de Binance desde 2017,
  también los de monedas ya retiradas (para no ver solo a las supervivientes).
- **Analiza el mercado cada día** con la vela cerrada: qué monedas se pueden evaluar (liquidez,
  antigüedad, calidad de datos; sin estables ni tokens apalancados), su puntuación de 0 a
  100 por la fuerza de su subida ajustada por lo que suelen moverse (7 a 90 días), su
  tendencia y el estado del mercado (BTC frente a su media de 100 días). Escribe el informe
  «Análisis del mercado» en castellano.
- **Cartera de prueba** de 100 $ ficticios: tiene las 5 mejores con tendencia (18 $ cada
  una), vende si bajan del puesto 12, pierden la tendencia, caen un 25 % o el mercado se
  vuelve bajista (todo a USDT). Ejecuta al precio real del libro de Binance (compra al ask,
  vende al bid) con comisiones. Puedes lanzar una prueba con la duración que quieras.
- **Backtest** de la misma regla desde 2017 frente a comprar BTC y mantener, BTC con el
  mismo filtro, las 10 más negociadas y 1.000 carteras al azar, con costes calibrados,
  pruebas de estrés y estadística honesta. Semáforo «¿Es rentable?» con criterios fijados
  antes de ver resultados.

## Documentación

- Bot de cripto: diseño aprobado [`docs/cripto/diseno.md`](docs/cripto/diseno.md) y
  preregistro de la regla [`docs/cripto/preregistro-v1.md`](docs/cripto/preregistro-v1.md).

Fases anteriores (laboratorio de copy-trading de memecoins en Solana, abandonado porque
copiar con retraso perdía dinero):

- Fase 1 (investigación, arquitectura, presupuesto y criterios para usar dinero real):
  [`docs/fase-1-investigacion.md`](docs/fase-1-investigacion.md)
- Informe de la Fase 2 (verificación con datos reales, pruebas y limitaciones):
  [`docs/informes/fase-2.md`](docs/informes/fase-2.md)
- Informe de la Fase 3 (descubrimiento y puntuación de traders, resultados con datos
  reales): [`docs/informes/fase-3.md`](docs/informes/fase-3.md)
- Informe de la Fase 4 (simulación de copy-trading con dos libros, riesgo y prueba en
  vivo): [`docs/informes/fase-4.md`](docs/informes/fase-4.md)
- Informe de la Fase 5 (backtest fuera de muestra del copy-trading frente a SOL, BTC,
  ETH y el S&P 500 tokenizado): [`docs/informes/fase-5.md`](docs/informes/fase-5.md)

## Uso

```bash
uv run copybot crypto sync                  # descargar velas nuevas de Binance
uv run copybot crypto analyze --day 2026-10-20   # análisis de un día pasado
uv run copybot crypto coin PEPE             # por qué una moneda está o no, y su puesto
uv run copybot crypto backtest --out bt.md  # backtest de la regla (se registra siempre)
uv run copybot crypto daily                 # paso diario completo, una vez
uv run copybot crypto run                   # servicio: el paso diario a las 00:10 UTC
uv run copybot crypto today                 # último informe diario
uv run copybot crypto paper start --days 28 # lanzar una prueba de 28 días
uv run copybot crypto paper status | pause | resume | stop
uv run copybot crypto costs calibrate       # foto del libro para calibrar costes
uv run copybot crypto prereg check          # el código coincide con lo registrado
```

Necesita PostgreSQL (`COPYBOT_DATABASE_URL`) y acceso a `data-api.binance.vision`.

## Panel

```bash
uv run copybot panel                              # http://127.0.0.1:8000, solo este ordenador
uv run copybot panel --host 0.0.0.0 --port 8000   # para verlo desde el móvil en tu red
```

Página local con el estado del bot de cripto: saldo de la cartera de prueba de 100 $
frente a BTC comprado el mismo día, ganancia o pérdida con comisiones, aciertos, caída
máxima, semáforo «¿Es rentable?», registro de compras y ventas con su motivo y su
resultado, la decisión de hoy con las 10 mejor puntuadas y su «por qué», los umbrales de
la regla (y si la nº 1 de hoy los cumple), el latido del paso diario y los seis pasos del
día (descarga, filtro, puntúa, riesgo, ejecuta y libro), cada uno con su mascota. Se
actualiza sola cada 15 s, se ve bien en el móvil y funciona sin internet (todo va dentro
de la página). La pestaña «Sniper Solana (simulación)» queda reservada para el futuro bot
de sniping y de momento dice «sin datos todavía».

Todos los números salen de la base de datos; si no hay, pone «sin datos todavía». El
panel solo lee (su conexión es de solo lectura) y nunca enseña la configuración ni claves.
No tiene contraseña: con `--host 0.0.0.0` lo puede ver cualquiera que llegue a tu red.
La página usa una API JSON: `/api/estado` (con `?cartera=N` enseña otra cartera) y
`/api/sniper`.

## Uso para desarrolladores (laboratorio de memecoins, fases 2-5)

Requisitos: Python 3.12+, [uv](https://docs.astral.sh/uv/) y PostgreSQL 16. El
descubrimiento y la importación de historiales necesitan una clave de Helius
(`COPYBOT_HELIUS_API_KEY`).

```bash
uv sync                       # instala dependencias
cp .env.example .env          # y edita los valores
uv run alembic upgrade head   # crea las tablas
uv run copybot wallets add <dirección>
uv run copybot monitor        # sigue las wallets en vivo
```

Descubrimiento y análisis de traders:

```bash
uv run copybot discover run --windows 12 --days 7 --top 70 --token-lookups 5  # todo seguido
uv run copybot discover sample --windows 12 --days 7   # muestrear el relayer de FOMO
uv run copybot discover candidates                     # las más recurrentes
uv run copybot discover import --top 20                # importar sus historiales
uv run copybot discover screen --limit 2000 --max-windows 1          # cuánto opera cada una
uv run copybot discover import --activity 0.7:15 --sample 150 --days 97  # las que operan poco
uv run copybot discover add --file leaderboard.txt --source leaderboard  # direcciones a mano
uv run copybot discover import --source leaderboard
uv run copybot discover import --refresh               # poner al día lo ya importado
uv run copybot analyze --prices [--token-lookups 5]    # analizar ahora, con precios
uv run copybot analyze --as-of 2026-10-18T18:00:00     # o a una fecha pasada
uv run copybot traders ranking                         # ranking con motivos
uv run copybot traders show <wallet>                   # explicación completa
```

Simulación (sigue las wallets añadidas con `copybot wallets add`):

```bash
uv run copybot sim run                 # motor + monitor en vivo (Ctrl+C para parar)
uv run copybot sim run --minutes 90    # o solo un rato
uv run copybot sim status              # estado y resumen
uv run copybot sim report              # informe completo y comparaciones
uv run copybot sim pause | resume | stop

# Backtest: selección semanal desde el 5-oct entre los traders descubiertos por actividad
uv run copybot backtest copy --select-as-of 2026-10-05 --until 2026-10-19T18:00 \
    --fetch --max-lookups 4000 --out informe.md
# Sin --fetch usa solo lo guardado en caché (no gasta créditos).
# --universe leaderboard: cota optimista (la leaderboard se capturó sabiendo quién ganó).
```

Los parámetros del motor se cambian con variables `COPYBOT_SIM_*` (latencia, slippage,
stop-loss…) y los de los filtros con `COPYBOT_SAFETY_*`, en el entorno o en `.env`. Los
límites de riesgo (`COPYBOT_RISK_*`) solo pueden endurecerse. Por defecto se usa el
perfil agresivo decidido en la Fase 4: 25 $ por posición, hasta 8 posiciones, 10 % de
reserva, 20 % de pérdida diaria y 50 % de caída máxima. Una variable `COPYBOT_*`
desconocida (por ejemplo, con una errata) impide arrancar.

El fichero de `discover add --file` tiene una wallet por línea: `dirección` o
`dirección, etiqueta`. Para medir a 90 días: `COPYBOT_SCORING_WINDOW_DAYS=90`.

Cada orden que usa Helius dice al terminar cuántos créditos ha gastado y cuántos quedan
del presupuesto del mes para el análisis; al llegar al límite se detiene antes de pedir
nada más.

Tests (los marcados `postgres` necesitan una base de datos de pruebas; si no la
encuentran, se saltan y lo indican):

```bash
uv run pytest
uv run ruff check src tests scripts migrations
```

Scripts de verificación con datos reales (ver los informes de las Fases 2 y 3):

```bash
uv run python scripts/spike_fomo_verification.py --cache-dir /ruta/cache --fetch 60
uv run python scripts/spike_fomo_verification.py --cache-dir /ruta/cache \
    --helius-windows 6 --window-days 7 --per-window 100 --seed 3 --explain-transfer-fees
uv run python scripts/spike_ws_latency.py --seconds 120
```

# trading-bot

Laboratorio de análisis y copy-trading **simulado** de memecoins en Solana.

> **Estado:** Fase 4 (motor de simulación, filtros de seguridad y gestión de riesgo),
> pendiente de aprobación; resultados en el informe de la Fase 4.
> El proyecto solo simula: no maneja claves privadas, no firma transacciones y no envía
> órdenes. Un test impide añadir ese tipo de código. El modo con dinero real solo se
> planteará si la simulación cumple los criterios fijados de antemano (apartado 12 del
> informe de la Fase 1).

## Documentación

- Fase 1 (investigación, arquitectura, presupuesto y criterios para usar dinero real):
  [`docs/fase-1-investigacion.md`](docs/fase-1-investigacion.md)
- Informe de la Fase 2 (verificación con datos reales, pruebas y limitaciones):
  [`docs/informes/fase-2.md`](docs/informes/fase-2.md)
- Informe de la Fase 3 (descubrimiento y puntuación de traders, resultados con datos
  reales): [`docs/informes/fase-3.md`](docs/informes/fase-3.md)
- Informe de la Fase 4 (simulación de copy-trading con dos libros, riesgo y prueba en
  vivo): [`docs/informes/fase-4.md`](docs/informes/fase-4.md)

## Qué hace ahora mismo

- **Sigue wallets de Solana en vivo** y guarda cada transacción en PostgreSQL. Si se corta
  la conexión, recupera después lo que se haya perdido.
- **Clasifica cada transacción** como compra, venta, transferencia o ambigua, con un nivel
  de confianza. La autoría se deduce de quién cambia de saldo, no de quién paga la
  comisión. En FOMO, la comisión la paga siempre una wallet de la propia FOMO.
- **Descubre traders de FOMO** muestreando lo que firma su relayer (también los que
  operan poco, con un filtro de actividad barato) o a partir de direcciones que le
  pases (por ejemplo, de la leaderboard de FOMO), **importa su historial** y **calcula
  sus métricas**: rentabilidad por operación, acierto, tiempo hasta vender, caídas,
  posiciones abandonadas (valoradas a su precio de venta), concentración del
  beneficio, compras en el lanzamiento del token…
- **Puntúa a cada trader** de 0 a 100 con una explicación en texto, o lo excluye diciendo
  por qué. Cada cálculo se guarda con su fecha (`as_of`) y la versión de sus parámetros,
  y nunca usa información posterior a esa fecha.

- **Simula el copy-trading en vivo, sin dinero real.** Cada compra de un trader seguido
  pasa por filtros del token y del mercado y por los límites de riesgo. Después se
  «ejecuta» con cotizaciones reales de Jupiter: latencia, slippage, MEV, comisiones y
  renta de la cuenta de token. Lleva dos libros: la cartera de 100 $ con todos los
  límites y un libro que copia cada señal con 10 $ para medir su calidad. Copia las
  ventas del trader en la misma proporción y tiene stop-loss, venta a las 48 h, parada
  automática por pérdidas y modo de solo lectura si el monitor deja de dar datos.

Todavía no tiene interfaz gráfica ni bot de Telegram. Eso llega en las fases siguientes.

## Uso para desarrolladores

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

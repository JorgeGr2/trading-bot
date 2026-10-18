# trading-bot

Laboratorio de análisis y copy-trading **simulado** de memecoins en Solana.

> **Estado:** Fase 2 (conectores de datos y monitor de wallets), pendiente de aprobación.
> El proyecto solo simula: no maneja claves privadas, no firma transacciones y no envía
> órdenes. Un test impide añadir ese tipo de código. El modo con dinero real solo se
> planteará si la simulación cumple los criterios fijados de antemano (apartado 12 del
> informe de la Fase 1).

## Documentación

- Fase 1 (investigación, arquitectura, presupuesto y criterios para usar dinero real):
  [`docs/fase-1-investigacion.md`](docs/fase-1-investigacion.md)
- Informe de la Fase 2 (verificación con datos reales, pruebas y limitaciones):
  [`docs/informes/fase-2.md`](docs/informes/fase-2.md)

## Qué hace ahora mismo

- **Sigue wallets de Solana en vivo** y guarda cada transacción en PostgreSQL. Si se corta
  la conexión, recupera después lo que se haya perdido.
- **Clasifica cada transacción** como compra, venta, transferencia o ambigua, con un nivel
  de confianza. La autoría se deduce de quién cambia de saldo, no de quién paga la
  comisión. En FOMO, la comisión la paga siempre una wallet de la propia FOMO.

Todavía no simula operaciones, no tiene interfaz gráfica ni bot de Telegram. Eso llega
en las fases siguientes.

## Uso para desarrolladores

Requisitos: Python 3.12+, [uv](https://docs.astral.sh/uv/) y PostgreSQL 16.

```bash
uv sync                       # instala dependencias
cp .env.example .env          # y edita los valores
uv run alembic upgrade head   # crea las tablas
uv run copybot wallets add <dirección>
uv run copybot monitor        # sigue las wallets en vivo
```

Tests (los marcados `postgres` necesitan una base de datos de pruebas; si no la
encuentran, se saltan y lo indican):

```bash
uv run pytest
uv run ruff check src tests scripts migrations
```

Scripts de verificación con datos reales (ver el informe de la Fase 2):

```bash
uv run python scripts/spike_fomo_verification.py --cache-dir /ruta/cache --fetch 60
uv run python scripts/spike_ws_latency.py --seconds 120
```

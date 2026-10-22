"""cripto: índice de posiciones abiertas solo para la cartera

El libro de señales abre la misma moneda varios días seguidos (una posición por día de
entrada); la unicidad de posición abierta por moneda solo se aplica a la cartera.

Revision ID: a1c3e5f7b9d2
Revises: f64c64747be7
Create Date: 2026-10-22 10:40:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a1c3e5f7b9d2"
down_revision: str | Sequence[str] | None = "f64c64747be7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_index(
        "uq_crypto_paper_positions_open",
        table_name="crypto_paper_positions",
        postgresql_where=sa.text("status = 'open'"),
    )
    op.create_index(
        "uq_crypto_paper_positions_open",
        "crypto_paper_positions",
        ["run_id", "book", "symbol"],
        unique=True,
        postgresql_where=sa.text("status = 'open' AND book = 'portfolio'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_crypto_paper_positions_open",
        table_name="crypto_paper_positions",
        postgresql_where=sa.text("status = 'open' AND book = 'portfolio'"),
    )
    op.create_index(
        "uq_crypto_paper_positions_open",
        "crypto_paper_positions",
        ["run_id", "book", "symbol"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
    )

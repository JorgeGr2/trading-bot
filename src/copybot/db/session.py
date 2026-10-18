from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker, create_async_engine


def _json_default(value: Any) -> Any:
    # Los importes se guardan como texto para no perder precisión.
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, set | frozenset | tuple):
        return list(value)
    raise TypeError(f"{type(value).__name__} no se puede guardar en JSON")


def json_dumps(value: Any) -> str:
    return json.dumps(value, default=_json_default, ensure_ascii=False)


def make_engine(url: str) -> AsyncEngine:
    return create_async_engine(
        url, pool_pre_ping=True, pool_size=5, max_overflow=5, json_serializer=json_dumps
    )


def make_sessionmaker(engine: AsyncEngine) -> async_sessionmaker:
    return async_sessionmaker(engine, expire_on_commit=False)

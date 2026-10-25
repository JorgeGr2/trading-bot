"""Panel web: estado de los agentes, constructores de la API (base vacía y con datos), la
página, que no se filtren secretos y que la conexión sea de solo lectura."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from copybot import cli, panel_cli
from copybot.db.models import (
    CryptoAnalysis,
    CryptoAnalysisRow,
    CryptoBacktestRun,
    CryptoCostSnapshot,
    CryptoPaperBook,
    CryptoPaperDay,
    CryptoPaperEvent,
    CryptoPaperOrder,
    CryptoPaperPosition,
    CryptoPaperRun,
    CryptoPaperSnapshot,
    MarketBar,
    MarketSymbol,
    ServiceHeartbeat,
)
from copybot.db.session import make_sessionmaker
from copybot.panel.app import DB_ERROR, create_app, make_readonly_engine
from copybot.panel.common import (
    EN_ESPERA,
    SIN_DATOS,
    TRABAJANDO,
    agent_state,
    max_drawdown,
)
from copybot.panel.crypto import crypto_state, exclusion_label, pick_run
from copybot.panel.sniper import sniper_state
from tests.conftest import TEST_DB_URL

D = Decimal
DAY = date(2026, 10, 24)  # vela de hoy (cerrada)
NOW = datetime(2026, 10, 25, 0, 15, tzinfo=UTC)  # tres minutos después del paso
STEP_AT = datetime(2026, 10, 25, 0, 12, tzinfo=UTC)


# --- Lógica pura --------------------------------------------------------------------------


def test_agent_state_works_while_running_or_within_the_window():
    now = datetime(2026, 10, 25, 0, 20, tzinfo=UTC)
    assert agent_state(None, now) == EN_ESPERA  # nunca ha corrido
    assert agent_state(None, now, running=True) == TRABAJANDO
    assert agent_state(now - timedelta(minutes=3), now) == TRABAJANDO
    assert agent_state(now - timedelta(minutes=10), now) == TRABAJANDO  # el límite cuenta
    assert agent_state(now - timedelta(minutes=10, seconds=1), now) == EN_ESPERA
    assert agent_state(now - timedelta(hours=20), now) == EN_ESPERA
    assert agent_state(now + timedelta(seconds=30), now) == TRABAJANDO  # reloj adelantado
    assert agent_state(now - timedelta(hours=20), now, running=True) == TRABAJANDO
    assert agent_state(now - timedelta(minutes=30), now, window=timedelta(hours=1)) == TRABAJANDO


def test_max_drawdown_takes_the_closing_peak_and_the_worst_moment():
    assert max_drawdown(100.0, []) is None
    # Pico al cierre de 120; el peor momento del día siguiente, 90: caída del 25 %.
    assert max_drawdown(100.0, [(110, 105), (120, 112), (100, 90)]) == pytest.approx(0.25)
    # El peor momento del primer día se compara con el capital inicial.
    assert max_drawdown(100.0, [(101, 95)]) == pytest.approx(0.05)


def test_pick_run_prefers_the_active_continua():
    def run(i, kind, status, version="cripto-v1"):
        return CryptoPaperRun(id=i, kind=kind, status=status, strategy_version=version)

    old = run(1, "prueba", "FINISHED")
    test = run(2, "prueba", "RUNNING")
    continua = run(3, "continua", "PAUSED")
    assert pick_run([]) is None
    assert pick_run([old]) is old
    assert pick_run([old, test]) is test
    assert pick_run([old, test, continua]) is continua
    assert pick_run([old, test, continua], 1) is old
    assert pick_run([old], 9) is None


def test_exclusion_reasons_are_grouped_without_their_numbers():
    assert exclusion_label("solo lleva 45 días en Binance (mínimo 120)") == (
        "lleva menos de 120 días en Binance"
    )
    assert exclusion_label("poco volumen: mediana de 1,2 M $ al día (mínimo 5 M $)").startswith(
        "poco volumen (menos de"
    )
    assert exclusion_label("fuera de las 100 más líquidas (puesto 130)") == (
        "fuera de las 100 más líquidas"
    )
    assert exclusion_label("moneda estable o divisa") == "moneda estable o divisa"
    assert exclusion_label(None) == "sin negociación ese día"


def test_panel_command_is_registered_and_local_by_default(monkeypatch):
    parser = argparse.ArgumentParser()
    panel_cli.add_parsers(parser.add_subparsers(dest="command"))
    args = parser.parse_args(["panel"])
    assert (args.host, args.port) == ("127.0.0.1", 8000)
    assert parser.parse_args(["panel", "--port", "9000"]).port == 9000
    assert panel_cli.is_loopback("127.0.0.1") and panel_cli.is_loopback("localhost")
    assert not panel_cli.is_loopback("0.0.0.0")  # noqa: S104
    seen = {}
    monkeypatch.setattr(panel_cli, "run", lambda a, _s: seen.setdefault("args", a) and 0)
    monkeypatch.setattr(cli, "unknown_settings", list)  # solo se prueba el reparto
    assert cli.main(["panel", "--host", "127.0.0.1", "--port", "8123"]) == 0
    assert seen["args"].port == 8123


async def test_sniper_has_no_data_yet():
    assert await sniper_state(None) == {"estado": SIN_DATOS}


# --- Base de datos ------------------------------------------------------------------------


async def seed(sessions) -> int:
    """Cartera continua con tres días, órdenes con resultado, un análisis, un backtest, el
    latido y las velas: todo lo que lee el panel."""
    async with sessions.begin() as s:
        run = CryptoPaperRun(
            kind="continua",
            start_day=DAY - timedelta(days=2),
            status="RUNNING",
            strategy_version="cripto-v1",
            params_hash="h",
            limits={},
            initial_usd=D(100),
            created_by="cli",
        )
        s.add(run)
        await s.flush()
        s.add_all(
            [
                CryptoPaperBook(
                    run_id=run.id,
                    book="portfolio",
                    initial_usd=D(100),
                    cash_usd=D("64.10"),
                    peak_equity_usd=D(104),
                    prev_equity_usd=D(103),
                    last_day=DAY,
                ),
                CryptoPaperEvent(
                    run_id=run.id,
                    at=datetime(2026, 10, 22, 0, 12, tzinfo=UTC),
                    kind="inicio",
                    details={"btc_precio": "90000"},
                ),
                CryptoPaperEvent(
                    run_id=run.id,
                    at=STEP_AT,
                    kind="bloqueo",
                    details={"bloqueadas": {"WIFUSDT": "diferencial del 0,7 %"}},
                ),
            ]
        )
        for k, (equity, worst, btc) in enumerate(((101, 98, 100), (104, 102, 103), (103, 95, 99))):
            day = DAY - timedelta(days=2 - k)
            s.add(
                CryptoPaperSnapshot(
                    run_id=run.id,
                    book="portfolio",
                    day=day,
                    equity_usd=D(equity),
                    worst_equity_usd=D(worst),
                    cash_usd=D("64.10"),
                    positions_value_usd=D(equity) - D("64.10"),
                    open_positions=2,
                    btc_benchmark_usd=D(btc),
                )
            )
            s.add(
                CryptoPaperDay(
                    run_id=run.id,
                    day=day,
                    status="ok",
                    started_at=STEP_AT - timedelta(days=2 - k),
                    finished_at=STEP_AT - timedelta(days=2 - k),
                )
            )

        def order(symbol, side, purpose, day, status, *, reason, qty=None, price=None, fee=None):
            return CryptoPaperOrder(
                run_id=run.id,
                book="portfolio",
                day=day,
                idempotency_key=f"{symbol}{side}{day}",
                symbol=symbol,
                side=side,
                purpose=purpose,
                requested_usd=D(18) if side == "buy" else None,
                status=status,
                reason=reason,
                qty=qty,
                price=price,
                fee_usd=fee,
                filled_at=None
                if status != "filled"
                else STEP_AT - timedelta(days=(DAY - day).days),
            )

        d0, d1 = DAY - timedelta(days=2), DAY - timedelta(days=1)
        s.add_all(
            [
                order(
                    "SOLUSDT",
                    "buy",
                    "entrada",
                    d0,
                    "filled",
                    reason="puesto 1",
                    qty=D("0.1"),
                    price=D(180),
                    fee=D("0.018"),
                ),
                order(
                    "ADAUSDT",
                    "buy",
                    "entrada",
                    d0,
                    "filled",
                    reason="puesto 2",
                    qty=D(30),
                    price=D("0.6"),
                    fee=D("0.018"),
                ),
                order(
                    "SOLUSDT",
                    "sell",
                    "puesto",
                    DAY,
                    "filled",
                    reason="bajó al puesto 14",
                    qty=D("0.1"),
                    price=D(200),
                    fee=D("0.02"),
                ),
                order(
                    "ADAUSDT",
                    "sell",
                    "stop",
                    d1,
                    "filled",
                    reason="stop de emergencia",
                    qty=D(30),
                    price=D("0.45"),
                    fee=D("0.0135"),
                ),
                order(
                    "LINKUSDT",
                    "buy",
                    "entrada",
                    DAY,
                    "filled",
                    reason="puesto 1",
                    qty=D(1),
                    price=D(18),
                    fee=D("0.018"),
                ),
                order(
                    "WIFUSDT",
                    "buy",
                    "entrada",
                    DAY,
                    "rejected",
                    reason="puesto 3 · sin precio de compra ahora",
                ),
            ]
        )
        s.add_all(
            [
                CryptoPaperPosition(
                    run_id=run.id,
                    book="portfolio",
                    symbol="SOLUSDT",
                    status="closed",
                    qty=D(0),
                    entry_price=D(180),
                    cost_usd=D("18.018"),
                    opened_day=d0,
                    closed_day=DAY,
                    proceeds_usd=D("19.98"),
                    realized_pnl_usd=D("1.962"),
                    exit_purpose="puesto",
                ),
                CryptoPaperPosition(
                    run_id=run.id,
                    book="portfolio",
                    symbol="ADAUSDT",
                    status="closed",
                    qty=D(0),
                    entry_price=D("0.6"),
                    cost_usd=D("18.018"),
                    opened_day=d0,
                    closed_day=d1,
                    proceeds_usd=D("13.4865"),
                    realized_pnl_usd=D("-4.5315"),
                    exit_purpose="stop",
                ),
                CryptoPaperPosition(
                    run_id=run.id,
                    book="portfolio",
                    symbol="LINKUSDT",
                    status="open",
                    qty=D(1),
                    entry_price=D(18),
                    cost_usd=D("18.018"),
                    opened_day=DAY,
                    last_value_usd=D("18.9"),
                ),
                CryptoPaperPosition(
                    run_id=run.id,
                    book="signals",
                    symbol="LINKUSDT",
                    status="closed",
                    qty=D(0),
                    entry_price=D(17),
                    cost_usd=D(10),
                    opened_day=d0,
                    closed_day=DAY,
                    proceeds_usd=D(11),
                    realized_pnl_usd=D(1),
                ),
            ]
        )
        analysis = CryptoAnalysis(
            day=DAY,
            strategy_version="cripto-v1",
            params_hash="h",
            data_until=datetime(2026, 10, 25, tzinfo=UTC),
            computed_live=True,
            regime={"btc": 98000.0, "sma100": 82000.0, "favorable": True},
            universe_size=2,
            status="ok",
            confidence="BAJA",
            report_md="ANÁLISIS DEL MERCADO — informe",
            created_at=STEP_AT - timedelta(minutes=1),
        )
        s.add(analysis)
        await s.flush()
        medias = {"20": True, "50": True, "100": True}
        s.add_all(
            [
                CryptoAnalysisRow(
                    analysis_id=analysis.id,
                    symbol="LINKUSDT",
                    included=True,
                    rank=1,
                    liquidity_rank=12,
                    median_qv_30d=D(76_000_000),
                    score=D("97.2"),
                    components={"tendencia": 1.0, "medias": medias},
                    ret_7d=D("0.10"),
                    ret_30d=D("0.42"),
                    ret_90d=D("0.8"),
                    vol_daily=D("0.042"),
                    best_day_share=D("0.14"),
                    p5_week=D("-0.15"),
                    risk_level="Medio",
                    entry_eligible=True,
                ),
                CryptoAnalysisRow(
                    analysis_id=analysis.id,
                    symbol="ADAUSDT",
                    included=True,
                    rank=2,
                    liquidity_rank=30,
                    median_qv_30d=D(40_000_000),
                    score=D("40"),
                    components={"tendencia": 1 / 3, "medias": {"20": True}},
                    ret_30d=D("-0.05"),
                    vol_daily=D("0.04"),
                    entry_eligible=False,
                ),
                CryptoAnalysisRow(
                    analysis_id=analysis.id,
                    symbol="USDCUSDT",
                    included=False,
                    exclusion_reason="moneda estable o divisa",
                ),
                CryptoAnalysisRow(
                    analysis_id=analysis.id,
                    symbol="NEWUSDT",
                    included=False,
                    exclusion_reason="solo lleva 30 días en Binance (mínimo 120)",
                ),
            ]
        )
        s.add(
            CryptoBacktestRun(
                strategy_version="cripto-v1",
                config_hash="c",
                config={},
                role="principal",
                counts_for_k=True,
                period_from=date(2020, 2, 20),
                period_to=DAY,
                data_hash="d",
                cost_table_hash="t",
                seed=7,
                n_days=2439,
                metrics={
                    "exceso_anual": 0.036,
                    "margen": [-0.375, 0.759],
                    "luz": "rojo",
                    "periodos": [["2023-hoy", 0.174, 0.516, 0.34, 0.209]],
                },
            )
        )
        s.add(
            ServiceHeartbeat(
                service="crypto_daily",
                beat_at=STEP_AT + timedelta(seconds=40),
                details={"dia": DAY.isoformat(), "estado": "ok"},
            )
        )
        s.add(
            MarketSymbol(
                exchange="binance",
                symbol="LINKUSDT",
                base="LINK",
                quote="USDT",
                status="TRADING",
                spot=True,
                first_seen_at=STEP_AT - timedelta(days=400),
                last_seen_at=STEP_AT - timedelta(minutes=30),
            )
        )
        await s.flush()
        for k in range(3):
            day = DAY - timedelta(days=k)
            s.add(
                MarketBar(
                    exchange="binance",
                    symbol="LINKUSDT",
                    interval="1d",
                    open_time=datetime(day.year, day.month, day.day, tzinfo=UTC),
                    open=D(17),
                    high=D(19),
                    low=D(16),
                    close=D(18),
                    volume=D(1000),
                    quote_volume=D(18000),
                    trades=100,
                )
            )
        s.add(
            CryptoCostSnapshot(
                taken_at=STEP_AT,
                symbol="LINKUSDT",
                bid=D("17.99"),
                ask=D("18.01"),
                bid_qty=D(100),
                ask_qty=D(100),
            )
        )
        return run.id


@pytest.mark.postgres
async def test_empty_database_says_there_is_no_data_yet(engine):
    d = await crypto_state(make_sessionmaker(engine), NOW)
    json.dumps(d)  # se puede servir tal cual
    cards = d["tarjetas"]
    assert cards["saldo"] is None and cards["resultado"] is None and cards["aciertos"] is None
    assert d["cartera"] is None and d["curva"] == [] and d["actividad"] == []
    assert d["hoy"] is None and d["backtest"] is None
    assert d["latido"]["en_vivo"] is False and d["latido"]["ultimo"] is None
    assert [a["id"] for a in d["agentes"]] == ["01", "02", "03", "04", "05", "06"]
    assert all(a["estado"] == EN_ESPERA and a["linea"] == SIN_DATOS for a in d["agentes"])
    assert len(d["pulso"]) == 30 and all(p["paso"] is None for p in d["pulso"])
    lines = [line for g in d["umbrales"]["grupos"] for line in g["lineas"]]
    assert lines and all(line["medido"] is None and line["cumple"] is None for line in lines)
    assert d["mercado"]["velas"] == 0
    # Sin backtest ni cartera, el semáforo no puede pasar de gris.
    assert cards["semaforo"]["conclusion"] == "gris"


@pytest.mark.postgres
async def test_seeded_state_comes_from_the_database(engine):
    sessions = make_sessionmaker(engine)
    run_id = await seed(sessions)
    d = await crypto_state(sessions, NOW)
    json.dumps(d)
    c = d["tarjetas"]
    assert c["saldo"]["valor"] == 103 and c["saldo"]["dia"] == DAY.isoformat()
    assert c["resultado"]["usd"] == 3 and c["resultado"]["operaciones"] == 5
    assert c["resultado"]["comisiones"] == pytest.approx(0.018 * 3 + 0.02 + 0.0135)
    assert (c["aciertos"]["ganadas"], c["aciertos"]["perdidas"], c["aciertos"]["abiertas"]) == (
        1,
        1,
        1,
    )
    assert c["aciertos"]["senales_cerradas"] == 1 and c["aciertos"]["senales_ganadas"] == 1
    # Pico al cierre de 104 y peor momento de hoy, 95.
    assert c["caida"]["maxima"] == pytest.approx(1 - 95 / 104, abs=1e-6)
    assert c["semaforo"]["backtest"] == "rojo" and c["semaforo"]["conclusion"] == "rojo"
    assert c["semaforo"]["en_vivo"] == "gris" and c["semaforo"]["dias_en_vivo"] == 3
    assert d["cartera"]["id"] == run_id and d["cartera"]["efectivo"] == 64.1
    assert [p["moneda"] for p in d["cartera"]["posiciones"]] == ["LINK"]
    assert [p["valor"] for p in d["curva"]] == [101, 104, 103]
    assert [p["btc"] for p in d["curva"]] == [100, 103, 99]

    log = d["actividad"]
    sell = next(r for r in log if r.get("lado") == "venta" and r["moneda"] == "SOL")
    assert sell["resultado"]["gana"] is True and sell["resultado"]["usd"] == pytest.approx(1.96)
    assert sell["motivo"] == "bajó al puesto 14" and sell["usd"] == 20
    stop = next(r for r in log if r.get("lado") == "venta" and r["moneda"] == "ADA")
    assert stop["resultado"]["gana"] is False
    link = next(r for r in log if r.get("lado") == "compra" and r["moneda"] == "LINK")
    assert link["resultado"]["abierta"] is True and link["resultado"]["usd"] == pytest.approx(0.88)
    rejected = next(r for r in log if r.get("moneda") == "WIF")
    assert rejected["estado"] == "rejected" and rejected["resultado"] is None
    events = [r["texto"] for r in log if r["tipo"] == "evento"]
    assert any("WIF" in t and "diferencial" in t for t in events)
    assert any(t.startswith("Arranca la cartera") for t in events)
    whens = [r["cuando"] for r in log]
    assert whens == sorted(whens, reverse=True)

    hoy = d["hoy"]
    assert hoy["decision"] == "La mejor puntuada hoy según la regla: LINK (97 de 100)."
    assert hoy["tiene"] == ["LINK"] and hoy["efectivo"] == 64.1
    assert [m["moneda"] for m in hoy["mejores"]] == ["LINK", "ADA"]
    assert hoy["mejores"][0]["en_cartera"] and hoy["mejores"][0]["por_que"].startswith("LINK: sube")
    assert {e["motivo"] for e in hoy["excluidas"]} == {
        "moneda estable o divisa",
        "lleva menos de 120 días en Binance",
    }
    assert [b["moneda"] for b in hoy["compras"]] == ["LINK"]
    assert [v["moneda"] for v in hoy["ventas"]] == ["SOL"]
    assert hoy["informe"].startswith("ANÁLISIS")

    u = d["umbrales"]
    assert u["moneda"] == "LINK" and u["puesto"] == 1
    lines = {line["nombre"]: line for g in u["grupos"] for line in g["lineas"]}
    assert lines["entry_rank"]["cumple"] is True and lines["market_sma"]["cumple"] is True
    assert lines["min_median_quote_volume"]["cumple"] is True
    assert lines["entry_trend"]["cumple"] is True and lines["exit_trend"]["cumple"] is True
    assert lines["stop_loss"]["cumple"] is True  # en cartera, +5 % desde la compra
    assert lines["max_drawdown_pct"]["cumple"] is True
    assert lines["MAX_SPREAD_PCT"]["cumple"] is None  # sin dato del diferencial
    assert lines["horizons"]["cumple"] is None

    agents = {a["paso"]: a for a in d["agentes"]}
    assert agents["DESCARGA"]["estado"] == EN_ESPERA  # descargó hace 33 minutos
    for step in ("FILTRO", "PUNTÚA", "RIESGO", "EJECUTA", "LIBRO"):
        assert agents[step]["estado"] == TRABAJANDO, step
    assert agents["PUNTÚA"]["linea"] == "nº 1: LINK (97 puntos)"
    assert agents["EJECUTA"]["linea"] == "hoy: 1 compra, 1 venta"
    assert agents["DESCARGA"]["linea"].startswith("3 velas")
    later = await crypto_state(sessions, NOW + timedelta(hours=2))
    assert all(a["estado"] == EN_ESPERA for a in later["agentes"])

    assert d["latido"]["en_vivo"] is True and d["latido"]["estado"] == "ok"
    assert (await crypto_state(sessions, NOW + timedelta(hours=27)))["latido"]["en_vivo"] is False
    assert [p["paso"] for p in d["pulso"][-3:]] == ["ok", "ok", "ok"]
    assert d["backtest"]["luz"] == "rojo" and d["backtest"]["margen"] == [-0.375, 0.759]
    assert d["backtest"]["periodos"][0]["nombre"] == "2023-hoy"


def _client(sessions, clock=lambda: NOW) -> httpx.AsyncClient:
    app = create_app(sessions, clock=clock)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://panel")


@pytest.mark.postgres
async def test_page_and_api_answer(engine):
    sessions = make_sessionmaker(engine)
    await seed(sessions)
    async with _client(sessions) as client:
        page = await client.get("/")
        estado = await client.get("/api/estado")
        other = await client.get("/api/estado", params={"cartera": "999"})
        sniper = await client.get("/api/sniper")
    assert page.status_code == 200 and "Bot cripto" in page.text
    assert "Sniper Solana (simulación)" in page.text
    assert "default-src 'none'" in page.headers["content-security-policy"]
    # Todo va dentro de la página: sin CDN ni recursos externos.
    assert "<script src" not in page.text and '<link rel="stylesheet"' not in page.text
    assert not re.search(r"""(src|href)=["']?https?:|url\(\s*["']?https?:|@import""", page.text)
    assert estado.status_code == 200 and estado.headers["cache-control"] == "no-store"
    assert estado.json()["tarjetas"]["saldo"]["valor"] == 103
    assert other.status_code == 200 and other.json()["cartera"] is None
    assert sniper.json() == {"estado": SIN_DATOS}


@pytest.mark.postgres
async def test_no_secrets_reach_the_panel(engine, monkeypatch):
    monkeypatch.setenv("COPYBOT_HELIUS_API_KEY", "clave-helius-secreta")
    monkeypatch.setenv("COPYBOT_DATABASE_URL", "postgresql+asyncpg://u:pw-secreta@db/x")
    sessions = make_sessionmaker(engine)
    await seed(sessions)
    async with _client(sessions) as client:
        bodies = [(await client.get(path)).text for path in ("/", "/api/estado", "/api/sniper")]
    password = TEST_DB_URL.split("://", 1)[1].split("@", 1)[0].split(":", 1)[1]
    for body in bodies:
        for secret in ("clave-helius-secreta", "pw-secreta", "api-key", "postgresql", password):
            assert secret not in body


async def test_a_database_error_is_reported_without_details():
    bad = make_readonly_engine("postgresql+asyncpg://copybot:pw-secreta@127.0.0.1:1/nada")
    try:
        async with _client(make_sessionmaker(bad)) as client:
            r = await client.get("/api/estado")
    finally:
        await bad.dispose()
    assert r.status_code == 503 and r.json() == {"error": DB_ERROR}
    assert "pw-secreta" not in r.text and "127.0.0.1" not in r.text


@pytest.mark.postgres
async def test_the_panel_connection_is_read_only(engine):
    readonly = make_readonly_engine(TEST_DB_URL)
    try:
        async with readonly.connect() as conn:
            assert (await conn.execute(text("SHOW default_transaction_read_only"))).scalar() == "on"
            with pytest.raises(DBAPIError, match="read-only"):
                await conn.execute(
                    text(
                        "INSERT INTO service_heartbeats (service, beat_at, details) "
                        "VALUES ('x', now(), '{}')"
                    )
                )
    finally:
        await readonly.dispose()

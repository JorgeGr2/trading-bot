"""Panel web local: ``copybot panel [--host 127.0.0.1] [--port 8000]``.

Solo lee la base de datos. Por defecto solo se ve desde este ordenador; con
``--host 0.0.0.0`` se ve desde otros dispositivos de la red (el panel no tiene
contraseña).
"""

from __future__ import annotations

import argparse
import ipaddress

from copybot.config import Settings
from copybot.db.session import make_sessionmaker
from copybot.panel.app import create_app, make_readonly_engine


def add_parsers(sub: argparse._SubParsersAction) -> None:
    panel = sub.add_parser("panel", help="panel web local (solo lectura)")
    panel.add_argument("--host", default="127.0.0.1", help="dirección (por defecto, solo local)")
    panel.add_argument("--port", type=int, default=8000)


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def run(args: argparse.Namespace, settings: Settings) -> int:
    import uvicorn

    engine = make_readonly_engine(settings.database_url.get_secret_value())
    app = create_app(make_sessionmaker(engine), engine=engine)
    shown = "IP-de-este-ordenador" if args.host in ("0.0.0.0", "::") else args.host  # noqa: S104
    print(f"Panel en http://{shown}:{args.port} (Ctrl+C para parar). Solo lectura.")
    if not is_loopback(args.host):
        print(
            "Aviso: el panel no tiene contraseña; cualquiera que llegue a esta dirección "
            "podrá verlo."
        )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0

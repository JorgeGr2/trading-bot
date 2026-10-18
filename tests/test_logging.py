from __future__ import annotations

import logging

from copybot.logging import configure, redact


def test_stdlib_logs_are_redacted_and_httpx_is_quiet(capsys):
    configure()
    logging.getLogger("otra.libreria").warning(
        "POST %s", "https://mainnet.helius-rpc.com/?api-key=CLAVE-SECRETA-1"
    )
    logging.getLogger("httpx").info("HTTP Request: POST https://x/?api-key=CLAVE-SECRETA-2")
    out = capsys.readouterr().out
    assert "CLAVE-SECRETA-1" not in out
    assert "api-key=***" in out
    assert "CLAVE-SECRETA-2" not in out


def test_api_key_in_url_is_hidden():
    out = redact("POST https://mainnet.helius-rpc.com/?api-key=abc123def456 failed")
    assert "abc123def456" not in out
    assert "api-key=***" in out


def test_database_password_is_hidden():
    out = redact("postgresql+asyncpg://copybot:supersecreto@db:5432/copybot")
    assert "supersecreto" not in out
    assert out == "postgresql+asyncpg://copybot:***@db:5432/copybot"


def test_telegram_token_is_hidden():
    out = redact("https://api.telegram.org/bot123456789:AAH-abcdefghijklmnopqrstuvwxyz012/getMe")
    assert "AAH-abcdefghijklmnopqrstuvwxyz012" not in out


def test_normal_text_is_untouched():
    text = "firma 2DgeLNS5rf compra 10 USDC"
    assert redact(text) == text

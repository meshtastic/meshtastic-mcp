# SPDX-FileCopyrightText: Meshtastic contributors
# SPDX-License-Identifier: GPL-3.0-only

"""Serve the MCP surface over HTTP from the machine the radios are plugged into.

One process owns the serial ports, the recorder and the firmware tree, so concurrent clients
share a single arbiter instead of racing each other through stdio children. Every request must
carry the bearer token; there is no anonymous mode.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Mapping
from pathlib import Path

from fastmcp.server.auth import AccessToken, TokenVerifier

from .bridge import ServerAlreadyRunning as ServerAlreadyRunning

TOKEN_ENV = "MESHTASTIC_MCP_TOKEN"
TOKEN_FILE_ENV = "MESHTASTIC_MCP_TOKEN_FILE"
MIN_TOKEN_LEN = 32

_HINT = (
    f"generate one with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'` and "
    f"export {TOKEN_ENV} or point {TOKEN_FILE_ENV} at a 0600 file holding it"
)


class TokenConfigError(ValueError):
    """The bearer token is missing or too weak to serve with."""


def load_token(env: Mapping[str, str] | None = None) -> str:
    env = os.environ if env is None else env
    token = (env.get(TOKEN_ENV) or "").strip()
    if not token and env.get(TOKEN_FILE_ENV):
        path = Path(env[TOKEN_FILE_ENV]).expanduser()
        try:
            mode = path.stat().st_mode & 0o077
            token = path.read_text().strip()
        except OSError as exc:
            raise TokenConfigError(f"cannot read {path}: {exc}") from exc
        if mode:
            raise TokenConfigError(f"{path} is readable by group/other; chmod 600 it")
    if not token:
        raise TokenConfigError(f"no bearer token configured; {_HINT}")
    if len(token) < MIN_TOKEN_LEN:
        raise TokenConfigError(f"token is shorter than {MIN_TOKEN_LEN} characters; {_HINT}")
    return token


class BenchTokenVerifier(TokenVerifier):
    """Accept exactly one shared secret, compared in constant time."""

    def __init__(self, token: str) -> None:
        super().__init__()
        self._token = token.encode()

    async def verify_token(self, token: str) -> AccessToken | None:
        if hmac.compare_digest(token.encode(), self._token):
            return AccessToken(token=token, client_id="bench", scopes=[])
        return None


def serve(host: str, port: int, token: str) -> None:
    """Serve over HTTP, as this host's one server. Raises `ServerAlreadyRunning`."""
    from . import _version, bridge

    # Clients on this host find the server through the token file, never the token itself.
    from_env = bool((os.environ.get(TOKEN_ENV) or "").strip())
    token_file = None if from_env else os.environ.get(TOKEN_FILE_ENV)
    lock = bridge.claim_host(
        bridge.local_url(host, port),
        str(Path(token_file).expanduser().resolve()) if token_file else None,
        getattr(_version, "__version__", ""),
    )
    # The server spawns pio, install scripts, emulators and model servers; none needs the token.
    os.environ.pop(TOKEN_ENV, None)
    from .server import app

    app.auth = BenchTokenVerifier(token)
    try:
        app.run(transport="http", host=host, port=port)
    finally:
        lock.close()

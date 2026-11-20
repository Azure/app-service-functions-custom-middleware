"""Application authentication and library configuration."""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Callable
from typing import Any

_DEFAULT_TIMEOUT = 20.0


class AuthError(Exception):
    """The application credential or the web app's base URL is missing or unusable."""


# -- configuration ----------------------------------------------------------

#: Returns this app's own bearer for the web app's audience, or ``None``.
PresenterProvider = Callable[[], str | None]

_base_url: str | None = None
_presenter: PresenterProvider | None = None
_timeout: float = _DEFAULT_TIMEOUT


def configure(
    app: Any = None,
    *,
    base_url: str | None = None,
    presenter: PresenterProvider | None = None,
    timeout: float | None = None,
) -> None:
    """Configure the library, and optionally listen on every incoming request.

    All arguments are optional. With none, the base URL comes from ``APP_BASE_URL`` (or is derived
    from ``MCP_BASE_URL``) and the presenter is a managed identity built from ``MCP_AUTH_SCOPE`` and
    ``MCP_AUTH_CLIENT_ID``.

    Outbound requests use the application's configured credentials. Passing ``app`` a FastAPI
    app or an ``azure.functions.FunctionApp`` scopes the hook invocation to each request.

    Hosted Skills integration is enabled automatically when its runtime is installed. Configure
    this library **before** calling the runtime's ``create_function_app()``.
    """
    global _base_url, _presenter, _timeout
    if base_url is not None:
        _base_url = base_url.rstrip("/")
    if presenter is not None:
        _presenter = presenter
    if timeout is not None:
        _timeout = timeout
    if app is not None:
        # Imported here rather than at module scope: listening is about requests, which is the
        # layer above this one.
        from .hooks import listen

        listen(app)

    from . import azure_functions_agents_integration

    if azure_functions_agents_integration.is_available():
        azure_functions_agents_integration.enable()


def app_base_url() -> str:
    """The web app's origin."""
    if _base_url:
        return _base_url

    explicit = (os.environ.get("APP_BASE_URL") or "").strip()
    if explicit:
        return explicit.rstrip("/")

    mcp_base = (os.environ.get("MCP_BASE_URL") or "").strip()
    if mcp_base:
        parsed = urllib.parse.urlparse(mcp_base)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme}://{parsed.netloc}"

    raise AuthError(
        "app base URL not configured; set APP_BASE_URL or MCP_BASE_URL, "
        "or call configure(base_url=...)"
    )


def request_timeout() -> float:
    """Seconds to wait on a call to the web app's own API."""
    return _timeout


_credential: Any = None


def _managed_identity_presenter() -> str | None:
    """Default presenter. The credential is built once so its token cache survives between turns."""
    global _credential

    scope = (os.environ.get("MCP_AUTH_SCOPE") or "").strip()
    if not scope:
        return None

    if _credential is None:
        from azure.identity import ManagedIdentityCredential

        client_id = (os.environ.get("MCP_AUTH_CLIENT_ID") or "").strip()
        _credential = (
            ManagedIdentityCredential(client_id=client_id)
            if client_id
            else ManagedIdentityCredential()
        )

    return _credential.get_token(scope).token


def presenter_token() -> str:
    """This app's own bearer for the web app's audience -- proof of which application is asking."""
    provider = _presenter or _managed_identity_presenter
    try:
        token = provider()
    except Exception as exc:
        raise AuthError(f"presenter credential could not be acquired: {exc}") from exc

    if not token:
        raise AuthError(
            "no presenter credential; set MCP_AUTH_SCOPE (and MCP_AUTH_CLIENT_ID for a "
            "user-assigned identity), or call configure(presenter=...)"
        )
    return token

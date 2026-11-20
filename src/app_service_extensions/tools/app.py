"""Call the web app's own API with application credentials, from code or as an agent tool."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ..auth import AuthError, app_base_url, presenter_token, request_timeout
from ..hooks import HOOK_INVOCATION_HEADER, hook_invocation, logger
from .request import _maybe_json

#: Headers call_web_app() sets itself; callers can't override them.
_MANAGED_HEADERS = frozenset({"authorization", HOOK_INVOCATION_HEADER.lower()})


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    """Refuse redirects: urllib would resend the application credential to the redirect target."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001, ANN201
        return None


_opener = urllib.request.build_opener(_NoRedirects)


class AppCallError(Exception):
    """A call to the web app's own API failed.

    ``status`` is the HTTP status when the call reached the app, and ``None`` when it did not. The
    app's own body is on the chained ``__cause__``, for a caller that wants it.
    """

    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def call_web_app(
    method: str,
    path: str,
    *,
    params: dict[str, Any] | None = None,
    body: Any = None,
    headers: dict[str, str] | None = None,
) -> str:
    """Call the configured web app with application credentials; return response text or raise error.

    A ``bytes`` body is sent as is; other non-``None`` bodies are sent as JSON.
    """
    headers = headers or {}
    for name in headers:
        if name.lower() in _MANAGED_HEADERS:
            raise ValueError(f"{name} is managed by call_web_app() and cannot be overridden")

    verb = method.upper()
    url = f"{app_base_url()}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    sends_json = body is not None and not isinstance(body, bytes)
    data = json.dumps(body).encode("utf-8") if sends_json else body

    request = urllib.request.Request(url, data=data, method=verb)
    request.add_header("Authorization", f"Bearer {presenter_token()}")
    invocation = hook_invocation()
    if invocation:
        request.add_header(HOOK_INVOCATION_HEADER, invocation)
    request.add_header("Accept", "application/json")
    if sends_json:
        request.add_header("Content-Type", "application/json")
    for name, value in headers.items():
        request.add_header(name, value)

    try:
        with _opener.open(request, timeout=request_timeout()) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise AppCallError(f"{verb} {path} returned {exc.code}", exc.code) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise AppCallError(f"could not reach the web app at {url}: {exc}") from exc

    return raw.decode("utf-8", errors="replace")


def call_web_app_wrapper(method: str, path: str, body: str = "") -> str:
    """Call the web app via ``call_web_app()`` and return readable success or error messages."""
    try:
        answer = call_web_app(method, path, body=_maybe_json(body) if body else None)
    except AppCallError as err:
        return f"Web app request failed: {err}"
    except AuthError as err:
        return f"Web app authentication or configuration failed: {err}"
    except Exception:  # noqa: BLE001 - a tool answers a model, so nothing reaches it as a raise.
        logger.warning("call_web_app_wrapper failed unexpectedly", exc_info=True)
        return "The web app call failed unexpectedly."

    if not answer:
        return f"{method.upper()} {path} succeeded with an empty response body."
    return f"{method.upper()} {path} returned:\n\n{answer}"


APP_TOOLS = [call_web_app_wrapper]

__all__ = ["APP_TOOLS", "AppCallError", "call_web_app", "call_web_app_wrapper"]

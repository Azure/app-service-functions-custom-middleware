"""The inbound side: reading a hook, the framework adapters, and per-request listening."""

from __future__ import annotations

import functools
import inspect
import json
import logging
from collections.abc import Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Iterator
from urllib.parse import parse_qsl, urlparse

if TYPE_CHECKING:
    from .rewrite_request import EditableRequest

logger = logging.getLogger("azure.functions.AppServiceExtensions")

#: Namespace EasyAuth reserves for the headers it injects on a hook.
HOOK_HEADER_PREFIX = "X-MS-Hook-"

HOOK_NAME_HEADER = HOOK_HEADER_PREFIX + "Name"
HOOK_MODE_HEADER = HOOK_HEADER_PREFIX + "Mode"
HOOK_INVOCATION_HEADER = HOOK_HEADER_PREFIX + "Invocation"
HOOK_ORIGIN_HEADER = HOOK_HEADER_PREFIX + "Origin"
HOOK_PATH_HEADER = HOOK_HEADER_PREFIX + "Path"
HOOK_ROUTE_HEADER = HOOK_HEADER_PREFIX + "Route"

#: The method the client used.
HOOK_REQUEST_METHOD_HEADER = HOOK_HEADER_PREFIX + "Request-Method"

#: The hook's access key for the handler endpoint.
FUNCTION_KEY_HEADER = "x-functions-key"

# The signed callback chain is per request. It must not be exposed to the model, but every callback
# to the originating site must preserve it.
_hook_invocation: ContextVar[str | None] = ContextVar("ase_hook_invocation", default=None)


#: The one mode whose response is the user's own, rather than a patch for the middleware to apply.
MODE_PROXY = "proxy"


def _mode_guidance(mode: str | None) -> str:
    if (mode or "").lower() == "intercept":
        return (
            "This is an intercept hook: the app is waiting for your decision. "
            "Call passthrough() to let the original request through, use the request edit tools "
            "to modify it before the app handles it, or call respond() to return an HTTP response "
            "without running the app. A text reply alone is not a decision."
        )
    if (mode or "").lower() == "mirror":
        return (
            "This is a mirror hook: you received a copy of the request while the original "
            "continues independently. Your reply and request edits cannot change the original "
            "request or its response. Do not call request decision tools such as respond() or "
            "passthrough() to control it; other tools may be used for independent follow-up work."
        )
    return ""


# Reserve platform namespaces instead of listing internal header names.
_SENSITIVE_HEADERS = frozenset(
    {
        FUNCTION_KEY_HEADER,
        "api-key",
        "authorization",
        "cookie",
        "ocp-apim-subscription-key",
        "proxy-authenticate",
        "proxy-authorization",
        "x-api-key",
        "x-arr-clientcert",
        "x-zumo-auth",
    }
)

_SENSITIVE_HEADER_PREFIXES = ("x-ms-", "mwh-")


def _normalized(name: str) -> str:
    """Normalize a header name the way EasyAuth compares them."""
    return name.replace("_", "-").strip().lower()


def _is_sensitive(name: str) -> bool:
    """True for credentials and reserved platform header namespaces."""
    normalized = _normalized(name)
    return normalized in _SENSITIVE_HEADERS or normalized.startswith(_SENSITIVE_HEADER_PREFIXES)


def _is_credential(name: str) -> bool:
    """True if the header must be hidden from a model, even on traffic EasyAuth never filtered."""
    normalized = _normalized(name)
    return (
        _is_sensitive(normalized)
        or normalized.endswith("-token")
        or normalized == HOOK_INVOCATION_HEADER.lower()
    )


def _without_credentials(headers: Mapping[str, str]) -> dict[str, str]:
    """A copy of headers without credential or reserved-platform headers."""
    return {name: value for name, value in headers.items() if not _is_credential(name)}


def _lookup(headers: Mapping[str, str] | None, name: str) -> str | None:
    """Read a header case-insensitively. ``headers`` may be ``None``."""
    if not headers:
        return None

    value = headers.get(name)
    if value is not None:
        return value

    lowered = name.lower()
    return next((v for k, v in headers.items() if k.lower() == lowered), None)


@dataclass(frozen=True)
class HookRequest:
    """One request forwarded by an EasyAuth hook."""

    #: The ``name`` of the hook rule that matched.
    hook: str | None = None
    #: The hook's mode: ``mirror``, ``intercept`` or ``proxy``.
    mode: str | None = None
    #: The path the client actually requested, as opposed to the handler's own URL.
    path: str | None = None
    #: The route template that matched, e.g. ``/api/expenses/{id}/approve``.
    route: str | None = None
    #: The proxy-aware, client-facing origin of the site that dispatched the hook.
    origin: str | None = None
    #: The client's HTTP method, forwarded verbatim.
    method: str | None = None
    #: The client's query string, without a leading ``?``.
    query: str = ""
    #: The client's content type, forwarded verbatim.
    content_type: str | None = None
    #: The client's body, decoded as text and left exactly as sent.
    body: str | None = None
    #: Forwarded headers, excluding credentials and reserved platform namespaces.
    headers: Mapping[str, str] = field(default_factory=dict)
    #: The signed hook-name chain that callbacks to the originating site must preserve.
    invocation: str | None = None

    @classmethod
    def from_http(
        cls,
        *,
        method: str | None = None,
        headers: Mapping[str, str] | None = None,
        body: str | bytes | None = None,
        query: str = "",
        content_type: str | None = None,
    ) -> HookRequest:
        """Build a :class:`HookRequest` from a raw inbound request."""
        raw_headers: Mapping[str, str] = headers or {}
        text_body = body.decode("utf-8", errors="replace") if isinstance(body, bytes) else body
        # Prefer the header: mirror and intercept hooks always arrive as POST.
        client_method = _lookup(raw_headers, HOOK_REQUEST_METHOD_HEADER) or method

        return cls(
            hook=_lookup(raw_headers, HOOK_NAME_HEADER),
            mode=_lookup(raw_headers, HOOK_MODE_HEADER),
            path=_lookup(raw_headers, HOOK_PATH_HEADER),
            route=_lookup(raw_headers, HOOK_ROUTE_HEADER),
            origin=_lookup(raw_headers, HOOK_ORIGIN_HEADER),
            method=client_method.upper() if client_method else None,
            query=query.lstrip("?"),
            content_type=content_type or _lookup(raw_headers, "Content-Type"),
            body=text_body,
            headers=_without_credentials(raw_headers),
            invocation=_lookup(raw_headers, HOOK_INVOCATION_HEADER),
        )

    @property
    def is_hook(self) -> bool:
        return bool(self.hook or self.mode)

    @property
    def answers_with_patch(self) -> bool:
        """True when the middleware reads this handler's response as a patch."""
        return self.is_hook and (self.mode or "").lower() == "intercept"

    @property
    def query_params(self) -> dict[str, str]:
        """:attr:`query` parsed into a mapping, keeping blank values."""
        return dict(parse_qsl(self.query, keep_blank_values=True))

    def envelope(self) -> dict[str, Any]:
        """This request as one JSON-serializable object.

        Credential and reserved platform headers are left out.
        """
        return {
            "hook": self.hook,
            "mode": self.mode,
            "method": self.method,
            "path": self.path,
            "route": self.route,
            "origin": self.origin,
            "query": self.query,
            "contentType": self.content_type,
            "headers": _without_credentials(self.headers),
            "body": self.body,
        }

    def to_prompt(self) -> str:
        """Render this request as the prompt text an agent reasons about."""
        rendered = json.dumps(self.envelope(), ensure_ascii=False, indent=2, default=str)
        kind = {"mirror": "Mirrored", MODE_PROXY: "Proxied"}.get(
            (self.mode or "").lower(), "Intercepted"
        )
        guidance = _mode_guidance(self.mode)
        if guidance:
            guidance += "\n\n"
        return f"{kind} HTTP request:\n{guidance}```json\n{rendered}\n```"

    def edit(self) -> EditableRequest:
        """Open a mutable view of this request that derives its own patch."""
        from .rewrite_request import EditableRequest

        return EditableRequest(self)


# -- adapters ---------------------------------------------------------------
#
# One hook, read from whichever host is serving it: Starlette and FastAPI, the Azure Functions
# FastAPI extension, ``azure.functions.HttpRequest``, and a raw ASGI scope. Each keeps the same
# request somewhere different, and the rest of the library sees only a HookRequest.


def _adopt(hook: HookRequest) -> HookRequest:
    """Set the current callback chain from ``hook``, and return ``hook``."""
    _adopt_hook_invocation(hook.invocation)
    return hook


def _adopt_hook_invocation(token: str | None) -> None:
    """Set the callback chain for the request currently being served."""
    _hook_invocation.set(token)


def hook_invocation() -> str | None:
    """The signed callback chain for the request currently being served, if one arrived."""
    return _hook_invocation.get()


@contextmanager
def _use_hook_invocation(token: str | None) -> Iterator[None]:
    """Internal test helper: use one callback chain for a block, then restore the prior value."""
    reset = _hook_invocation.set(token)
    try:
        yield
    finally:
        _hook_invocation.reset(reset)


def from_asgi(request: Any, body: str | bytes | None = None) -> HookRequest:
    """Adapt a Starlette or FastAPI ``Request``. Also sets the current callback chain."""
    headers = dict(request.headers)
    return _adopt(
        HookRequest.from_http(
            method=request.method,
            headers=headers,
            body=body,
            query=request.url.query,
            content_type=headers.get("content-type"),
        )
    )


def from_functions(request: Any) -> HookRequest:
    """Adapt an ``azure.functions.HttpRequest``. Also sets the current callback chain."""
    headers = dict(request.headers)
    return _adopt(
        HookRequest.from_http(
            method=request.method,
            headers=headers,
            body=request.get_body(),
            query=urlparse(getattr(request, "url", "") or "").query,
            content_type=headers.get("content-type"),
        )
    )


# -- the request being served -----------------------------------------------

_current: ContextVar[EditableRequest | None] = ContextVar("ase_current_request", default=None)


class NoHookError(RuntimeError):
    """Raised by :func:`current_request` when no hook is being handled."""


def current_request() -> EditableRequest:
    """The hook being handled, ready to edit.

    The same call on every host, so a handler names no framework type.
    """
    request = _current.get()
    if request is None:
        raise NoHookError(
            f"No hook is being handled here; current_request() answers on a request the "
            f"middleware forwarded, which is one carrying {HOOK_NAME_HEADER}."
        )
    return request


def hold_request(hook: HookRequest) -> EditableRequest:
    """Make ``hook`` the request :func:`current_request` returns, as an editable view."""
    editable = hook.edit()
    _current.set(editable)
    return editable


def _clear_hook(invocation: str | None = None) -> None:
    """Clear the held request and set the callback chain to ``invocation``."""
    _adopt_hook_invocation(invocation)
    _current.set(None)


def listen(app: Any) -> None:
    """Scope each request's callback chain and hold every hook ready to edit."""
    if hasattr(app, "add_middleware"):
        app.add_middleware(_Middleware)
    else:
        _wrap_function_app(app)


class _Middleware:
    """ASGI middleware that preserves the callback chain and reads a hook's body."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> Any:
        if scope.get("type") != "http":
            return await self.app(scope, receive, send)

        try:
            _clear_hook(_scope_header(scope, HOOK_INVOCATION_HEADER))
            if _scope_header(scope, HOOK_NAME_HEADER):
                body, receive = await _buffer_body(receive)
                hold_request(_from_scope(scope, body))
            return await self.app(scope, receive, send)
        finally:
            _clear_hook()


async def _buffer_body(receive: Any) -> tuple[bytes, Any]:
    """Read the whole request body and return it with a ``receive`` that can serve it again."""
    messages: list[dict[str, Any]] = []
    while True:
        message = await receive()
        messages.append(message)
        if message.get("type") != "http.request" or not message.get("more_body", False):
            break

    body = b"".join(m.get("body", b"") for m in messages if m.get("type") == "http.request")
    pending = iter(messages)

    async def replay() -> Any:
        try:
            return next(pending)
        except StopIteration:
            return await receive()

    return body, replay


def _from_scope(scope: Any, body: bytes) -> HookRequest:
    """Build a hook from a raw ASGI scope, without needing a framework request object."""
    headers = {
        key.decode("latin-1"): value.decode("latin-1") for key, value in scope.get("headers") or []
    }
    return _adopt(
        HookRequest.from_http(
            method=scope.get("method"),
            headers=headers,
            body=body,
            query=(scope.get("query_string") or b"").decode("latin-1"),
            content_type=headers.get("content-type"),
        )
    )


def _scope_header(scope: Any, name: str) -> str | None:
    """Read one header off an ASGI scope."""
    wanted = name.lower().encode()
    found = next((v for k, v in scope.get("headers") or [] if k.lower() == wanted), None)
    return found.decode("latin-1") if found else None


def _wrap_function_app(app: Any) -> None:
    """Wrap every function this app registers from now on, so each call gets its own hook state."""
    if getattr(app, "_ase_listening", False):
        return

    # Every decorator calls the SDK's private _validate_type. Wrapping only raw functions there
    # (not FunctionBuilders) wraps each function exactly once.
    validate = getattr(app, "_validate_type", None)
    if not callable(validate):
        raise TypeError(
            f"don't know how to listen on {type(app).__name__}; pass a FastAPI or Starlette app, "
            "or an azure.functions.FunctionApp"
        )

    from azure.functions.decorators.function_app import FunctionBuilder

    def _validate_type(func: Any) -> Any:
        if callable(func) and not isinstance(func, FunctionBuilder):
            func = _prepared(func)
        return validate(func)

    app._validate_type = _validate_type
    app._ase_listening = True


_UNREADABLE = (
    "Could not read the inbound request; the callback chain was preserved but "
    "current_request() will not be available"
)


def _prepare(request: Any) -> None:
    """Set up a call's hook state: its callback chain and, for a hook, the request to edit."""
    if _header_of(request, HOOK_NAME_HEADER) and not _is_async_request(request):
        try:
            hold_request(from_functions(request))
            return
        except Exception:  # noqa: BLE001 - don't fail the call over a request we can't read.
            logger.warning(_UNREADABLE, exc_info=True)

    _clear_hook(_header_of(request, HOOK_INVOCATION_HEADER))


async def _prepare_async(request: Any) -> None:
    """Same as :func:`_prepare`, for a request whose body has to be awaited (Starlette's)."""
    if _header_of(request, HOOK_NAME_HEADER) and _is_async_request(request):
        try:
            hold_request(from_asgi(request, await request.body()))
            return
        except Exception:  # noqa: BLE001 - same reason as in _prepare.
            logger.warning(_UNREADABLE, exc_info=True)

    _prepare(request)


def _is_async_request(request: Any) -> bool:
    """True if the request's body has to be awaited, as with Starlette."""
    return inspect.iscoroutinefunction(getattr(request, "body", None))


def _prepared(handler: Any) -> Any:
    """Keep hook state only for the function call."""
    # functools.wraps keeps the parameter names, which the worker uses to pass arguments.
    if inspect.iscoroutinefunction(handler):

        @functools.wraps(handler)
        async def entry(*call_args: Any, **call_kwargs: Any) -> Any:
            try:
                await _prepare_async(_request_among(call_args, call_kwargs))
                return _as_http_response(await handler(*call_args, **call_kwargs))
            finally:
                _clear_hook()

    else:
        # Sync stays sync.
        @functools.wraps(handler)
        def entry(*call_args: Any, **call_kwargs: Any) -> Any:
            try:
                _prepare(_request_among(call_args, call_kwargs))
                return _as_http_response(handler(*call_args, **call_kwargs))
            finally:
                _clear_hook()

    return entry


def _as_http_response(result: Any) -> Any:
    """Return an :class:`EditableRequest` as an HTTP response, and anything else unchanged."""
    from .rewrite_request import EditableRequest

    if not isinstance(result, EditableRequest):
        return result

    body = result.to_json()
    # The worker rejects azure.functions.HttpResponse once the FastAPI extension is loaded.
    if _http_v2_enabled():
        from azurefunctions.extensions.http.fastapi import Response

        return Response(content=body, status_code=200, media_type="application/json")

    import azure.functions as func

    return func.HttpResponse(body, status_code=200, mimetype="application/json")


def _http_v2_enabled() -> bool:
    """True if the FastAPI HTTP extension is loaded."""
    try:
        from azurefunctions.extensions.base import HttpV2FeatureChecker
    except ImportError:
        return False
    return HttpV2FeatureChecker.http_v2_enabled()


def _request_among(args: Any, kwargs: Any) -> Any:
    """The HTTP request among a handler's arguments, or ``None`` for a trigger that has none.

    Found by shape rather than by position or name: the worker binds by the handler's own parameter
    names, which this library does not get to choose.
    """
    for value in (*args, *kwargs.values()):
        if hasattr(value, "headers"):
            return value
    return None


def _header_of(request: Any, name: str) -> str | None:
    """Read one header off a host's request object, case-insensitively."""
    return _lookup(getattr(request, "headers", None), name)

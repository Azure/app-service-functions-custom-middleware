"""Hook prompts and the runtime functions that deliver them."""

from __future__ import annotations

import functools
import inspect
import json
from typing import Any

from ..hooks import from_asgi, hold_request
from . import logger, replace_runtime_reference
from ._answer import answer_with, collect, failed_answer, succeeded
from ._mcp_request_scope import RequestScope, _missing_functions, finish


class _PreparedRequest:
    """The inbound request, with its body replaced by the description the runtime should reason about."""

    def __init__(self, request: Any, payload: Any) -> None:
        self._request = request
        self._payload = payload

    # Everything the runtime asks for comes from the real request...
    def __getattr__(self, name: str) -> Any:
        return getattr(self._request, name)

    # ...except the body, which is the one thing we are here to change.
    async def json(self) -> Any:
        return self._payload

    async def body(self) -> bytes:
        return json.dumps(self._payload).encode()


def validate_prompt() -> None:
    """Check prompt support; built-in chat routes are optional."""
    required: dict[str, tuple[str, ...]] = {
        "azure_functions_agents.registration._handlers": ("make_http_agent_handler",),
    }
    try:
        from azure_functions_agents.registration import endpoints
    except ImportError:
        pass
    else:
        found = tuple(name for name in _CHAT_REGISTRARS if hasattr(endpoints, name))
        if found:
            required["azure_functions_agents.registration.endpoints"] = found

    missing = _missing_functions(required)
    if missing:
        raise RuntimeError(
            "app-service-functions-custom-middleware could not find the runtime functions "
            f"needed to extend hook prompts ({', '.join(missing)}). "
            "Upgrade app-service-functions-custom-middleware, or pass "
            "enable(prompt=False) to skip prompt integration."
        )


def patch_prompt() -> None:
    """Make an HTTP-triggered agent see the whole request, as the prompt."""
    validate_prompt()
    _patch_agent_trigger()
    _patch_builtin_endpoints()


# -- one turn, wherever it arrived ------------------------------------------


def _as_envelope(hook: Any) -> Any:
    """An agent's own route reads the body itself, so the hook is the body."""
    return hook.envelope()


def _as_chat_prompt(hook: Any) -> Any:
    """The chat routes read a ``prompt`` key, so the hook is rendered into one.

    Without this an agent there reasons about a body the client never sent -- empty on a GET.
    """
    return {"prompt": hook.to_prompt()}


def _serving(handler: Any, as_payload: Any) -> Any:
    """Run one turn with request-scoped callbacks and the hook as its prompt."""

    @functools.wraps(handler)
    async def serve(req: Any, *rest: Any, **named: Any) -> Any:
        scope = RequestScope.from_request(req)
        scope.adopt()
        try:
            hook = from_asgi(req, await req.body())
            if not hook.answers_with_patch:
                # A direct call or a proxy hook: the agent's own answer is the response.
                return await finish(await handler(req, *rest, **named), scope)

            # Held so the request tools can reach it: an agent edits the hook by calling
            # them, and what it edits has to be the same object this handler answers with.
            editable = hold_request(hook)

            # Run the runtime's own handler, with the hook standing in for the body it reads.
            response = await handler(_PreparedRequest(req, as_payload(hook)), *rest, **named)

            # A streaming handler returns before the agent has run, so nothing it decides has
            # happened yet.
            if getattr(response, "body_iterator", None) is not None:
                response = await collect(response, scope)

            if scope.failed or not succeeded(response):
                # The run failed: drop its edits so Hooks applies the hook's failurePolicy.
                return await finish(failed_answer(response, editable), scope)

            if not editable.touched:
                # The agent decided in prose rather than with the tools.
                return await finish(response, scope)

            # Tools were used, so the patch they built is the answer.
            return await finish(answer_with(response, editable), scope)
        except BaseException:
            # However this ended, hook state must not outlive it on a pooled worker.
            await scope.close()
            raise

    return serve


# -- an agent's own route --------------------------------------------------


def _patch_agent_trigger() -> None:
    """Give an agent's own route the whole incoming request and headers as its prompt."""
    from azure_functions_agents.registration import _handlers

    original = _handlers.make_http_agent_handler

    @functools.wraps(original)
    def make_http_agent_handler(*args: Any, **kwargs: Any) -> Any:
        # Let the runtime build its handler, whatever its signature, then wrap it.
        return _serving(original(*args, **kwargs), _as_envelope)

    # triggers.py imports the factory by name, so it holds a second reference to the original.
    replace_runtime_reference(
        _handlers,
        "make_http_agent_handler",
        make_http_agent_handler,
        "azure_functions_agents.registration.triggers",
    )


# -- the built-in chat routes ----------------------------------------------

#: Wrap these functions to add hook support to the chat handlers they create.
_CHAT_REGISTRARS = ("_register_http_chat", "_register_http_chat_stream")


def _patch_builtin_endpoints() -> None:
    """Scope callbacks on the runtime's built-in chat endpoints and let them handle a hook."""
    # Find the module, or leave the chat routes alone.
    try:
        from azure_functions_agents.registration import endpoints
    except ImportError:
        return

    # Both registrars are optional, but losing all of them is worth saying out loud.
    found = [name for name in _CHAT_REGISTRARS if hasattr(endpoints, name)]
    if not found:
        logger.warning(
            "Hosted Skills built-in chat endpoints were not recognised; hook prompts and "
            "request-scoped callbacks will not be available there"
        )
        return

    # Swap each one for a version that wraps whatever it registers.
    for name in found:
        setattr(endpoints, name, _adopting_registrar(getattr(endpoints, name)))


def _adopting_registrar(register: Any) -> Any:
    """Wrap a registrar so the route it registers scopes hook state."""

    @functools.wraps(register)
    def registering(app: Any, *args: Any, **kwargs: Any) -> Any:
        original = app.route
        shadowed = "route" in vars(app)

        # Wrap whatever handler the runtime registers through app.route.
        def route(*route_args: Any, **route_kwargs: Any) -> Any:
            decorate = original(*route_args, **route_kwargs)
            return lambda handler: decorate(_adopting(handler))

        app.route = route
        try:
            return register(app, *args, **kwargs)
        finally:
            # Undo our override, leaving app.route exactly as it was.
            if shadowed:
                app.route = original
            else:
                del app.route

    return registering


def _adopting(handler: Any) -> Any:
    """Wrap a chat handler so callbacks are scoped and hooks are answered with patches."""
    if not inspect.iscoroutinefunction(handler):
        # The chat page is a synchronous GET. Wrapping it as a coroutine would move it off the
        # worker's thread pool onto the event loop, and it has no hook to answer anyway.
        return handler

    return _serving(handler, _as_chat_prompt)

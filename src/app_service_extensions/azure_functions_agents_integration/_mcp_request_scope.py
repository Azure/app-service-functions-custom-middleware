"""Request-scoped MCP sessions and same-site hook recursion guards."""

from __future__ import annotations

import functools
import importlib
import inspect
import json
import urllib.parse
from collections.abc import Iterable
from contextvars import ContextVar
from typing import Any

from ..auth import AuthError, app_base_url
from ..hooks import (
    HOOK_INVOCATION_HEADER,
    _adopt_hook_invocation,
    _clear_hook,
    _lookup,
    hook_invocation,
)
from . import logger, replace_runtime_reference

#: State of the request being handled now; None outside a request (e.g. at start-up).
_request_scope: ContextVar[RequestScope | None] = ContextVar("ase_request_scope", default=None)


class RequestScope:
    """Per-request state: X-MS-Hook-Invocation, MCP sessions, and whether the run failed."""

    def __init__(self, invocation: str | None) -> None:
        self._invocation = invocation
        self._tools: list[Any] = []
        #: True if an agent run in this request raised or reported an error.
        self.failed = False

    @classmethod
    def from_request(cls, request: Any) -> RequestScope:
        """The scope for an inbound request, read off its headers."""
        return cls(_lookup(getattr(request, "headers", None), HOOK_INVOCATION_HEADER))

    def adopt(self) -> None:
        """Make this the current request in this context.

        Streams call this again: their body is read in a different context.
        """
        _adopt_hook_invocation(self._invocation)
        _request_scope.set(self)

    def register(self, tools: Iterable[Any]) -> None:
        """Close these MCP tools when the request ends."""
        self._tools.extend(tools)

    def mark_failed(self) -> None:
        self.failed = True

    async def close(self) -> None:
        """End the request: clear its hook state and close its sessions."""
        tools, self._tools = self._tools, []
        _request_scope.set(None)
        _clear_hook()

        for tool in tools:
            try:
                await tool.close()
            except Exception:  # noqa: BLE001 - a session we cannot close must not fail the turn.
                logger.warning("An MCP session could not be closed", exc_info=True)
            await _close_client_of(tool)


async def _close_client_of(tool: Any) -> None:
    """Dispose the HTTP client built for a tool, which closing the tool does not.

    The runtime hands each tool its own ``AsyncClient`` for request headers, and MAF's ``close()``
    only unwinds the session on its lifecycle-owner task -- nothing disposes that client.
    Rebuilding a session per turn must not leak that connection pool.
    """
    client = getattr(tool, "_httpx_client", None)
    aclose = getattr(client, "aclose", None)
    if aclose is None:
        return
    try:
        await aclose()
    except Exception:  # noqa: BLE001 - a pool we cannot close must not fail the turn either.
        logger.warning("An MCP HTTP client could not be closed", exc_info=True)


async def finish(response: Any, scope: RequestScope) -> Any:
    """Close the scope, or hand it to the response's stream when the turn is still running."""
    iterator = getattr(response, "body_iterator", None)
    if iterator is None:
        await scope.close()
        return response

    async def streamed() -> Any:
        scope.adopt()
        try:
            async for chunk in iterator:
                yield chunk
        finally:
            await scope.close()

    try:
        response.body_iterator = streamed()
    except Exception:  # noqa: BLE001 - a response we cannot re-wrap still has to be answered with.
        logger.warning("Could not extend the request scope over a streaming response")
        await scope.close()

    return response


# -- preserving the app's own callback chain --------------------------------

#: Private runtime functions this file patches. Refuse to start if any is missing.
_REQUIRED_CALLBACK_FUNCTIONS = {
    "azure_functions_agents.discovery.mcp": (
        "_build_header_provider",
        "_build_http_client",
        "discover_mcp_servers",
        "clear_mcp_cache",
    ),
    "azure_functions_agents.runner": ("run_agent", "run_agent_stream", "get_app_root"),
}

_REQUIRED_DESCRIPTOR_FUNCTIONS = {
    "azure_functions_agents.harness.agent_framework._maf_mcp": (
        "build_maf_mcp_tools",
        "build_mcp_header_provider",
        "_OwnedHTTPClientMCPStreamableHTTPTool",
    ),
    "azure_functions_agents.runner": ("run_agent", "run_agent_stream", "get_agent_runner"),
}


def validate_hook_forwarding() -> None:
    """Check callback prerequisites without replacing runtime functions."""
    required = (
        _REQUIRED_DESCRIPTOR_FUNCTIONS
        if _uses_mcp_descriptors()
        else _REQUIRED_CALLBACK_FUNCTIONS
    )
    missing = _missing_functions(required)
    if missing:
        raise RuntimeError(
            "app-service-functions-custom-middleware could not find the runtime functions "
            f"needed to preserve hook recursion guards ({', '.join(missing)}). "
            "Upgrade app-service-functions-custom-middleware, or pass "
            "enable(callbacks=False) to accept it."
        )


def patch_hook_forwarding() -> None:
    """Preserve callback recursion guards and scope MCP sessions to each request."""
    validate_hook_forwarding()
    if _uses_mcp_descriptors():
        from ._mcp_request_scope_maf import patch_descriptor_runtime

        patch_descriptor_runtime()
        return

    _patch_mcp_header_provider()
    _patch_mcp_http_client()
    _patch_mcp_connect()
    _patch_mcp_sessions()


def _missing_functions(required: dict[str, tuple[str, ...]]) -> list[str]:
    missing: list[str] = []
    for module_name, attributes in required.items():
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            missing.append(module_name)
            continue
        missing.extend(
            f"{module_name}.{name}"
            for name in attributes
            if not callable(getattr(module, name, None))
        )
    return missing


def _uses_mcp_descriptors() -> bool:
    try:
        from azure_functions_agents.discovery import mcp
    except ImportError:
        return False
    return isinstance(getattr(mcp, "MCPServerDescriptor", None), type)


#: Distinguishes "no provider cached yet" from a cached ``None``, the runtime's answer for a server
#: that needs no headers -- and what decides whether it builds an HTTP client at all.
_UNSET = object()


def _patch_mcp_header_provider() -> None:
    """Send X-MS-Hook-Invocation only to MCP servers on this site. Auth headers are unchanged."""
    from azure_functions_agents.discovery import mcp as runtime_mcp

    original = runtime_mcp._build_header_provider

    # One provider per distinct server config, for as long as this patch is installed.
    #
    # The runtime builds a ``DefaultAzureCredential`` and an empty token cache inside each provider
    # it makes, then reuses that provider for the life of the process -- so building one per request
    # is nowhere in its design. Rebuilding MCP sessions on every turn
    # (``_patch_agent_runs``) calls the builder again, so without this each request walked the
    # whole credential chain and re-fetched a token the previous request already held.
    #
    # Scoped to the closure rather than the module so it cannot outlive the builder it was filled
    # from: a second patch, over a reloaded runtime, starts empty rather than handing back providers
    # built by the old one.
    providers: dict[str, Any] = {}

    def shared(effective: Any) -> Any:
        """The runtime's provider for this server, built once and then reused.

        Safe to share because everything a provider returns is this *application's* -- its managed
        identity bearer, and any static headers from the config. Request context is added by the
        wrapper below and is never cached here.

        A racing thread may build a second provider; both are valid and one is simply discarded.
        """
        try:
            key = json.dumps(effective, sort_keys=True, default=str)
        except Exception:  # noqa: BLE001 - unkeyable config: correct, just not cached.
            return original(effective)

        provider = providers.get(key, _UNSET)
        if provider is _UNSET:
            # The first turn pays for the credential chain; no later one does.
            provider = original(effective)
            providers[key] = provider

        return provider

    @functools.wraps(original)
    def _build_header_provider(server: Any) -> Any:
        provider = shared(server)
        if not _is_same_site(server):
            # Somebody else's server is not ours to touch -- including leaving the runtime's own
            # answer alone when it is ``None``, which decides whether it builds an HTTP client.
            return provider

        return _SameSiteHeaders(provider, server["url"])

    runtime_mcp._build_header_provider = _build_header_provider


class _SameSiteHeaders:
    """The runtime's headers for an MCP server on this site, plus X-MS-Hook-Invocation."""

    def __init__(self, provider: Any, url: str) -> None:
        self._provider = provider
        self.url = url

    def __call__(self, ctx: Any) -> dict[str, str]:
        headers = dict(self._provider(ctx)) if self._provider is not None else {}
        invocation = hook_invocation()
        if invocation:
            headers[HOOK_INVOCATION_HEADER] = invocation
        return headers


def _is_same_site(server: Any) -> bool:
    """True if the MCP server's scheme, host and port match the app's base URL (APP_BASE_URL)."""
    return _is_same_site_url(server.get("url") if isinstance(server, dict) else None)


def _is_same_site_url(url: Any) -> bool:
    origin = _origin(url)
    if origin is None:
        return False

    # Use the configured URL, not X-MS-Hook-Origin: clients can influence that header.
    try:
        site = _origin(app_base_url())
    except AuthError:
        site = None
    if site is None:
        _report_unconfigured_origin()
        return False

    return origin == site


def _origin(url: Any) -> tuple[str, str, int] | None:
    """Split an http(s) URL into (scheme, host, port). None if it isn't one."""
    if not isinstance(url, str):
        return None
    try:
        parts = urllib.parse.urlsplit(url.strip())
        port = parts.port
        host = parts.hostname
        # Compare non-ASCII hosts in punycode, as clients send them.
        if host and not host.isascii():
            host = host.encode("idna").decode("ascii")
    except (UnicodeError, ValueError):
        return None

    scheme = parts.scheme.lower()
    if scheme not in ("http", "https") or not host:
        return None
    return scheme, host, port or (443 if scheme == "https" else 80)


@functools.cache
def _report_unconfigured_origin() -> None:
    """Warn once that no base URL is set, so X-MS-Hook-Invocation is sent nowhere."""
    logger.warning(
        "The originating site's origin is not configured as an absolute http(s) URL, so hook "
        "recursion chains are not sent to MCP servers. Set APP_BASE_URL (or MCP_BASE_URL), or "
        "call configure(base_url=...), when MCP tools call back into the site."
    )


def _patch_mcp_http_client() -> None:
    """Block redirects to other sites, so httpx can't carry X-MS-Hook-Invocation there."""
    from azure_functions_agents.discovery import mcp as runtime_mcp

    original = runtime_mcp._build_http_client

    @functools.wraps(original)
    def _build_http_client(header_provider: Any) -> Any:
        client = original(header_provider)
        if not isinstance(header_provider, _SameSiteHeaders):
            return client

        if client is None:
            from httpx import AsyncClient

            client = AsyncClient(follow_redirects=True)
        _keep_on_origin(client, header_provider.url)
        return client

    runtime_mcp._build_http_client = _build_http_client


def _keep_on_origin(client: Any, url: str) -> None:
    """Make the client refuse any request, redirects included, to a site other than ``url``'s."""
    from httpx import URL, RequestError

    # Compare URLs as httpx writes them, so the server's own URL always matches.
    def origin_of(target: Any) -> tuple[str, bytes, int]:
        default = 443 if target.scheme == "https" else 80
        return target.scheme, target.raw_host.lower(), target.port or default

    expected = origin_of(URL(url))

    async def stay_on_origin(request: Any) -> None:
        if origin_of(request.url) != expected:
            raise RequestError(
                "refused to follow a redirect from the originating site to "
                f"{request.url.scheme}://{request.url.host}",
                request=request,
            )

    hooks = client.event_hooks
    client.event_hooks = {**hooks, "request": [*hooks.get("request", []), stay_on_origin]}


def _patch_mcp_connect() -> None:
    """Let a server nobody can reach cost its own tools rather than the whole turn.

    An MCP session connects when the runner enters it, and a refusal there is raised through a
    ``TaskGroup`` -- so one unreachable server takes down every other tool with it, including the
    local ones that need no network, before the agent has run at all. That is the wrong trade: a
    server that cannot be reached has no tools, which is not the same as having no agent.

    Only reachability is softened. Nothing here touches what a tool is allowed to do, and a server
    that drops out can only ever narrow what an agent can reach -- never widen it.
    """
    from azure_functions_agents.discovery import mcp as runtime_mcp

    tool_class = getattr(runtime_mcp, "MCPStreamableHTTPTool", None)
    if not isinstance(tool_class, type):
        logger.warning(
            "The Hosted Skills MCP tool class was not recognised; one unreachable MCP server will fail the "
            "whole turn rather than only its own tools"
        )
        return

    # _build_mcp_tool reads this at build time, so every tool it builds is guarded.
    replace_runtime_reference(runtime_mcp, "MCPStreamableHTTPTool", _guarded_class(tool_class))


def _guarded_class(cls: type) -> type:
    """A subclass of ``cls`` whose session failing to open is a warning rather than an exception."""

    class Guarded(cls):  # type: ignore[misc,valid-type]
        _ase_unreachable = False

        async def __aenter__(self) -> Any:
            try:
                return await super().__aenter__()
            except Exception as exc:  # noqa: BLE001 - the point: one server is not the turn.
                self._ase_unreachable = True
                logger.warning(
                    "MCP server '%s' could not be reached, so this turn runs without its tools "
                    "(%s: %s). Every other tool is unaffected.",
                    getattr(self, "name", "?"),
                    type(exc).__name__,
                    exc,
                )
                return self

        async def __aexit__(self, *args: Any) -> Any:
            if self._ase_unreachable:
                # Entering already closed what it opened, so there is nothing to unwind.
                return None
            try:
                return await super().__aexit__(*args)
            except Exception:  # noqa: BLE001 - a failed teardown must not fail a served turn.
                logger.warning(
                    "MCP server '%s' did not close cleanly", getattr(self, "name", "?"), exc_info=True
                )
                return None

    Guarded.__name__ = f"Guarded{cls.__name__}"
    Guarded.__qualname__ = Guarded.__name__
    return Guarded


def _patch_mcp_sessions() -> None:
    """Give every request its own MCP tool objects rather than process-wide cached ones."""
    from azure_functions_agents.discovery import mcp as runtime_mcp

    original = runtime_mcp.discover_mcp_servers
    clear = runtime_mcp.clear_mcp_cache

    @functools.wraps(original)
    def discover_mcp_servers(*args: Any, **kwargs: Any) -> Any:
        scope = _request_scope.get()
        if scope is None:
            # Start-up, a warm-up probe, a test: nothing to scope this to, so leave the cache alone.
            return original(*args, **kwargs)

        clear()
        discovered = original(*args, **kwargs)
        scope.register(discovered.servers.values())
        return discovered

    replace_runtime_reference(
        runtime_mcp, "discover_mcp_servers", discover_mcp_servers, "azure_functions_agents.runner"
    )

    _patch_agent_runs(discover_mcp_servers)


def _patch_agent_runs(discover: Any) -> None:
    """Give each agent run fresh MCP tools, and record whether the run failed.

    Discovery alone is not enough: the runtime runs it once at start-up, carries the objects it built
    on ``capabilities.filtered_mcp_tools``, and hands those same ones to ``run_agent`` on every turn --
    so the discovery wrapper above never fires during a request. The run is where a turn can still
    be reached.

    It has to be the objects, not just the headers. A session connects on a long-lived task, and
    ``asyncio.create_task`` freezes that task's context at creation, so every call made through a
    shared session reads the hook state of whichever request opened it. Building inside the turn
    gives the session a task born in this request's context.
    """
    from azure_functions_agents import runner

    _watch_agent_runs(lambda tools: _rebuilt_for_this_turn(tools, discover, runner))
    logger.info("MCP sessions will be opened per request")


def _watch_agent_runs(rebuild: Any = None) -> None:
    from azure_functions_agents import runner

    for name in ("run_agent", "run_agent_stream"):
        original = getattr(runner, name)

        @functools.wraps(original)
        def scoped(*args: Any, _original: Any = original, **kwargs: Any) -> Any:
            # Not async, so the same wrapper works for run_agent and run_agent_stream.
            scope = _request_scope.get()
            if scope is None:
                return _original(*args, **kwargs)

            if rebuild is not None:
                rebuilt = rebuild(kwargs.get("mcp_tools"))
                if rebuilt is not None:
                    kwargs["mcp_tools"] = rebuilt
            return _watched(_original(*args, **kwargs), scope)

        setattr(runner, name, scoped)

def _rebuilt_for_this_turn(tools: Any, discover: Any, runner: Any) -> list[Any] | None:
    """Fresh copies of this run's MCP tools. Never falls back to the shared start-up ones."""
    if tools is None:
        # No tools given: the runtime finds them itself, through our per-request wrapper.
        return None

    wanted: list[str] = []
    for tool in tools:
        name = getattr(tool, "name", None)
        if not isinstance(name, str) or not name.strip():
            raise TypeError("MCP tools must have a nonblank string 'name' attribute")
        wanted.append(name)

    if not wanted:
        return []

    try:
        # The discovery wrapper clears the runtime's memo and registers what it builds on the
        # scope, so these are closed when the turn ends.
        servers = discover(runner.get_app_root()).servers
    except Exception:  # noqa: BLE001 - a discovery failure need not fail the whole turn.
        logger.warning(
            "Could not rebuild this turn's MCP sessions, so it runs without MCP tools",
            exc_info=True,
        )
        return []

    rebuilt = [servers[name] for name in wanted if name in servers]
    if len(rebuilt) < len(wanted):
        missing = ", ".join(sorted(str(name) for name in wanted if name not in servers))
        logger.warning(
            "MCP server(s) %s could not be rebuilt for this turn and were left out", missing
        )

    return rebuilt


def _watched(result: Any, scope: RequestScope) -> Any:
    """Wrap a run so the request is marked failed if the run raises or streams an error."""
    if inspect.isasyncgen(result):
        return _watched_stream(result, scope)
    if inspect.isawaitable(result):
        return _watched_run(result, scope)
    return result


async def _watched_run(run: Any, scope: RequestScope) -> Any:
    try:
        return await run
    except BaseException:
        scope.mark_failed()
        raise


async def _watched_stream(events: Any, scope: RequestScope) -> Any:
    finished = False
    try:
        async for event in events:
            if _is_error_event(event):
                scope.mark_failed()
            yield event
        finished = True
    finally:
        if not finished:
            # The stream raised, was cancelled, or was closed early: the run didn't finish.
            scope.mark_failed()
            await events.aclose()


def _is_error_event(event: Any) -> bool:
    """True if a streamed event is an error. The runtime streams errors instead of raising."""
    if not isinstance(event, str):
        return False

    for line in event.splitlines():
        if not line.startswith("data:"):
            continue
        try:
            payload = json.loads(line[len("data:"):])
        except ValueError:
            continue
        if isinstance(payload, dict) and payload.get("type") == "error":
            return True
    return False

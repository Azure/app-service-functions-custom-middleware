"""The Hosted Skills integration against the installed runtime, with only I/O replaced."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

httpx = pytest.importorskip("httpx")
runtime_mcp = pytest.importorskip("azure_functions_agents.discovery.mcp")
handlers = pytest.importorskip("azure_functions_agents.registration._handlers")

from azure_functions_agents.config import (  # noqa: E402
    BuiltinEndpointsConfig,
    ResolvedAgent,
    ToolsFilter,
)
from azure_functions_agents.registration.capabilities import AgentCapabilities  # noqa: E402
from starlette.requests import Request  # noqa: E402

from app_service_extensions import auth, hooks  # noqa: E402
from app_service_extensions.azure_functions_agents_integration import _mcp_request_scope, _prompt  # noqa: E402
from app_service_extensions.azure_functions_agents_integration._prompt import (  # noqa: E402
    _as_chat_prompt,
    _as_envelope,
    _serving,
)
from app_service_extensions.azure_functions_agents_integration._tools import patch_tool_discovery  # noqa: E402
from app_service_extensions.tools import ALL_TOOLS  # noqa: E402
from app_service_extensions.tools import request as request_tools  # noqa: E402

SITE = "https://app.example.invalid"

requires_legacy_mcp = pytest.mark.skipif(
    _mcp_request_scope._uses_mcp_descriptors(), reason="exercises the legacy MCP implementation"
)


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    current = hooks._current.set(None)
    chain = hooks._hook_invocation.set(None)
    scope = _mcp_request_scope._request_scope.set(None)
    monkeypatch.setattr(auth, "_base_url", SITE)
    # Save the originals so monkeypatch puts them back after the test.
    if not _mcp_request_scope._uses_mcp_descriptors():
        monkeypatch.setattr(runtime_mcp, "_build_header_provider", runtime_mcp._build_header_provider)
        monkeypatch.setattr(runtime_mcp, "_build_http_client", runtime_mcp._build_http_client)
    try:
        yield
    finally:
        hooks._current.reset(current)
        hooks._hook_invocation.reset(chain)
        _mcp_request_scope._request_scope.reset(scope)
        runtime_mcp.clear_mcp_cache()


@pytest.fixture
def network(monkeypatch):
    """The runtime's real HTTP client, answered from ``routes`` instead of the network."""
    sent: list[tuple[str, str | None]] = []
    routes: dict[str, httpx.Response] = {}

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), request.headers.get("x-ms-hook-invocation")))
        return routes.get(str(request.url), httpx.Response(200))

    real = httpx.AsyncClient

    class MockNetworkClient(real):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(answer)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", MockNetworkClient)
    return sent, routes


def same_site_client():
    _mcp_request_scope._patch_mcp_header_provider()
    _mcp_request_scope._patch_mcp_http_client()
    provider = runtime_mcp._build_header_provider({"url": f"{SITE}/.ai/mcp/default"})
    return runtime_mcp._build_http_client(provider)


@requires_legacy_mcp
class TestCallbackChainBoundary:
    @pytest.mark.asyncio
    async def test_a_redirect_to_another_origin_is_refused_before_anything_is_sent(self, network):
        # httpx copies our headers, X-MS-Hook-Invocation included, onto the redirected request.
        sent, routes = network
        routes[f"{SITE}/.ai/mcp/default"] = httpx.Response(
            302, headers={"Location": "https://third-party.invalid/collect"}
        )
        client = same_site_client()

        with hooks._use_hook_invocation("SYNTHETIC-CHAIN"):
            with pytest.raises(httpx.RequestError, match="refused to follow a redirect"):
                await client.get(f"{SITE}/.ai/mcp/default")

        assert sent == [(f"{SITE}/.ai/mcp/default", "SYNTHETIC-CHAIN")]

    @pytest.mark.asyncio
    async def test_a_redirect_within_the_site_is_still_followed(self, network):
        sent, routes = network
        routes[f"{SITE}/.ai/mcp/default"] = httpx.Response(
            307, headers={"Location": f"{SITE}/.ai/mcp/default/"}
        )
        client = same_site_client()

        with hooks._use_hook_invocation("SYNTHETIC-CHAIN"):
            response = await client.get(f"{SITE}/.ai/mcp/default")

        assert response.status_code == 200
        assert [chain for _, chain in sent] == ["SYNTHETIC-CHAIN", "SYNTHETIC-CHAIN"]

    def test_the_runtime_builds_same_site_tools_with_the_guarded_client(self, network):
        _mcp_request_scope._patch_mcp_header_provider()
        _mcp_request_scope._patch_mcp_http_client()
        tool, error = runtime_mcp._build_mcp_tool("site", {"url": f"{SITE}/.ai/mcp/default"})
        assert error is None
        assert any(
            hook.__name__ == "stay_on_origin" for hook in tool._httpx_client.event_hooks["request"]
        )


def resolved_agent(*, input_schema=None):
    return ResolvedAgent(
        name="review",
        description="Integration fixture",
        trigger=None,
        instructions="Integration fixture",
        is_main=False,
        builtin_endpoints=BuiltinEndpointsConfig(),
        model=None,
        timeout=1,
        enabled_mcp_names=[],
        enabled_skills_names=[],
        tool_filter=ToolsFilter(),
        sandbox_config=None,
        input_schema=input_schema,
        response_schema=None,
        response_example=None,
    )


def hook_request(mode="intercept"):
    headers = {"content-type": "application/json"}
    if mode is not None:
        headers.update({
            "x-ms-hook-name": "review",
            "x-ms-hook-mode": mode,
            "x-ms-hook-path": "/api/review",
        })

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "server": ("handler.example.invalid", 443),
            "path": "/hook",
            "query_string": b"",
            "headers": [(key.encode(), value.encode()) for key, value in headers.items()],
        },
        receive,
    )


class TestHttpAgentPrompts:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("intercept", "app is waiting"), ("mirror", "continues independently")],
    )
    async def test_guidance_reaches_the_model_without_changing_the_validated_envelope(
        self, monkeypatch, mode, expected
    ):
        triggers = pytest.importorskip("azure_functions_agents.registration.triggers")
        captured = []

        async def run(prompt, **kwargs):
            captured.append(prompt)
            if mode == "intercept":
                request_tools.passthrough()
            return SimpleNamespace(content="observed", model="test", tool_calls=[], session_id="test")

        monkeypatch.setattr(handlers, "_run_agent", run)
        monkeypatch.setattr(handlers, "make_http_agent_handler", handlers.make_http_agent_handler)
        monkeypatch.setattr(triggers, "make_http_agent_handler", triggers.make_http_agent_handler)
        _prompt._patch_agent_trigger()

        fields = ("hook", "mode", "method", "path", "route", "origin", "query",
                  "contentType", "headers", "body")
        schema = {
            "type": "object",
            "properties": {name: {} for name in fields},
            "required": list(fields),
            "additionalProperties": False,
        }
        handler = handlers.make_http_agent_handler(
            resolved_agent(input_schema=schema), AgentCapabilities()
        )
        response = await handler(hook_request(mode))

        assert response.status_code == 200
        if mode == "intercept":
            assert json.loads(response.body) == {"action": "passthrough"}
        else:
            assert response.body == b"observed"
        assert len(captured) == 1
        assert expected in captured[0]
        request_json = captured[0].split("HTTP request data:\n```json\n", 1)[1].split("\n```", 1)[0]
        envelope = json.loads(request_json)
        assert set(envelope) == set(fields)
        assert envelope["mode"] == mode

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [None, "proxy"], ids=["direct", "proxy"])
    async def test_direct_and_proxy_calls_do_not_receive_hook_guidance(self, monkeypatch, mode):
        triggers = pytest.importorskip("azure_functions_agents.registration.triggers")
        captured = []

        async def run(prompt, **kwargs):
            captured.append(prompt)
            return SimpleNamespace(content="observed", model="test", tool_calls=[], session_id="test")

        monkeypatch.setattr(handlers, "_run_agent", run)
        monkeypatch.setattr(handlers, "make_http_agent_handler", handlers.make_http_agent_handler)
        monkeypatch.setattr(triggers, "make_http_agent_handler", triggers.make_http_agent_handler)
        _prompt._patch_agent_trigger()
        handler = handlers.make_http_agent_handler(resolved_agent(), AgentCapabilities())

        response = await handler(hook_request(mode))
        assert response.status_code == 200
        assert "app is waiting" not in captured[0]
        assert "continues independently" not in captured[0]
        assert '"mode"' not in captured[0]

    @pytest.mark.asyncio
    async def test_concurrent_hook_modes_do_not_share_guidance(self, monkeypatch):
        triggers = pytest.importorskip("azure_functions_agents.registration.triggers")
        captured = {}
        both_started = asyncio.Event()

        async def run(prompt, **kwargs):
            envelope = json.loads(
                prompt.split("HTTP request data:\n```json\n", 1)[1].split("\n```", 1)[0]
            )
            captured[envelope["mode"]] = prompt
            if envelope["mode"] == "intercept":
                request_tools.passthrough()
            if len(captured) == 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=3)
            return SimpleNamespace(content="observed", model="test", tool_calls=[], session_id="test")

        monkeypatch.setattr(handlers, "_run_agent", run)
        monkeypatch.setattr(handlers, "make_http_agent_handler", handlers.make_http_agent_handler)
        monkeypatch.setattr(triggers, "make_http_agent_handler", triggers.make_http_agent_handler)
        _prompt._patch_agent_trigger()
        handler = handlers.make_http_agent_handler(resolved_agent(), AgentCapabilities())

        responses = await asyncio.gather(handler(hook_request("intercept")), handler(hook_request("mirror")))

        assert [response.status_code for response in responses] == [200, 200]
        assert "app is waiting" in captured["intercept"]
        assert "continues independently" not in captured["intercept"]
        assert "continues independently" in captured["mirror"]
        assert "app is waiting" not in captured["mirror"]

    @pytest.mark.asyncio
    async def test_validation_failure_does_not_leak_hook_guidance(self, monkeypatch):
        triggers = pytest.importorskip("azure_functions_agents.registration.triggers")
        captured = []

        async def run(prompt, **kwargs):
            captured.append(prompt)
            return SimpleNamespace(content="observed", model="test", tool_calls=[], session_id="test")

        monkeypatch.setattr(handlers, "_run_agent", run)
        monkeypatch.setattr(handlers, "make_http_agent_handler", handlers.make_http_agent_handler)
        monkeypatch.setattr(triggers, "make_http_agent_handler", triggers.make_http_agent_handler)
        _prompt._patch_agent_trigger()
        schema = {"type": "object", "properties": {}, "additionalProperties": False}
        handler = handlers.make_http_agent_handler(
            resolved_agent(input_schema=schema), AgentCapabilities()
        )

        response = await handler(hook_request("intercept"))
        await handlers._run_agent("unrelated run")

        assert response.status_code == 400
        assert captured == ["unrelated run"]

    @pytest.mark.asyncio
    async def test_guidance_is_not_reapplied_to_a_nested_agent_run(self, monkeypatch):
        triggers = pytest.importorskip("azure_functions_agents.registration.triggers")
        captured = []

        async def run(prompt, **kwargs):
            captured.append(prompt)
            if prompt != "nested run":
                await handlers._run_agent("nested run")
                request_tools.passthrough()
            return SimpleNamespace(content="observed", model="test", tool_calls=[], session_id="test")

        monkeypatch.setattr(handlers, "_run_agent", run)
        monkeypatch.setattr(handlers, "make_http_agent_handler", handlers.make_http_agent_handler)
        monkeypatch.setattr(triggers, "make_http_agent_handler", triggers.make_http_agent_handler)
        _prompt._patch_agent_trigger()
        handler = handlers.make_http_agent_handler(resolved_agent(), AgentCapabilities())

        response = await handler(hook_request("intercept"))

        assert response.status_code == 200
        assert "app is waiting" in captured[0]
        assert captured[1] == "nested run"


class TestFailedTurns:
    @pytest.mark.asyncio
    async def test_the_runtimes_failure_is_returned_rather_than_a_partial_patch(self, monkeypatch):
        async def fail_after_editing(*args, **kwargs):
            request_tools.set_header("X-Reviewed", "partial")
            raise RuntimeError("synthetic model failure")

        monkeypatch.setattr(handlers, "_run_agent", fail_after_editing)
        handler = handlers.make_http_agent_handler(resolved_agent(), AgentCapabilities())

        response = await _serving(handler, _as_envelope)(hook_request())

        assert response.status_code == 500
        assert json.loads(response.body) == {"error": "synthetic model failure"}

    @pytest.mark.asyncio
    @pytest.mark.parametrize("fails", [True, False])
    async def test_the_chat_stream_commits_only_a_completed_turn(self, monkeypatch, fails):
        # The stream sends 200 before the agent runs, so only the run itself can report failure.
        runner = pytest.importorskip("azure_functions_agents.runner")
        endpoints = pytest.importorskip("azure_functions_agents.registration.endpoints")
        functions = pytest.importorskip("azure.functions")

        async def edit_then_finish(*args, **kwargs):
            request_tools.set_header("X-Reviewed", "yes")
            event = {"type": "error", "content": "Timeout after 30s"} if fails else {"type": "done"}
            yield f"data: {json.dumps(event)}\n\n"

        # Save the originals so monkeypatch puts them back after the test.
        monkeypatch.setattr(runner, "run_agent", runner.run_agent)
        monkeypatch.setattr(runner, "run_agent_stream", edit_then_finish)
        if _mcp_request_scope._uses_mcp_descriptors():
            _mcp_request_scope._watch_agent_runs()
        else:
            monkeypatch.setattr(runtime_mcp, "discover_mcp_servers", runtime_mcp.discover_mcp_servers)
            monkeypatch.setattr(runner, "discover_mcp_servers", runner.discover_mcp_servers)
            _mcp_request_scope._patch_mcp_sessions()

        from azure_functions_agents import config

        auth_options = {}
        if hasattr(config, "EndpointAuthConfig"):
            auth_options["auth"] = config.EndpointAuthConfig()

        app = functions.FunctionApp()
        endpoints._register_http_chat_stream(
            app,
            resolved_agent(),
            AgentCapabilities(),
            route="agents/review/chat/stream",
            function_name="review_chat_stream",
            **auth_options,
        )
        [function] = app.get_functions()
        handler = _serving(function.get_user_function(), _as_chat_prompt)

        response = await handler(hook_request())

        if fails:
            assert response.status_code == 500
            assert b"edits" not in response.body
        else:
            assert response.status_code == 200
            assert json.loads(response.body)["edits"][0]["name"] == "X-Reviewed"


@requires_legacy_mcp
class TestSessionRebuild:
    def test_a_configuration_that_no_longer_loads_yields_no_tools(self, tmp_path):
        config = tmp_path / "mcp.json"
        config.write_text(json.dumps({"servers": {"site": {"url": f"{SITE}/mcp"}}}))
        runtime_mcp.clear_mcp_cache()
        startup = list(runtime_mcp.discover_mcp_servers(tmp_path).servers.values())
        assert len(startup) == 1

        config.write_text("{")
        runtime_mcp.clear_mcp_cache()

        class Runner:
            @staticmethod
            def get_app_root():
                return tmp_path

        assert _mcp_request_scope._rebuilt_for_this_turn(startup, runtime_mcp.discover_mcp_servers, Runner) == []


@requires_legacy_mcp
class TestConnectionGuard:
    def test_the_runtime_builds_guarded_tools(self, monkeypatch):
        from agent_framework import MCPStreamableHTTPTool

        # Save the original so monkeypatch puts it back after the test.
        monkeypatch.setattr(runtime_mcp, "MCPStreamableHTTPTool", runtime_mcp.MCPStreamableHTTPTool)
        _mcp_request_scope._patch_mcp_connect()

        tool, error = runtime_mcp._build_mcp_tool("site", {"url": f"{SITE}/.ai/mcp/default"})
        assert error is None
        assert isinstance(tool, MCPStreamableHTTPTool)
        assert type(tool).__name__ == "GuardedMCPStreamableHTTPTool"


class TestToolDiscovery:
    def test_every_discovery_path_carries_the_extension_tools_once(self, monkeypatch, tmp_path):
        tools = pytest.importorskip("azure_functions_agents.discovery.tools")
        runner = pytest.importorskip("azure_functions_agents.runner")
        app = pytest.importorskip("azure_functions_agents.app")
        # Save the originals so monkeypatch puts them back; discovery also extends sys.path.
        monkeypatch.setattr(tools, "discover_project_tools", tools.discover_project_tools)
        monkeypatch.setattr(app, "discover_project_tools", app.discover_project_tools)
        monkeypatch.setattr(sys, "path", list(sys.path))

        patch_tool_discovery()
        try:
            expected = sorted(tool.__name__ for tool in ALL_TOOLS)
            run_tools = runner.discover_user_tools(tmp_path).tools
            startup_tools = app.discover_project_tools(tmp_path).user_tools
            assert sorted(tool.name for tool in run_tools) == expected
            assert sorted(tool.name for tool in startup_tools) == expected
        finally:
            tools.clear_tool_discovery_cache()

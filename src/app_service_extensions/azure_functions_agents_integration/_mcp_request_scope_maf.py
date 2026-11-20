"""Request-scoped MCP tools for the descriptor-based Hosted Skills runtime."""

from __future__ import annotations

import functools
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from ._mcp_request_scope import (
    _SameSiteHeaders,
    _guarded_class,
    _is_same_site_url,
    _keep_on_origin,
    _request_scope,
    _watch_agent_runs,
    logger,
)


def patch_descriptor_runtime() -> None:
    from agent_framework._mcp import MCP_DEFAULT_SSE_READ_TIMEOUT, MCP_DEFAULT_TIMEOUT
    from azure_functions_agents import runner
    from azure_functions_agents.discovery.mcp import MCPServerDescriptor
    from azure_functions_agents.harness.agent_framework import _maf_mcp
    from httpx import AsyncClient, Timeout

    build = _maf_mcp.build_maf_mcp_tools
    get_runner = runner.get_agent_runner
    # Share runtime identity providers, never the tools or sessions that use them.
    get_provider = functools.lru_cache(maxsize=128)(_maf_mcp.build_mcp_header_provider)

    class SameSiteTool(_maf_mcp._OwnedHTTPClientMCPStreamableHTTPTool):
        @asynccontextmanager
        async def get_mcp_client(self) -> AsyncIterator[Any]:
            async with AsyncClient(
                follow_redirects=True,
                timeout=Timeout(MCP_DEFAULT_TIMEOUT, read=MCP_DEFAULT_SSE_READ_TIMEOUT),
            ) as client:
                _keep_on_origin(client, self.url)
                self._httpx_client = client
                try:
                    async with super().get_mcp_client() as transport:
                        yield transport
                finally:
                    self._httpx_client = None
                    if hasattr(self, "_inject_headers_hook"):
                        del self._inject_headers_hook

    guarded_same_site = _guarded_class(SameSiteTool)
    guarded_other_site = _guarded_class(_maf_mcp._OwnedHTTPClientMCPStreamableHTTPTool)

    @functools.wraps(build)
    def scoped_tools(servers: Sequence[MCPServerDescriptor]) -> list[Any]:
        if _request_scope.get() is None:
            return build(servers)

        tools = []
        for server in servers:
            provider = get_provider(server)
            tool_class = guarded_other_site
            if _is_same_site_url(server.url):
                provider = _SameSiteHeaders(provider, server.url)
                tool_class = guarded_same_site
            tools.append(
                tool_class(
                    name=server.name,
                    url=server.url,
                    allowed_tools=list(server.tools) if server.tools is not None else None,
                    load_tools=True,
                    load_prompts=False,
                    approval_mode=_maf_mcp.ApprovalMode.NEVER_REQUIRE.value,
                    header_provider=provider,
                )
            )
        return tools

    @functools.wraps(get_runner)
    def scoped_runner(harness: Any) -> Any:
        if _request_scope.get() is not None and harness.name != "maf":
            raise RuntimeError(
                "Hosted Skills hook callbacks on the descriptor-based runtime require "
                "the Agent Framework backend (maf)."
            )
        return get_runner(harness)

    _maf_mcp.build_maf_mcp_tools = scoped_tools
    runner.get_agent_runner = scoped_runner
    _watch_agent_runs()
    logger.info("Agent Framework MCP sessions will be opened per request")

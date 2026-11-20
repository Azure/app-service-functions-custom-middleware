"""What the Hosted Skills runtime discovers, including this library's tools."""

from __future__ import annotations

import functools
from dataclasses import replace
from typing import Any

from ..tools import ALL_TOOLS, APP_TOOLS
from . import logger, replace_runtime_reference
from ._mcp_request_scope import _missing_functions

#: Never handed to an agent, whatever ALL_TOOLS happens to contain.
_WITHHELD_FROM_RUNTIME = frozenset(fn.__name__ for fn in APP_TOOLS)


def _extension_tools() -> list[Any]:
    """Wrap our tools as the ``FunctionTool`` objects the runtime's runner expects."""
    from azure_functions_agents import tool

    # The name and description are read off the function itself, so an agent sees exactly what the
    # tool declares.
    return [
        tool(fn, name=fn.__name__, description=(fn.__doc__ or "").strip())
        for fn in ALL_TOOLS
        if fn.__name__ not in _WITHHELD_FROM_RUNTIME
    ]


def validate_tool_discovery() -> None:
    """Check tool prerequisites without replacing runtime functions."""
    missing = _missing_functions({
        "azure_functions_agents.discovery.tools": ("discover_project_tools",),
        "azure_functions_agents": ("tool",),
    })
    if missing:
        raise RuntimeError(
            "app-service-functions-custom-middleware could not find the runtime functions "
            f"needed to register extension tools ({', '.join(missing)}). "
            "Upgrade app-service-functions-custom-middleware, or pass "
            "enable(tools=False) to skip tool integration."
        )


def patch_tool_discovery() -> None:
    """Append our tools to those found in the app's own ``tools/`` folder."""
    validate_tool_discovery()

    from azure_functions_agents.discovery import tools as runtime_tools

    original = runtime_tools.discover_project_tools

    # discover_user_tools calls this too, so one patch covers every discovery path.
    replace_runtime_reference(
        runtime_tools,
        "discover_project_tools",
        _with_our_tools(original),
        "azure_functions_agents.app",
    )


def _with_our_tools(original: Any) -> Any:
    """Wrap ``discover_project_tools`` so the user tools it returns include this library's."""

    @functools.wraps(original)
    def discover_project_tools(*args: Any, **kwargs: Any) -> Any:
        found = original(*args, **kwargs)

        # The app's own tools win on a name clash: this library is a default, not an override.
        taken = {tool.name for tool in found.user_tools}
        added = [tool for tool in _extension_tools() if tool.name not in taken]
        if added:
            logger.info(
                "Added %d app-service-functions-custom-middleware tool(s) to Hosted Skills",
                len(added),
            )

        return replace(found, user_tools=[*found.user_tools, *added])

    return discover_project_tools

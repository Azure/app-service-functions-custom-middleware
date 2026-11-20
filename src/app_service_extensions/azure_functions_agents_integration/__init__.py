"""Hosted Skills integration: tools, hook prompts, and per-request MCP sessions."""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType
from typing import Any

from ..hooks import logger

#: Set once the runtime has been patched, preventing a second configure() from double-wrapping it.
_enabled = False


def is_available() -> bool:
    """True when the Hosted Skills runtime package is installed alongside this library."""
    return importlib.util.find_spec("azure_functions_agents") is not None


def enable(*, tools: bool = True, prompt: bool = True, callbacks: bool = True) -> bool:
    """Validate and install enabled integrations; return whether anything was done."""
    global _enabled
    if _enabled or not is_available():
        return False

    from ._mcp_request_scope import patch_hook_forwarding, validate_hook_forwarding
    from ._prompt import patch_prompt, validate_prompt
    from ._tools import patch_tool_discovery, validate_tool_discovery

    if tools:
        validate_tool_discovery()
    if prompt:
        validate_prompt()
    if callbacks:
        validate_hook_forwarding()

    if callbacks:
        patch_hook_forwarding()
    if tools:
        patch_tool_discovery()
    if prompt:
        patch_prompt()

    _enabled = True
    return True


def replace_runtime_reference(
    module: ModuleType, name: str, replacement: Any, *importers: str
) -> None:
    """Replace ``module.name``, also in loaded ``importers`` that imported it by name."""
    original = getattr(module, name)
    setattr(module, name, replacement)
    for importer_name in importers:
        importer = sys.modules.get(importer_name)
        if importer is not None and getattr(importer, name, None) is original:
            setattr(importer, name, replacement)

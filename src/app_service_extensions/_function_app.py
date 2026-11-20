"""Convenience factory for Hosted Skills Azure Function apps."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .auth import configure


def create_function_app(app_root: Path | None = None) -> Any:
    """Create a Hosted Skills Function app with this extension configured.

    This is equivalent to::

        configure()
        app = azure_functions_agents.create_function_app(app_root)
        configure(app)

    Outbound authentication uses the application's configured credentials.
    """
    try:
        from azure_functions_agents import create_function_app as create_hosted_skills_app
    except ModuleNotFoundError as exc:
        if exc.name != "azure_functions_agents":
            raise
        raise ModuleNotFoundError(
            "create_function_app() requires the hosted-skills extra; install "
            "'app-service-functions-custom-middleware[hosted-skills]'"
        ) from exc

    configure()
    app = create_hosted_skills_app(app_root)
    configure(app)
    return app

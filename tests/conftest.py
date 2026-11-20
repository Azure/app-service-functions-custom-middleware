"""Isolation shared by every test."""

from __future__ import annotations

import pytest

from app_service_extensions import azure_functions_agents_integration


@pytest.fixture(autouse=True)
def runtime_left_unpatched(monkeypatch):
    """Keep configure() from patching the installed runtime; integration tests opt back in."""
    monkeypatch.setattr(azure_functions_agents_integration, "_enabled", True)

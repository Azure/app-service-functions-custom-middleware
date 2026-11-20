"""Tests for the Hosted Skills Function app convenience factory."""

from __future__ import annotations

import builtins
import sys
import types
from pathlib import Path

import pytest

import app_service_extensions as ase
from app_service_extensions import _function_app


def test_factory_runs_the_existing_initialization_sequence(monkeypatch, tmp_path):
    app = object()
    calls = []
    runtime = types.ModuleType("azure_functions_agents")

    def create_runtime_app(app_root):
        calls.append(("create", app_root))
        return app

    runtime.create_function_app = create_runtime_app
    monkeypatch.setitem(sys.modules, "azure_functions_agents", runtime)
    monkeypatch.setattr(
        _function_app,
        "configure",
        lambda configured_app=None, **kwargs: calls.append(("configure", configured_app, kwargs)),
    )

    assert ase.create_function_app(tmp_path) is app
    assert calls == [
        ("configure", None, {}),
        ("create", tmp_path),
        ("configure", app, {}),
    ]


def test_factory_passes_the_default_app_root(monkeypatch):
    runtime = types.ModuleType("azure_functions_agents")
    seen = []
    app = object()

    def create_runtime_app(app_root):
        seen.append(app_root)
        return app

    runtime.create_function_app = create_runtime_app
    monkeypatch.setitem(sys.modules, "azure_functions_agents", runtime)
    configured = []
    monkeypatch.setattr(
        _function_app, "configure", lambda app=None, **kwargs: configured.append(kwargs)
    )

    assert ase.create_function_app() is app
    assert seen == [None]
    assert configured == [{}, {}]


def test_factory_explains_how_to_install_the_runtime(monkeypatch):
    real_import = builtins.__import__

    def missing_runtime(name, *args, **kwargs):
        if name == "azure_functions_agents":
            error = ModuleNotFoundError("No module named 'azure_functions_agents'")
            error.name = "azure_functions_agents"
            raise error
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing_runtime)

    with pytest.raises(ModuleNotFoundError, match=r"\[hosted-skills\]"):
        ase.create_function_app(Path.cwd())

"""Tests for application credentials and base URL configuration."""

from __future__ import annotations

import sys
import types

import pytest

from app_service_extensions import auth


@pytest.fixture(autouse=True)
def isolate(monkeypatch):
    monkeypatch.setattr(auth, "_base_url", None)
    monkeypatch.setattr(auth, "_presenter", None)
    monkeypatch.setattr(auth, "_credential", None)
    monkeypatch.delenv("MCP_AUTH_SCOPE", raising=False)


class TestPresenter:
    def test_uses_the_configured_application_credential(self):
        auth.configure(presenter=lambda: "application-token")
        assert auth.presenter_token() == "application-token"

    def test_missing_credentials_are_a_configuration_error(self):
        with pytest.raises(auth.AuthError, match="no presenter credential"):
            auth.presenter_token()

    def test_an_empty_provider_is_a_configuration_error(self):
        auth.configure(presenter=lambda: None)
        with pytest.raises(auth.AuthError, match="no presenter credential"):
            auth.presenter_token()

    def test_a_failed_provider_preserves_the_cause(self):
        failure = RuntimeError("credential unavailable")

        def broken():
            raise failure

        auth.configure(presenter=broken)
        with pytest.raises(auth.AuthError, match="credential unavailable") as caught:
            auth.presenter_token()
        assert caught.value.__cause__ is failure

    @pytest.mark.parametrize("client_id", [None, "user-assigned-id"])
    def test_managed_identity_uses_the_configured_scope_and_caches_its_credential(
        self, monkeypatch, client_id
    ):
        built = []
        scopes = []

        class Credential:
            def __init__(self, **kwargs):
                built.append(kwargs)

            def get_token(self, scope):
                scopes.append(scope)
                return types.SimpleNamespace(token="application-token")

        identity = types.ModuleType("azure.identity")
        identity.ManagedIdentityCredential = Credential
        monkeypatch.setitem(sys.modules, "azure.identity", identity)
        monkeypatch.setenv("MCP_AUTH_SCOPE", "api://application/.default")
        if client_id:
            monkeypatch.setenv("MCP_AUTH_CLIENT_ID", client_id)
        else:
            monkeypatch.delenv("MCP_AUTH_CLIENT_ID", raising=False)

        assert auth.presenter_token() == "application-token"
        assert auth.presenter_token() == "application-token"
        assert built == ([{"client_id": client_id}] if client_id else [{}])
        assert scopes == ["api://application/.default"] * 2


class TestBaseUrl:
    def test_explicit_configuration_wins(self, monkeypatch):
        monkeypatch.setenv("APP_BASE_URL", "https://environment.example")
        auth.configure(base_url="https://configured.example/")
        assert auth.app_base_url() == "https://configured.example"

    def test_falls_back_to_the_environment(self, monkeypatch):
        monkeypatch.setenv("APP_BASE_URL", "https://from-env.example/")
        assert auth.app_base_url() == "https://from-env.example"

    def test_derives_from_the_mcp_endpoint(self, monkeypatch):
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        monkeypatch.setenv("MCP_BASE_URL", "https://derived.example/.ai/mcp/default")
        assert auth.app_base_url() == "https://derived.example"

    def test_missing_configuration_raises(self, monkeypatch):
        monkeypatch.delenv("APP_BASE_URL", raising=False)
        monkeypatch.delenv("MCP_BASE_URL", raising=False)
        with pytest.raises(auth.AuthError, match="base URL not configured"):
            auth.app_base_url()

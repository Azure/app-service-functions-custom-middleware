"""Tests for ingesting a hook forward and rendering it as a prompt."""

from __future__ import annotations

import json

import pytest

from app_service_extensions import HookRequest


def build(**overrides):
    """A representative intercept forward, as EasyAuth would send it."""
    headers = {
        "X-MS-Hook-Name": "expense-patcher-intercept",
        "X-MS-Hook-Mode": "intercept",
        "X-MS-Hook-Path": "/api/expenses",
        "X-MS-Hook-Route": "/api/expenses",
        "X-MS-Hook-Origin": "https://contoso.example",
        "X-MS-Hook-Invocation": "signed-hook-chain",
        "X-Service-Token": "service-secret",
        "x-functions-key": "function-key-value",
        "User-Agent": "Mozilla/5.0",
    }
    headers.update(overrides.pop("headers", {}))
    return HookRequest.from_http(
        method=overrides.pop("method", "POST"),
        headers=headers,
        body=overrides.pop("body", '{"amount":"16 dollars"}'),
        query=overrides.pop("query", ""),
        content_type=overrides.pop("content_type", "application/json"),
    )


class TestTheClientsMethod:
    """A mirror or intercept forward is always POSTed, so the transport's method says nothing.

    The handler is being asked *about* a request, not serving it, and a handler cannot register a
    route for every method a client might use. The client's real verb travels as metadata instead.
    """

    def test_the_header_is_what_the_client_did(self):
        request = build(
            method="POST",
            headers={"X-MS-Hook-Request-Method": "GET"},
        )
        assert request.method == "GET"

    def test_it_survives_a_forward_that_was_posted(self):
        # The whole point: without this, every intercepted GET would read as a POST.
        request = build(method="POST", headers={"X-MS-Hook-Request-Method": "DELETE"})
        assert request.method == "DELETE"

    def test_the_transport_method_is_used_when_there_is_no_header(self):
        # A proxy forward replays the client's method, and an older middleware sends no header.
        request = build(method="PUT")
        assert request.method == "PUT"

    def test_it_is_normalised(self):
        request = build(headers={"X-MS-Hook-Request-Method": "patch"})
        assert request.method == "PATCH"

    def test_the_envelope_shows_the_client_s_method(self):
        # This is what an agent reasons about, so it has to be the caller's verb.
        request = build(method="POST", headers={"X-MS-Hook-Request-Method": "GET"})
        assert request.envelope()["method"] == "GET"


class TestIngest:
    def test_reads_injected_hook_headers(self):
        request = build()
        assert request.hook == "expense-patcher-intercept"
        assert request.mode == "intercept"
        assert request.path == "/api/expenses"
        assert request.origin == "https://contoso.example"
        assert request.method == "POST"
        assert request.is_hook
        assert request.headers == {"User-Agent": "Mozilla/5.0"}

    def test_reads_the_recursion_chain(self):
        assert build().invocation == "signed-hook-chain"

    def test_header_lookup_is_case_insensitive(self):
        # ASGI lower-cases header names; the middleware sends them mixed-case.
        request = HookRequest.from_http(
            headers={"x-ms-hook-name": "h", "x-ms-hook-invocation": "chain"}
        )
        assert request.hook == "h"
        assert request.invocation == "chain"

    def test_absent_injected_headers_are_tolerated(self):
        # Injected values over 1024 chars or non-printable are dropped silently by the middleware.
        request = HookRequest.from_http(method="GET", headers={})
        assert request.hook is None
        assert request.route is None
        assert not request.is_hook

    def test_bytes_body_is_decoded(self):
        assert build(body=b'{"a":1}').body == '{"a":1}'

    def test_invalid_utf8_body_does_not_raise(self):
        assert build(body=b"\xff\xfe").body is not None

    @pytest.mark.parametrize(
        "header",
        [
            "X-Service-Token",
            "x-functions-key",
            "X-MS-Hook-Invocation",
            "Authorization",
            "X-API-KEY",
            "X-MS-Example",
            "x_ms_example",
            "mWh-Example",
            " MWH_Example ",
        ],
    )
    def test_private_headers_are_not_retained_in_public_headers(self, header):
        retained = {name.lower() for name in build(headers={header: "secret-value"}).headers}
        assert header.lower() not in retained

    def test_leading_question_mark_is_stripped_from_query(self):
        assert build(query="?q=Karen").query_params == {"q": "Karen"}

    def test_body_is_kept_as_sent(self):
        assert build().body == '{"amount":"16 dollars"}'


class TestPrompt:
    def envelope_of(self, request):
        prompt = request.to_prompt()
        return json.loads(prompt.split("```json", 1)[1].rsplit("```", 1)[0])

    def test_carries_what_the_body_alone_would_lose(self):
        # Hosted Skills runtime's own handler prompts with the body and nothing else; a GET would render as {}.
        request = build(
            method="GET",
            body=None,
            query="q=Karen",
            headers={
                "X-MS-Hook-Route": "/api/expenses/{id}",
                "X-MS-Hook-Path": "/api/expenses/42",
            },
        )
        envelope = self.envelope_of(request)
        assert envelope["method"] == "GET"
        assert envelope["path"] == "/api/expenses/42"
        assert envelope["route"] == "/api/expenses/{id}"
        assert envelope["origin"] == "https://contoso.example"
        assert envelope["query"] == "q=Karen"

    def test_body_stays_a_string(self):
        # A replacement is written against what the client actually sent.
        envelope = self.envelope_of(build())
        assert envelope["body"] == '{"amount":"16 dollars"}'
        assert isinstance(envelope["body"], str)

    @pytest.mark.parametrize(
        "credential",
        ["service-secret", "function-key-value", "signed-hook-chain"],
    )
    def test_credentials_never_reach_the_prompt(self, credential):
        assert credential not in build().to_prompt()

    @pytest.mark.parametrize(
        "header",
        [
            "Authorization",
            "Cookie",
            "Proxy-Authorization",
            "X-MS-Example",
            "x_ms_example",
            "mwh-example",
            " MWH_Example ",
            "X-Custom-Token",
        ],
    )
    @pytest.mark.parametrize("from_http", [True, False], ids=["http", "direct"])
    def test_private_headers_never_reach_a_prompt(self, header, from_http):
        headers = {header: "secret-value"}
        request = build(headers=headers) if from_http else HookRequest(headers=headers)
        assert "secret-value" not in request.to_prompt()
        assert header not in request.envelope()["headers"]

    @pytest.mark.parametrize("header", ["User-Agent", "X-MSish-Example", "MWHish-Example"])
    def test_non_credential_headers_are_kept(self, header):
        request = build(headers={header: "retained-value"})
        assert self.envelope_of(request)["headers"][header] == "retained-value"

    def test_prompt_embeds_parseable_json(self):
        assert self.envelope_of(build())["hook"] == "expense-patcher-intercept"

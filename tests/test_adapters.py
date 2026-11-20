"""Tests for the framework adapters.

Exercised against the real Starlette request where possible, and against a stand-in for Azure
Functions, whose SDK is not a dependency of this library.
"""

from __future__ import annotations

from app_service_extensions import from_asgi, from_functions

FORWARDED = {
    "X-MS-Hook-Name": "expense-normalizer",
    "X-MS-Hook-Mode": "intercept",
    "X-MS-Hook-Path": "/api/expenses",
    "X-MS-Hook-Route": "/api/expenses/{id}",
    "X-MS-Hook-Origin": "https://contoso.example",
    "X-MS-Hook-Invocation": "signed-chain",
    "Content-Type": "application/json",
}

BODY = b'{"amount":"16 dollars"}'


class FakeFunctionsRequest:
    """The shape of ``azure.functions.HttpRequest`` that :func:`from_functions` relies on."""

    def __init__(self, method="POST", url="", headers=None, body=b""):
        self.method = method
        self.url = url
        self.headers = headers or {}
        self._body = body

    def get_body(self) -> bytes:
        return self._body


def starlette_request(query="", body=BODY, headers=None):
    from starlette.requests import Request

    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or FORWARDED).items()]
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/hooks/expenses",
        "query_string": query.encode(),
        "headers": raw,
    }
    return Request(scope), body


class TestFromAsgi:
    def test_reads_the_injected_headers(self):
        request, body = starlette_request()
        hook = from_asgi(request, body)
        assert hook.hook == "expense-normalizer"
        assert hook.mode == "intercept"
        assert hook.path == "/api/expenses"
        assert hook.route == "/api/expenses/{id}"
        assert hook.origin == "https://contoso.example"
        assert hook.is_hook

    def test_reads_the_callback_chain(self):
        request, body = starlette_request()
        assert from_asgi(request, body).invocation == "signed-chain"

    def test_reads_method_body_and_content_type(self):
        request, body = starlette_request()
        hook = from_asgi(request, body)
        assert hook.method == "POST"
        assert hook.body == BODY.decode()
        assert hook.content_type == "application/json"

    def test_carries_the_query_string(self):
        # Omitting this is the quiet failure the adapter exists to prevent.
        request, body = starlette_request(query="tag=a&debug=1")
        assert from_asgi(request, body).query == "tag=a&debug=1"

    def test_body_is_optional(self):
        request, _ = starlette_request()
        assert from_asgi(request).body is None

    def test_feeds_the_editable_request(self):
        request, body = starlette_request(query="debug=1")
        answer = from_asgi(request, body).edit().remove_query("debug").to_dict("tidied")
        assert answer["action"] == "modify"
        assert answer["edits"] == [{"op": "remove", "target": "query", "name": "debug"}]


class TestFromFunctions:
    def build(self, url="https://f.azurewebsites.net/api/hook", body=BODY):
        return FakeFunctionsRequest(url=url, headers=dict(FORWARDED), body=body)

    def test_reads_the_injected_headers(self):
        hook = from_functions(self.build())
        assert hook.hook == "expense-normalizer"
        assert hook.mode == "intercept"
        assert hook.invocation == "signed-chain"

    def test_reads_the_body_itself(self):
        assert from_functions(self.build()).body == BODY.decode()

    def test_recovers_the_query_from_the_url(self):
        # The middleware appends the client's query to the handler's own URL.
        hook = from_functions(self.build(url="https://f.azurewebsites.net/api/hook?tag=a&debug=1"))
        assert hook.query == "tag=a&debug=1"

    def test_tolerates_a_missing_url(self):
        request = FakeFunctionsRequest(headers=dict(FORWARDED), body=BODY)
        assert from_functions(request).query == ""

    def test_feeds_the_editable_request(self):
        answer = from_functions(self.build()).edit().set_header("X-A", "1").to_dict("tagged")
        assert answer["edits"] == [
            {"op": "set", "target": "header", "name": "X-A", "value": "1"}
        ]

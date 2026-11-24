import json

import pytest
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from app_service_extensions.hooks import HookRequest, current_request
from app_service_extensions.azure_functions_agents_integration._prompt import (
    _as_chat_prompt,
    _serving,
)
from app_service_extensions.tools import request as request_tools


def mirror_request():
    body = b'{"event":"validation"}'

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": "https",
            "path": "/agents/observer/chat",
            "query_string": b"source=mirror",
            "headers": [
                (b"x-ms-hook-name", b"observer"),
                (b"x-ms-hook-mode", b"mirror"),
                (b"x-ms-hook-path", b"/events"),
                (b"x-ms-hook-request-method", b"PUT"),
            ],
        },
        receive,
    )


def test_mirror_response_is_not_a_patch():
    assert not HookRequest(hook="observer", mode="mirror").answers_with_patch
    assert HookRequest(hook="reviewer", mode="intercept").answers_with_patch


@pytest.mark.asyncio
async def test_mirror_agent_gets_event_prompt_without_replacing_its_response():
    seen = {}

    async def handler(req):
        seen.update(await req.json())
        answer = request_tools.respond(status=207, body='{"mustNotReplace": true}')
        assert "mirror" in answer.lower()
        assert "intercept" in answer.lower()
        assert not current_request().touched
        current_request().respond(status=207, body={"mustNotReplace": True})
        assert current_request().touched
        return Response("observed", status_code=202)

    response = await _serving(handler, _as_chat_prompt)(mirror_request())
    assert seen["prompt"].startswith("Mirrored HTTP request:")
    assert "continues independently" in seen["prompt"]
    assert "cannot change" in seen["prompt"]
    assert '"method": "PUT"' in seen["prompt"]
    assert response.status_code == 202
    assert response.body == b"observed"


@pytest.mark.asyncio
async def test_mirror_stream_stays_a_stream_instead_of_becoming_patch_json():
    async def handler(req):
        assert json.loads((await req.json())["prompt"].split("```json\n")[1].split("\n```")[0])[
            "body"
        ] == '{"event":"validation"}'
        current_request().set_header("X-Not-Applied", "mirror")

        async def chunks():
            yield b"event: observed\n"
            yield b"data: done\n\n"

        return StreamingResponse(chunks(), media_type="text/event-stream")

    response = await _serving(handler, _as_chat_prompt)(mirror_request())
    assert isinstance(response, StreamingResponse)
    assert b"".join([chunk async for chunk in response.body_iterator]) == (
        b"event: observed\ndata: done\n\n"
    )

from __future__ import annotations

import json

import pytest

from daimon_sdk._transport import decode_tool_result
from daimon_sdk.exceptions import DaimonProtocolError
from daimon_sdk.models import ExecResult, SessionHandle


class DummyText:
    def __init__(self, text: str) -> None:
        self.text = text
        self.type = "text"


class DummyResult:
    def __init__(self, *, structured_content=None, data=None, content=None) -> None:
        self.structured_content = structured_content
        self.data = data
        self.content = content or []


def test_decode_tool_result_prefers_structured_content() -> None:
    payload, content = decode_tool_result(
        DummyResult(
            structured_content={"ok": True},
            content=[DummyText(json.dumps({"ok": True}))],
        )
    )
    assert payload == {"ok": True}
    assert content[0]["type"] == "text"


def test_decode_tool_result_raises_for_invalid_text_json() -> None:
    with pytest.raises(DaimonProtocolError):
        decode_tool_result(DummyResult(content=[DummyText("not-json")]))


@pytest.mark.asyncio
async def test_session_handle_wait_for_exit_times_out() -> None:
    class DummyExecAPI:
        async def write_stdin(self, session_id: int, *, chars: str = "", yield_time_ms=None, max_output_tokens=None):
            return ExecResult(
                output="",
                wall_time_seconds=0.0,
                chunk_id="x",
                original_token_count=0,
                session_id=session_id,
                exit_code=None,
                raw_payload={},
            )

    class DummyClient:
        exec = DummyExecAPI()

    handle = SessionHandle(DummyClient(), 123)
    with pytest.raises(TimeoutError):
        await handle.wait_for_exit(timeout_s=0.01, yield_time_ms=1, poll_interval_s=0.001)

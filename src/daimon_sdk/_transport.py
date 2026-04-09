from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from .exceptions import DaimonConnectionError, DaimonProtocolError, DaimonToolError


@dataclass(slots=True)
class ToolCallEnvelope:
    tool_name: str
    payload: dict[str, Any]
    content_blocks: list[dict[str, Any]]
    raw_result: Any


def _content_block_to_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)
    data: dict[str, Any] = {}
    for key in ("type", "text", "data", "mimeType", "mime_type", "annotations"):
        value = getattr(block, key, None)
        if value is not None:
            data[key] = value
    if not data and hasattr(block, "model_dump"):
        dumped = block.model_dump()
        if isinstance(dumped, dict):
            data = dumped
    if not data and hasattr(block, "__dict__"):
        data = {
            key: value
            for key, value in vars(block).items()
            if not key.startswith("_")
        }
    return data


def decode_tool_result(result: Any) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if isinstance(getattr(result, "structured_content", None), dict):
        payload = dict(result.structured_content)
        content = [_content_block_to_dict(block) for block in getattr(result, "content", []) or []]
        return payload, content
    if getattr(result, "data", None) is not None:
        data = result.data
        if isinstance(data, dict):
            payload = dict(data)
        elif isinstance(data, str):
            try:
                payload = json.loads(data)
            except json.JSONDecodeError as exc:
                raise DaimonProtocolError(f"tool response data was not valid JSON: {data}") from exc
        else:
            raise DaimonProtocolError(f"unsupported tool response data type: {type(data)!r}")
        content = [_content_block_to_dict(block) for block in getattr(result, "content", []) or []]
        return payload, content
    content = getattr(result, "content", None) or []
    if content:
        text = getattr(content[0], "text", None)
        if isinstance(text, str):
            try:
                payload = json.loads(text)
            except json.JSONDecodeError as exc:
                raise DaimonProtocolError(f"tool response text was not valid JSON: {text}") from exc
            return payload, [_content_block_to_dict(block) for block in content]
    raise DaimonProtocolError(f"unable to decode tool result: {result!r}")


class FastMCPTransportAdapter:
    def __init__(self, base_url: str, *, access_token: str | None, timeout_s: float) -> None:
        self.base_url = base_url
        self.access_token = access_token
        self.timeout_s = timeout_s
        self._client: Client | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise DaimonConnectionError("client is not connected")
        return self._client

    async def connect(self) -> None:
        if self._client is not None:
            return
        headers = {"X-Access-Token": self.access_token} if self.access_token else None
        transport = StreamableHttpTransport(
            self.base_url,
            headers=headers,
            httpx_client_factory=self._httpx_client_factory,
        )
        client = Client(transport, timeout=self.timeout_s)
        try:
            await client.__aenter__()
        except Exception as exc:  # pragma: no cover - fastmcp exception types vary
            raise DaimonConnectionError(str(exc)) from exc
        self._client = client

    async def close(self) -> None:
        if self._client is None:
            return
        try:
            await self._client.__aexit__(None, None, None)
        finally:
            self._client = None

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        raise_on_error: bool = True,
    ) -> ToolCallEnvelope:
        await self.connect()
        try:
            result = await self.client.call_tool(tool_name, arguments, raise_on_error=False)
        except Exception as exc:  # pragma: no cover - transport-side failures vary
            raise DaimonConnectionError(str(exc)) from exc
        payload, content_blocks = decode_tool_result(result)
        if raise_on_error and isinstance(payload.get("error"), str):
            raise DaimonToolError(payload["error"], tool_name=tool_name, payload=payload)
        return ToolCallEnvelope(
            tool_name=tool_name,
            payload=payload,
            content_blocks=content_blocks,
            raw_result=result,
        )

    def _httpx_client_factory(self, **kwargs: Any) -> httpx.AsyncClient:
        headers = dict(kwargs.pop("headers", {}) or {})
        kwargs.setdefault("timeout", self.timeout_s)
        kwargs.setdefault("follow_redirects", True)
        return httpx.AsyncClient(headers=headers, **kwargs)

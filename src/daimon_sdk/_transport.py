from __future__ import annotations

import json
import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
import anyio
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from .exceptions import (
    DaimonConnectionError,
    DaimonHttpError,
    DaimonProtocolError,
    DaimonToolError,
)


#: Sentinel for ``call_tool(timeout=...)``: fall back to the adapter's
#: configured ``timeout_s`` as the per-request read timeout. Passing ``None``
#: explicitly requests an unbounded read (no client-side timeout), and a float
#: sets an explicit per-call bound that overrides the configured default.
_USE_DEFAULT_TIMEOUT: Any = object()


def _consume_connect_exception(task: "asyncio.Task[None]") -> None:
    """Retrieve a failed connect attempt's exception.

    A waiter cancelled before the attempt settles may never observe the
    failure; consuming it here keeps asyncio from logging "Task exception was
    never retrieved". Waiters still present receive the same exception via
    their shielded await.
    """
    if not task.cancelled():
        task.exception()


@dataclass(slots=True)
class ToolCallEnvelope:
    tool_name: str
    payload: dict[str, Any]
    content_blocks: list[dict[str, Any]]
    display_text: str
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


def content_blocks_display_text(blocks: list[dict[str, Any]]) -> str:
    return "\n".join(
        str(block["text"])
        for block in blocks
        if block.get("type") == "text" and isinstance(block.get("text"), str)
    )


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
        # Serializes all connection lifecycle: connect() publishes its shared
        # connect task under it, and close() holds it for the whole teardown, so
        # no connect can slip a new connection in mid-teardown and concurrent
        # closers serialize instead of seeing half-detached state.
        self._lifecycle_lock = asyncio.Lock()
        # Single-flight connect: concurrent first calls await one shared attempt
        # instead of queueing cumulative retries behind the lifecycle lock.
        self._connect_task: asyncio.Task[None] | None = None

    @property
    def client(self) -> Client:
        if self._client is None:
            raise DaimonConnectionError("client is not connected")
        return self._client

    async def connect(self) -> None:
        while True:
            async with self._lifecycle_lock:
                if self._client is not None:
                    return
                task = self._connect_task
                if task is None or task.done():
                    task = asyncio.ensure_future(self._connect_once())
                    task.add_done_callback(_consume_connect_exception)
                    self._connect_task = task
            # Await the shared attempt OUTSIDE the lock: concurrent callers all
            # observe the same result (success or failure) from one connect, so
            # a failing endpoint costs every waiter a single attempt, not N. The
            # shield isolates each waiter: one caller's cancellation (e.g. its
            # own outer deadline) cancels only its own wait, not the shared
            # attempt for the others. close() cancels the owned task directly.
            await asyncio.shield(task)
            # Re-verify under the lock: the attempt may have completed and then
            # had its client torn down by a concurrent close() before this
            # waiter resumed. Accept it only if it is still the current attempt
            # and its client is still published; otherwise loop and connect
            # fresh.
            async with self._lifecycle_lock:
                if self._connect_task is task and self._client is not None:
                    return

    async def _connect_once(self) -> None:
        headers = {"X-Access-Token": self.access_token} if self.access_token else None
        transport = StreamableHttpTransport(
            self.base_url,
            headers=headers,
            httpx_client_factory=self._httpx_client_factory,
        )
        # No session-level read default: call_tool always passes an explicit
        # per-request timeout so per-call overrides (and explicit unbounded
        # calls) take effect instead of being capped by a construction-time
        # default. init_timeout bounds the initialize handshake specifically.
        client = Client(transport, init_timeout=self.timeout_s)
        try:
            # Cap the whole handshake, not just initialize(): on an init failure
            # fastmcp's cleanup may issue a terminate_session DELETE on the
            # read-unbounded HTTP client, which init_timeout alone does not
            # bound. Note this does NOT make connect time strictly <= timeout_s:
            # fastmcp runs a few seconds of SHIELDED cleanup after the cap
            # fires, so a timed-out connect surfaces after roughly timeout_s
            # plus that cleanup margin. Callers sizing an outer wait must
            # include a connect+cleanup allowance, not just timeout_s.
            with anyio.fail_after(self.timeout_s):
                await client.__aenter__()
        except TimeoutError as exc:
            raise DaimonConnectionError(
                f"MCP connection to {self.base_url} did not complete within "
                f"{self.timeout_s}s"
            ) from exc
        except Exception as exc:  # pragma: no cover - fastmcp exception types vary
            raise DaimonConnectionError(str(exc)) from exc
        self._client = client

    # NOTE on call-vs-close: call_tool uses an established _client without the
    # lifecycle lock, so close() must not be invoked concurrently with in-flight
    # calls — it may tear down the session mid-call. Callers are expected to
    # drain/stop calls before close() (the upper-layer client does).
    async def close(self) -> None:
        # Hold the lock for the whole teardown: concurrent closers serialize,
        # and a connect() cannot publish a new client mid-teardown (it blocks on
        # the lock until the teardown finishes, then sees _client is None).
        async with self._lifecycle_lock:
            task = self._connect_task
            self._connect_task = None
            client = self._client
            self._client = None
            if task is not None and not task.done():
                task.cancel()
                # Wait for the cancelled connect (and its shielded cleanup) to
                # settle so the caller's event loop is not stopped with it
                # still pending.
                try:
                    await task
                except BaseException:  # CancelledError / connect failure expected
                    pass
            if client is not None:
                try:
                    await client.__aexit__(None, None, None)
                except Exception:  # pragma: no cover - best-effort teardown
                    pass

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        raise_on_error: bool = True,
        timeout: Any = _USE_DEFAULT_TIMEOUT,
    ) -> ToolCallEnvelope:
        await self.connect()
        if timeout is _USE_DEFAULT_TIMEOUT:
            timeout = self.timeout_s
        try:
            result = await self.client.call_tool(
                tool_name,
                arguments,
                raise_on_error=False,
                timeout=timeout,
            )
        except Exception as exc:  # pragma: no cover - transport-side failures vary
            raise DaimonConnectionError(str(exc)) from exc
        payload, content_blocks = decode_tool_result(result)
        if raise_on_error and isinstance(payload.get("error"), str):
            raise DaimonToolError(payload["error"], tool_name=tool_name, payload=payload)
        return ToolCallEnvelope(
            tool_name=tool_name,
            payload=payload,
            content_blocks=content_blocks,
            display_text=content_blocks_display_text(content_blocks),
            raw_result=result,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        headers = {"X-Access-Token": self.access_token} if self.access_token else None
        async with self._httpx_client_factory(
            headers=headers,
            timeout=httpx.Timeout(self.timeout_s),
        ) as client:
            url = f"{self._service_base_url()}{path}"
            try:
                response = await client.request(
                    method,
                    url,
                    params=params,
                    content=content,
                )
            except Exception as exc:  # pragma: no cover - transport failures vary
                raise DaimonConnectionError(str(exc)) from exc
        if response.status_code >= 400:
            self._raise_http_error(response.status_code, response.content)
        return response

    @asynccontextmanager
    async def stream_request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        content: Any = None,
    ) -> AsyncIterator[httpx.Response]:
        headers = {"X-Access-Token": self.access_token} if self.access_token else None
        async with self._httpx_client_factory(
            headers=headers,
            timeout=httpx.Timeout(self.timeout_s),
        ) as client:
            url = f"{self._service_base_url()}{path}"
            try:
                async with client.stream(
                    method,
                    url,
                    params=params,
                    content=content,
                ) as response:
                    if response.status_code >= 400:
                        self._raise_http_error(response.status_code, await response.aread())
                    yield response
            except DaimonHttpError:
                raise
            except Exception as exc:  # pragma: no cover - transport failures vary
                raise DaimonConnectionError(str(exc)) from exc

    def _raise_http_error(self, status_code: int, body: bytes) -> None:
        payload: dict[str, Any] = {}
        text = ""
        try:
            text = body.decode("utf-8", errors="replace")
        except Exception:
            text = ""
        try:
            raw_payload = json.loads(text)
            if isinstance(raw_payload, dict):
                payload = dict(raw_payload)
        except Exception:
            payload = {}
        message = payload.get("error")
        if not isinstance(message, str) or not message:
            message = text or f"http {status_code}"
        raise DaimonHttpError(
            message,
            status_code=status_code,
            payload=payload,
        )

    def _httpx_client_factory(self, **kwargs: Any) -> httpx.AsyncClient:
        headers = dict(kwargs.pop("headers", {}) or {})
        # Tool calls are bounded by the MCP per-request read timeout, so keep
        # the HTTP read unbounded here: a flat timeout would otherwise act as a
        # second, shorter cap that kills long-running tool calls and the SSE
        # stream. connect/write/pool stay bounded at ``self.timeout_s``.
        #
        # Note: the MCP per-request timeout only stops WAITING for the response;
        # it does not cancel the in-flight HTTP POST (which runs in the MCP
        # transport's task group) nor the server-side command. That orphaned
        # work is bounded by the server-side command timeout (timeout_ms, else
        # the server's default), after which the POST completes and is dropped.
        kwargs.setdefault("timeout", httpx.Timeout(self.timeout_s, read=None))
        kwargs.setdefault("follow_redirects", True)
        return httpx.AsyncClient(headers=headers, **kwargs)

    def _service_base_url(self) -> str:
        return self.base_url.removesuffix("/mcp")

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import DaimonClient


@dataclass(slots=True)
class ContentBlock:
    type: str | None
    text: str | None = None
    mime_type: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "ContentBlock":
        return cls(
            type=payload.get("type"),
            text=payload.get("text"),
            mime_type=payload.get("mimeType") or payload.get("mime_type"),
            raw=payload,
        )


@dataclass(slots=True)
class ReadTextFile:
    file_path: str
    content: str
    num_lines: int
    start_line: int
    total_lines: int


@dataclass(slots=True)
class ReadImageFile:
    file_path: str
    mime_type: str


@dataclass(slots=True)
class ReadPartsFile:
    file_path: str
    count: int
    pages: str


@dataclass(slots=True)
class ReadResult:
    kind: str
    file: ReadTextFile | ReadImageFile | ReadPartsFile
    extra_content: list[ContentBlock]
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class EditResult:
    file_path: str
    old_string: str
    new_string: str
    original_file: str
    structured_patch: list[dict[str, Any]]
    user_modified: bool
    replace_all: bool
    git_diff: dict[str, Any] | None
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class WriteResult:
    type: str
    file_path: str
    content: str
    structured_patch: list[dict[str, Any]]
    original_file: str | None
    git_diff: dict[str, Any] | None
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class GlobResult:
    search_path: str
    filenames: list[str]
    num_files: int
    truncated: bool
    duration_ms: int
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class GrepResult:
    mode: str
    filenames: list[str]
    num_files: int
    content: str | None
    num_lines: int | None
    num_matches: int | None
    applied_limit: int | None
    applied_offset: int | None
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class ExecResult:
    output: str
    wall_time_seconds: float
    chunk_id: str
    original_token_count: int
    session_id: int | None
    exit_code: int | None
    raw_payload: dict[str, Any]

    @property
    def is_running(self) -> bool:
        return self.session_id is not None and self.exit_code is None

    @property
    def has_exited(self) -> bool:
        return self.exit_code is not None


@dataclass(slots=True)
class BashResult:
    stdout: str
    stderr: str
    interrupted: bool
    dangerously_disable_sandbox: bool
    persisted_output_path: str | None
    persisted_output_size: int | None
    background_task_id: str | None
    raw_payload: dict[str, Any]

    @property
    def is_background(self) -> bool:
        return self.background_task_id is not None


@dataclass(slots=True)
class WebFetchResult:
    url: str
    status_code: int
    content_type: str
    bytes: int
    result_type: str
    content: str
    redirect_url: str | None
    persisted_path: str | None
    persisted_size: int | None
    duration_ms: int
    raw_payload: dict[str, Any]


@dataclass(slots=True)
class RuntimeContextResult:
    payload: dict[str, Any]

    @property
    def base_workdir(self) -> str | None:
        return self.payload.get("baseWorkdir")

    @property
    def summary(self) -> str | None:
        value = self.payload.get("summary")
        return value if isinstance(value, str) else None

    @property
    def filesystem(self) -> dict[str, Any]:
        value = self.payload.get("filesystem")
        return value if isinstance(value, dict) else {}

    @property
    def network(self) -> dict[str, Any]:
        value = self.payload.get("network")
        return value if isinstance(value, dict) else {}


@dataclass(slots=True)
class SessionHandle:
    _client: "DaimonClient"
    session_id: int

    async def write(
        self,
        chars: str = "",
        *,
        yield_time_ms: int | None = None,
        max_output_tokens: int | None = None,
    ) -> ExecResult:
        return await self._client.exec.write_stdin(
            self.session_id,
            chars=chars,
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
        )

    async def poll(
        self,
        *,
        yield_time_ms: int | None = None,
        max_output_tokens: int | None = None,
    ) -> ExecResult:
        return await self.write(
            "",
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
        )

    async def wait_for_exit(
        self,
        *,
        timeout_s: float = 10.0,
        yield_time_ms: int = 5_000,
        poll_interval_s: float = 0.05,
        max_output_tokens: int | None = None,
    ) -> ExecResult:
        deadline = time.monotonic() + timeout_s
        last_result: ExecResult | None = None
        while time.monotonic() < deadline:
            last_result = await self.poll(
                yield_time_ms=yield_time_ms,
                max_output_tokens=max_output_tokens,
            )
            if last_result.has_exited:
                return last_result
            await asyncio.sleep(poll_interval_s)
        raise TimeoutError(f"session {self.session_id} did not exit within {timeout_s} seconds")

    async def close(
        self,
        *,
        exit_payload: str = "__EXIT__\n",
        yield_time_ms: int = 500,
        max_output_tokens: int | None = None,
    ) -> ExecResult:
        return await self.write(
            exit_payload,
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
        )

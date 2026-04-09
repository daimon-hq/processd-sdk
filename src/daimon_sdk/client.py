from __future__ import annotations

from typing import Any

from ._transport import FastMCPTransportAdapter, ToolCallEnvelope
from .models import (
    BashResult,
    ContentBlock,
    EditResult,
    ExecResult,
    GlobResult,
    GrepResult,
    ReadImageFile,
    ReadPartsFile,
    ReadResult,
    ReadTextFile,
    RuntimeContextResult,
    SessionHandle,
    WebFetchResult,
    WriteResult,
)


def _content_blocks(envelope: ToolCallEnvelope) -> list[ContentBlock]:
    return [ContentBlock.from_dict(block) for block in envelope.content_blocks]


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


class RawAPI:
    def __init__(self, client: "DaimonClient") -> None:
        self._client = client

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        raise_on_error: bool = True,
    ) -> dict[str, Any]:
        envelope = await self._client._call_tool(
            name,
            arguments or {},
            raise_on_error=raise_on_error,
        )
        return envelope.payload


class RuntimeAPI:
    def __init__(self, client: "DaimonClient") -> None:
        self._client = client

    async def get_context(self) -> RuntimeContextResult:
        envelope = await self._client._call_tool("GetRuntimeContext", {})
        return RuntimeContextResult(payload=envelope.payload)


class FilesAPI:
    def __init__(self, client: "DaimonClient") -> None:
        self._client = client

    async def read(
        self,
        file_path: str,
        *,
        offset: int | None = None,
        limit: int | None = None,
        pages: str | None = None,
    ) -> ReadResult:
        arguments: dict[str, Any] = {"file_path": file_path}
        if offset is not None:
            arguments["offset"] = offset
        if limit is not None:
            arguments["limit"] = limit
        if pages is not None:
            arguments["pages"] = pages
        envelope = await self._client._call_tool("Read", arguments)
        payload = envelope.payload
        kind = payload.get("type")
        file_data = payload.get("file") or {}
        if kind == "text":
            file_model = ReadTextFile(
                file_path=file_data["filePath"],
                content=file_data["content"],
                num_lines=file_data["numLines"],
                start_line=file_data["startLine"],
                total_lines=file_data["totalLines"],
            )
        elif kind == "image":
            file_model = ReadImageFile(
                file_path=file_data["filePath"],
                mime_type=file_data["mimeType"],
            )
        elif kind == "parts":
            file_model = ReadPartsFile(
                file_path=file_data["filePath"],
                count=file_data["count"],
                pages=file_data["pages"],
            )
        else:
            raise ValueError(f"unsupported read result type: {kind!r}")
        return ReadResult(
            kind=kind,
            file=file_model,
            extra_content=_content_blocks(envelope),
            raw_payload=payload,
        )

    async def edit(
        self,
        file_path: str,
        *,
        old_string: str,
        new_string: str,
        replace_all: bool = False,
    ) -> EditResult:
        envelope = await self._client._call_tool(
            "Edit",
            {
                "file_path": file_path,
                "old_string": old_string,
                "new_string": new_string,
                "replace_all": replace_all,
            },
        )
        payload = envelope.payload
        return EditResult(
            file_path=payload["filePath"],
            old_string=payload["oldString"],
            new_string=payload["newString"],
            original_file=payload["originalFile"],
            structured_patch=payload["structuredPatch"],
            user_modified=payload["userModified"],
            replace_all=payload["replaceAll"],
            git_diff=payload.get("gitDiff"),
            raw_payload=payload,
        )

    async def write(self, file_path: str, content: str) -> WriteResult:
        envelope = await self._client._call_tool(
            "Write",
            {"file_path": file_path, "content": content},
        )
        payload = envelope.payload
        return WriteResult(
            type=payload["type"],
            file_path=payload["filePath"],
            content=payload["content"],
            structured_patch=payload["structuredPatch"],
            original_file=payload.get("originalFile"),
            git_diff=payload.get("gitDiff"),
            raw_payload=payload,
        )

    async def glob(self, pattern: str, *, path: str | None = None) -> GlobResult:
        arguments: dict[str, Any] = {"pattern": pattern}
        if path is not None:
            arguments["path"] = path
        envelope = await self._client._call_tool("Glob", arguments)
        payload = envelope.payload
        return GlobResult(
            search_path=payload["searchPath"],
            filenames=list(payload["filenames"]),
            num_files=payload["numFiles"],
            truncated=payload["truncated"],
            duration_ms=payload["durationMs"],
            raw_payload=payload,
        )

    async def grep(
        self,
        pattern: str,
        *,
        path: str | None = None,
        glob: str | None = None,
        output_mode: str | None = None,
        before: int | None = None,
        after: int | None = None,
        context_n: int | None = None,
        context: int | None = None,
        line_number: bool | None = None,
        ignore_case: bool | None = None,
        file_type: str | None = None,
        head_limit: int | None = None,
        offset: int | None = None,
        multiline: bool | None = None,
    ) -> GrepResult:
        arguments: dict[str, Any] = {"pattern": pattern}
        optional_values = {
            "path": path,
            "glob": glob,
            "output_mode": output_mode,
            "-B": before,
            "-A": after,
            "-C": context_n,
            "context": context,
            "-n": line_number,
            "-i": ignore_case,
            "type": file_type,
            "head_limit": head_limit,
            "offset": offset,
            "multiline": multiline,
        }
        for key, value in optional_values.items():
            if value is not None:
                arguments[key] = value
        envelope = await self._client._call_tool("Grep", arguments)
        payload = envelope.payload
        return GrepResult(
            mode=payload["mode"],
            filenames=list(payload["filenames"]),
            num_files=payload["numFiles"],
            content=payload.get("content"),
            num_lines=payload.get("numLines"),
            num_matches=payload.get("numMatches"),
            applied_limit=payload.get("appliedLimit"),
            applied_offset=payload.get("appliedOffset"),
            raw_payload=payload,
        )


class ExecAPI:
    def __init__(self, client: "DaimonClient") -> None:
        self._client = client

    async def bash(
        self,
        command: str,
        *,
        timeout_ms: int | None = None,
        description: str | None = None,
        run_in_background: bool = False,
        dangerously_disable_sandbox: bool = False,
    ) -> BashResult:
        arguments: dict[str, Any] = {
            "command": command,
            "run_in_background": run_in_background,
            "dangerouslyDisableSandbox": dangerously_disable_sandbox,
        }
        if timeout_ms is not None:
            arguments["timeout"] = timeout_ms
        if description is not None:
            arguments["description"] = description
        envelope = await self._client._call_tool("Bash", arguments)
        payload = envelope.payload
        return BashResult(
            stdout=payload["stdout"],
            stderr=payload["stderr"],
            interrupted=payload["interrupted"],
            dangerously_disable_sandbox=payload["dangerouslyDisableSandbox"],
            persisted_output_path=payload.get("persistedOutputPath"),
            persisted_output_size=payload.get("persistedOutputSize"),
            background_task_id=payload.get("backgroundTaskId"),
            raw_payload=payload,
        )

    async def exec_command(
        self,
        cmd: str,
        *,
        workdir: str | None = None,
        shell: str | None = None,
        tty: bool = False,
        yield_time_ms: int | None = None,
        max_output_tokens: int | None = None,
        login: bool | None = None,
        sandbox_permissions: str | None = None,
        justification: str | None = None,
        prefix_rule: list[str] | None = None,
    ) -> ExecResult:
        arguments: dict[str, Any] = {"cmd": cmd, "tty": tty}
        optional_values = {
            "workdir": workdir,
            "shell": shell,
            "yield_time_ms": yield_time_ms,
            "max_output_tokens": max_output_tokens,
            "login": login,
            "sandbox_permissions": sandbox_permissions,
            "justification": justification,
            "prefix_rule": prefix_rule,
        }
        for key, value in optional_values.items():
            if value is not None:
                arguments[key] = value
        envelope = await self._client._call_tool("exec_command", arguments)
        return self._exec_from_payload(envelope.payload)

    async def start_session(
        self,
        cmd: str,
        *,
        workdir: str | None = None,
        shell: str | None = None,
        tty: bool = False,
        yield_time_ms: int | None = None,
        max_output_tokens: int | None = None,
        login: bool | None = None,
        sandbox_permissions: str | None = None,
        justification: str | None = None,
        prefix_rule: list[str] | None = None,
    ) -> SessionHandle:
        result = await self.exec_command(
            cmd,
            workdir=workdir,
            shell=shell,
            tty=tty,
            yield_time_ms=yield_time_ms,
            max_output_tokens=max_output_tokens,
            login=login,
            sandbox_permissions=sandbox_permissions,
            justification=justification,
            prefix_rule=prefix_rule,
        )
        if result.session_id is None:
            raise RuntimeError("processd did not return a running session")
        return SessionHandle(self._client, result.session_id)

    async def write_stdin(
        self,
        session_id: int,
        *,
        chars: str = "",
        yield_time_ms: int | None = None,
        max_output_tokens: int | None = None,
    ) -> ExecResult:
        arguments: dict[str, Any] = {"session_id": session_id, "chars": chars}
        if yield_time_ms is not None:
            arguments["yield_time_ms"] = yield_time_ms
        if max_output_tokens is not None:
            arguments["max_output_tokens"] = max_output_tokens
        envelope = await self._client._call_tool("write_stdin", arguments)
        return self._exec_from_payload(envelope.payload)

    @staticmethod
    def _exec_from_payload(payload: dict[str, Any]) -> ExecResult:
        return ExecResult(
            output=payload.get("output", ""),
            wall_time_seconds=float(payload.get("wall_time_seconds", 0.0)),
            chunk_id=str(payload.get("chunk_id", "")),
            original_token_count=int(payload.get("original_token_count", 0)),
            session_id=_int_or_none(payload.get("session_id")),
            exit_code=_int_or_none(payload.get("exit_code")),
            raw_payload=payload,
        )


class WebAPI:
    def __init__(self, client: "DaimonClient") -> None:
        self._client = client

    async def fetch(
        self,
        url: str,
        *,
        timeout_ms: int | None = None,
        max_bytes: int | None = None,
        follow_same_host_redirects: bool | None = None,
    ) -> WebFetchResult:
        arguments: dict[str, Any] = {"url": url}
        if timeout_ms is not None:
            arguments["timeout_ms"] = timeout_ms
        if max_bytes is not None:
            arguments["max_bytes"] = max_bytes
        if follow_same_host_redirects is not None:
            arguments["follow_same_host_redirects"] = follow_same_host_redirects
        envelope = await self._client._call_tool("WebFetch", arguments)
        payload = envelope.payload
        return WebFetchResult(
            url=payload["url"],
            status_code=payload["statusCode"],
            content_type=payload["contentType"],
            bytes=payload["bytes"],
            result_type=payload["resultType"],
            content=payload["content"],
            redirect_url=payload.get("redirectUrl"),
            persisted_path=payload.get("persistedPath"),
            persisted_size=payload.get("persistedSize"),
            duration_ms=payload["durationMs"],
            raw_payload=payload,
        )


class DaimonClient:
    def __init__(
        self,
        base_url: str,
        *,
        access_token: str | None = None,
        timeout_s: float = 30.0,
    ) -> None:
        self.base_url = base_url
        self.access_token = access_token
        self.timeout_s = timeout_s
        self._transport = FastMCPTransportAdapter(
            base_url,
            access_token=access_token,
            timeout_s=timeout_s,
        )
        self.raw = RawAPI(self)
        self.runtime = RuntimeAPI(self)
        self.files = FilesAPI(self)
        self.exec = ExecAPI(self)
        self.web = WebAPI(self)

    async def connect(self) -> "DaimonClient":
        await self._transport.connect()
        return self

    async def close(self) -> None:
        await self._transport.close()

    async def __aenter__(self) -> "DaimonClient":
        return await self.connect()

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def _call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        raise_on_error: bool = True,
    ) -> ToolCallEnvelope:
        return await self._transport.call_tool(
            tool_name,
            arguments,
            raise_on_error=raise_on_error,
        )

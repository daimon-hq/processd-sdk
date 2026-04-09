from __future__ import annotations

from typing import Any


class DaimonError(Exception):
    """Base SDK error."""


class DaimonConnectionError(DaimonError):
    """Raised when the SDK cannot connect to the MCP endpoint."""


class DaimonProtocolError(DaimonError):
    """Raised when the MCP response cannot be decoded into a processd payload."""


class DaimonToolError(DaimonError):
    """Raised when processd returns a structured tool error."""

    def __init__(self, message: str, *, tool_name: str, payload: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.tool_name = tool_name
        self.payload = payload or {}


class DaimonHttpError(DaimonError):
    """Raised when a SDK-only HTTP endpoint returns a non-success response."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        payload: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.payload = payload or {}

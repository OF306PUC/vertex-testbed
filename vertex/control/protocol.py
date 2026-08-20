"""Hub <-> agent control protocol: runs on every Raspberry Pi or linux embedded device.

Framing
-------
Each message is one JSON object on one line. A response carrying bulk data follows
its header line with exactly ``n_bytes`` raw bytes::

    {"ok": true, "kind": "blob", "n_bytes": 786432, "name": "7.bin"}\\n
    <786432 raw bytes>
"""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

__all__ = ["PROTOCOL_VERSION", "MAX_LINE", "Command", "Request", "Response",
           "ProtocolError", "encode_request", "decode_request",
           "encode_response", "decode_response", "ok", "fail"]

PROTOCOL_VERSION = 1

#: Refuse absurd header lines rather than buffering without bound. Bulk data is
#: length-prefixed and never travels as a line, so a legitimate header is small.
MAX_LINE = 1 << 20


class ProtocolError(ValueError):
    """Malformed or unsupported control message."""


Command = Literal["status", "configure", "start", "stop", "list_runs", "fetch"]


class Request(BaseModel):
    """One command from the hub.

    ``args`` is deliberately untyped here. The control plane's job is to carry a
    parameter set the agent already knows how to validate.
    """

    model_config = ConfigDict(extra="forbid")

    command: Command
    args: dict[str, Any] = Field(default_factory=dict)
    version: int = PROTOCOL_VERSION


class Response(BaseModel):
    """One reply. ``kind='blob'`` means ``n_bytes`` raw bytes follow the line."""

    model_config = ConfigDict(extra="forbid")

    ok: bool
    kind: Literal["json", "blob"] = "json"
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    n_bytes: int = 0
    name: str = ""
    version: int = PROTOCOL_VERSION


def ok(**data: Any) -> Response:
    return Response(ok=True, data=data)


def fail(error: str) -> Response:
    return Response(ok=False, error=error)


def encode_request(req: Request) -> bytes:
    return (json.dumps(req.model_dump(), separators=(",", ":")) + "\n").encode()


def encode_response(resp: Response) -> bytes:
    return (json.dumps(resp.model_dump(), separators=(",", ":")) + "\n").encode()


def _parse(line: bytes) -> dict[str, Any]:
    if len(line) > MAX_LINE:
        raise ProtocolError(f"header line of {len(line)} bytes exceeds {MAX_LINE}")
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError(f"not valid JSON: {exc}") from None
    if not isinstance(obj, dict):
        raise ProtocolError(f"expected a JSON object, got {type(obj).__name__}")
    return obj


def _check_version(obj: dict[str, Any]) -> None:
    v = obj.get("version", PROTOCOL_VERSION)
    if v != PROTOCOL_VERSION:
        raise ProtocolError(
            f"protocol version {v} but this build speaks {PROTOCOL_VERSION}; "
            "hub and agents are on different releases"
        )


def decode_request(line: bytes) -> Request:
    obj = _parse(line)
    _check_version(obj)
    try:
        return Request.model_validate(obj)
    except Exception as exc:
        raise ProtocolError(f"invalid request: {exc}") from None


def decode_response(line: bytes) -> Response:
    obj = _parse(line)
    _check_version(obj)
    try:
        return Response.model_validate(obj)
    except Exception as exc:
        raise ProtocolError(f"invalid response: {exc}") from None

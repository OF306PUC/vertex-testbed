"""Reading and writing control messages over asyncio streams.
"""

from __future__ import annotations

import asyncio

from .protocol import (MAX_LINE, ProtocolError, Request, Response, decode_request,
                       decode_response, encode_request, encode_response)

__all__ = ["read_request", "read_response", "write_request", "write_response",
           "read_blob"]


async def _read_line(reader: asyncio.StreamReader) -> bytes | None:
    """One header line, or ``None`` at a clean end of stream."""
    try:
        line = await reader.readuntil(b"\n")
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None                     # peer closed between messages: normal
        raise ProtocolError("stream ended mid-message") from None
    except asyncio.LimitOverrunError:
        raise ProtocolError(f"header line exceeds {MAX_LINE} bytes") from None
    return line.rstrip(b"\n")


async def read_request(reader: asyncio.StreamReader) -> Request | None:
    line = await _read_line(reader)
    return None if line is None else decode_request(line)


async def read_response(reader: asyncio.StreamReader) -> Response | None:
    line = await _read_line(reader)
    return None if line is None else decode_response(line)


async def write_request(writer: asyncio.StreamWriter, req: Request) -> None:
    writer.write(encode_request(req))
    await writer.drain()


async def write_response(
    writer: asyncio.StreamWriter, resp: Response, blob: bytes | None = None
) -> None:
    """Send a response, followed by its raw payload when ``kind='blob'``.
    """
    if resp.kind == "blob":
        if blob is None:
            raise ValueError("blob response requires a payload")
        if resp.n_bytes != len(blob):
            raise ValueError(
                f"declared n_bytes={resp.n_bytes} but payload is {len(blob)}"
            )
    writer.write(encode_response(resp))
    if resp.kind == "blob" and blob:
        writer.write(blob)
    await writer.drain()


async def read_blob(reader: asyncio.StreamReader, n_bytes: int) -> bytes:
    """Read exactly ``n_bytes``.
    """
    if n_bytes == 0:
        return b""
    try:
        return await reader.readexactly(n_bytes)
    except asyncio.IncompleteReadError as exc:
        raise ProtocolError(
            f"expected {n_bytes} bytes, connection closed after {len(exc.partial)}"
        ) from None

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


#: Read granularity. Large enough that a multi-megabyte rows file is not a
#: million awaits, small enough that the stall deadline is checked often.
BLOB_CHUNK = 1 << 16


async def read_blob(reader: asyncio.StreamReader, n_bytes: int, *,
                    stall_timeout: float | None = None) -> bytes:
    """Read exactly ``n_bytes``, tolerating a slow link but not a dead one.

    ``stall_timeout`` bounds the gap between chunks, NOT the transfer. The
    distinction is the whole point: a rows file grows with the neighbour count
    and the sample count, so at degree 4 and 50 Hz it is ~2 MB per node and the
    fleet fetches ~60 MB over one 2.4 GHz WLAN. A deadline on the whole transfer
    kills a download that is making steady progress -- measured on `n30-ring4`,
    where 24 of 30 nodes failed with `truncated transfer` while the run itself
    was intact and the data still sat on the Pis. A stall deadline still catches
    the case it was there for, a peer that has stopped sending.
    """
    if n_bytes == 0:
        return b""
    buf = bytearray()
    while len(buf) < n_bytes:
        want = min(BLOB_CHUNK, n_bytes - len(buf))
        try:
            chunk = await (reader.read(want) if stall_timeout is None
                           else asyncio.wait_for(reader.read(want), stall_timeout))
        except asyncio.TimeoutError:
            raise ProtocolError(
                f"stalled after {len(buf)} of {n_bytes} bytes; "
                f"no data for {stall_timeout}s"
            ) from None
        if not chunk:
            raise ProtocolError(
                f"expected {n_bytes} bytes, connection closed after {len(buf)}"
            ) from None
        buf += chunk
    return bytes(buf)

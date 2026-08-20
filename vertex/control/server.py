"""The agent's control server.

Listens for hub commands and applies them to an :class:`~vertex.agent.Agent`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Awaitable, Callable

from .protocol import ProtocolError, Request, Response, fail, ok
from .transport import read_request, write_response

__all__ = ["CommandHandler", "ControlServer"]

#: A command implementation. Returns a response, optionally with a payload.
CommandHandler = Callable[[dict[str, Any]], Awaitable[tuple[Response, bytes | None]]]


class ControlServer:
    """Serves the six control commands for one agent."""

    def __init__(
        self,
        handlers: dict[str, CommandHandler],
        *,
        host: str = "0.0.0.0",
        port: int = 3001,
    ) -> None:
        self.handlers = handlers
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        #: Open client connections. Tracked because `stop()` has to close them:
        #: from Python 3.12 `Server.wait_closed()` waits for every live handler, so
        #: a hub that is still connected makes shutdown hang forever -- a SIGTERM
        #: to an agent mid-experiment would then need a SIGKILL behind it.
        self._writers: set[asyncio.StreamWriter] = set()
        self.served = 0
        self.errors = 0

    @property
    def bound_port(self) -> int:
        if self._server is None or not self._server.sockets:
            return self.port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> "ControlServer":
        if self._server is None:
            self._server = await asyncio.start_server(
                self._serve, self.host, self.port, reuse_address=True
            )
        return self

    async def stop(self, *, drain_timeout: float = 2.0) -> None:
        """Stop listening and close every open connection.
        """
        if self._server is None:
            return
        self._server.close()

        for w in list(self._writers):
            try:
                w.close()
            except Exception:
                pass
        self._writers.clear()

        try:
            await asyncio.wait_for(self._server.wait_closed(), drain_timeout)
        except (asyncio.TimeoutError, Exception):
            # Listening socket is already closed, so nothing new can arrive; a
            # straggling handler is not worth blocking the shutdown for.
            pass
        self._server = None

    async def _serve(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        self._writers.add(writer)
        try:
            while True:
                try:
                    req = await read_request(reader)
                except ProtocolError as exc:
                    # Malformed input is the peer's problem, not ours. Report it and
                    # close: the stream position is no longer trustworthy.
                    self.errors += 1
                    await write_response(writer, fail(f"protocol error: {exc}"))
                    return
                if req is None:
                    return                          # clean close

                handler = self.handlers.get(req.command)
                if handler is None:
                    self.errors += 1
                    await write_response(
                        writer,
                        fail(f"unsupported command {req.command!r}; this build "
                             f"serves {sorted(self.handlers)}"))
                    continue

                try:
                    resp, blob = await handler(req.args)
                except Exception as exc:
                    # Serving is best-effort; the agent keeps running. A node that
                    # died on a bad request would take its data with it.
                    self.errors += 1
                    resp, blob = fail(f"{type(exc).__name__}: {exc}"), None

                await write_response(writer, resp, blob)
                self.served += 1
        except (ConnectionResetError, BrokenPipeError, asyncio.CancelledError):
            pass
        finally:
            self._writers.discard(writer)
            try:
                writer.close()
            except Exception:                                   # pragma: no cover
                pass

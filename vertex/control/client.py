"""The hub's control client.

One connection per agent, reused across commands.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .protocol import ProtocolError, Request, Response
from .transport import read_blob, read_response, write_request

__all__ = ["ControlClient", "ControlError"]


class ControlError(RuntimeError):
    """The agent refused a command, or could not be reached."""


class ControlClient:
    """Talks to one agent's :class:`~vertex.control.server.ControlServer`."""

    def __init__(self, host: str, port: int, *, timeout: float = 10.0,
                 node_id: int | None = None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.node_id = node_id
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._lock = asyncio.Lock()

    @property
    def label(self) -> str:
        return f"node {self.node_id}" if self.node_id is not None else f"{self.host}:{self.port}"

    async def connect(self) -> None:
        if self._writer is not None:
            return
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), self.timeout)
        except (OSError, asyncio.TimeoutError) as exc:
            raise ControlError(f"{self.label}: cannot connect ({exc})") from None

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except Exception:                                   # pragma: no cover
                pass
        self._reader = self._writer = None

    async def call(self, command: str, **args: Any) -> tuple[Response, bytes]:
        """Send one command and await its reply.

        Serialised by a lock. The hub fans out across nodes concurrently, and two
        overlapping commands on one connection would interleave their replies.
        """
        try:
            request = Request(command=command, args=args)
        except Exception:
            # Caught here rather than passed to pydantic's formatter: the caller
            # asked for a command that does not exist, and the useful reply is the
            # list of ones that do.
            from .protocol import Command
            import typing
            raise ControlError(
                f"{self.label}: unknown command {command!r}; "
                f"available: {sorted(typing.get_args(Command))}"
            ) from None

        async with self._lock:
            await self.connect()
            assert self._reader is not None and self._writer is not None
            try:
                await write_request(self._writer, request)
                resp = await asyncio.wait_for(
                    read_response(self._reader), self.timeout)
            except (OSError, asyncio.TimeoutError, ProtocolError) as exc:
                await self.close()
                raise ControlError(f"{self.label}: {command} failed ({exc})") from None

            if resp is None:
                await self.close()
                raise ControlError(f"{self.label}: connection closed during {command}")
            if not resp.ok:
                raise ControlError(f"{self.label}: {command} refused -- {resp.error}")

            blob = b""
            if resp.kind == "blob":
                try:
                    blob = await asyncio.wait_for(
                        read_blob(self._reader, resp.n_bytes), self.timeout)
                except (asyncio.TimeoutError, ProtocolError) as exc:
                    await self.close()
                    raise ControlError(
                        f"{self.label}: truncated transfer of {resp.name} ({exc})"
                    ) from None
            return resp, blob

    # control operations: ----------------------------------------------------------------
    async def status(self) -> dict[str, Any]:
        return (await self.call("status"))[0].data

    async def configure(self, **params: Any) -> dict[str, Any]:
        return (await self.call("configure", **params))[0].data

    async def start(self, run_name: str, **kw: Any) -> dict[str, Any]:
        return (await self.call("start", run_name=run_name, **kw))[0].data

    async def stop(self) -> dict[str, Any]:
        return (await self.call("stop"))[0].data

    async def list_runs(self) -> list[str]:
        return (await self.call("list_runs"))[0].data.get("runs", [])

    async def fetch(self, run_name: str, artifact: str) -> bytes:
        return (await self.call("fetch", run_name=run_name, artifact=artifact))[1]

    async def fetch_named(self, run_name: str, artifact: str) -> tuple[str, bytes]:
        """As :meth:`fetch`, but also the agent's own filename for the artefact.

        The name carries the format: a rows file is `<node>.bin`, `.csv` or
        `.jsonl`, and `recover_rows` dispatches on that suffix. A collector that
        invents its own name discards the only thing that says how to read the
        bytes back.
        """
        resp, blob = await self.call("fetch", run_name=run_name, artifact=artifact)
        return (resp.name or ""), blob

    async def __aenter__(self) -> "ControlClient":
        await self.connect()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

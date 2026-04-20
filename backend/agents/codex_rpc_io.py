from __future__ import annotations

import asyncio


async def read_jsonrpc_line(reader: asyncio.StreamReader, separator: bytes = b"\n") -> bytes:
    """Read a JSON-RPC line even when it exceeds StreamReader's configured limit."""
    chunks: list[bytes] = []
    while True:
        try:
            line = await reader.readuntil(separator)
            if not chunks:
                return line
            chunks.append(line)
            return b"".join(chunks)
        except asyncio.LimitOverrunError as exc:
            # The buffer keeps the oversized chunk intact. Drain the safe prefix and continue
            # until we can consume the separator-bearing tail.
            if exc.consumed <= 0:
                raise
            chunks.append(await reader.readexactly(exc.consumed))
        except asyncio.IncompleteReadError as exc:
            if chunks or exc.partial:
                chunks.append(exc.partial)
                return b"".join(chunks)
            return b""

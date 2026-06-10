#!/usr/bin/env python3
"""
Temporary TCP proxy: 0.0.0.0:11435 → 127.0.0.1:11434 (Ollama)

Needed on Linux when Ollama binds only to 127.0.0.1, making it unreachable
from Docker containers. Run once in the background before `docker compose up`:

    python3 scripts/ollama-proxy.py &

Preferred fix (survives reboots, no extra process):
    sudo systemctl edit ollama --force  # add Environment="OLLAMA_HOST=0.0.0.0:11434"
    sudo systemctl daemon-reload && sudo systemctl restart ollama
    # Then delete this proxy and revert litellm_config.yaml to port 11434.
"""

import asyncio
import sys

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 11435
TARGET_HOST = "127.0.0.1"
TARGET_PORT = 11434


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while not reader.at_eof():
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    finally:
        writer.close()


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    peer = client_w.get_extra_info("peername")
    try:
        target_r, target_w = await asyncio.open_connection(TARGET_HOST, TARGET_PORT)
    except OSError as exc:
        print(f"[proxy] cannot reach Ollama at {TARGET_HOST}:{TARGET_PORT}: {exc}", file=sys.stderr)
        client_w.close()
        return
    await asyncio.gather(_pipe(client_r, target_w), _pipe(target_r, client_w))


async def main() -> None:
    server = await asyncio.start_server(handle, LISTEN_HOST, LISTEN_PORT)
    addr = server.sockets[0].getsockname()
    print(f"[proxy] listening on {addr[0]}:{addr[1]} → {TARGET_HOST}:{TARGET_PORT}", flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())

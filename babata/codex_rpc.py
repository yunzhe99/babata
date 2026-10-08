"""Minimal asynchronous client for the official Codex app-server stdio protocol."""

import asyncio
import contextlib
import json


class CodexRPC:
    def __init__(self, command, env, on_event):
        self.command, self.env, self.on_event = command, env, on_event
        self.pending = {}
        self.counter = 0
        self.process = None
        self.reader = None
        self.write_lock = asyncio.Lock()

    async def start(self):
        self.process = await asyncio.create_subprocess_exec(
            *self.command,
            env=self.env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            limit=16 * 1024 * 1024,
        )
        self.reader = asyncio.create_task(self.read())
        await self.call(
            "initialize",
            {
                "clientInfo": {"name": "babata_tokyo", "version": "0.2"},
                "capabilities": {"experimentalApi": True},
            },
        )
        await self.write({"method": "initialized", "params": {}})

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                value = json.loads(line)
                if "method" not in value and value.get("id") in self.pending:
                    future = self.pending.pop(value["id"])
                    if future.done():
                        continue
                    if "error" in value:
                        future.set_exception(
                            RuntimeError("Codex RPC error " + str(value["error"].get("code")))
                        )
                    else:
                        future.set_result(value.get("result"))
                else:
                    self.on_event(value)
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Codex exited"))
            self.pending.clear()
            self.on_event({"method": "__closed"})

    async def write(self, value):
        async with self.write_lock:
            self.process.stdin.write((json.dumps(value, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()

    async def call(self, method, params, timeout=60):
        self.counter += 1
        identity = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[identity] = future
        await self.write({"id": identity, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(identity, None)

    async def close(self):
        if self.process and self.process.returncode is None:
            self.process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 10)
            if self.process.returncode is None:
                self.process.kill()
                await self.process.wait()
        if self.reader:
            with contextlib.suppress(Exception):
                await self.reader

"""The OneBot side of the bridge: sending text and audio through NapCat, with one rate limiter for everything."""
import asyncio
import base64
import json
import random
import time
import uuid

from .voice import validate_wav


class OneBot:
    """Needs self.ws (the open socket), self.pending, self.send_lock, self.last_send, self.cfg and self.blocked()."""

    async def send(self, source, text):
        # One limiter for progress, answers and commands, including long chunks.
        async with self.send_lock:
            if self.blocked(source):
                return
            await self._send(source, text)

    async def _send(self, source, text):
        # Group members listed in mention_qq are @-mentioned on replies to them.
        mention = source.kind == "group" and str(source.sender_id) in {str(q) for q in self.cfg.get("mention_qq", ())}
        for start in range(0, len(text), 1600):
            message = []
            if start == 0 and mention:
                message.append({"type": "at", "data": {"qq": str(source.sender_id)}})
            message.append({"type": "text", "data": {"text": text[start:start + 1600]}})
            await self._send_message(source, message)

    async def _send_message(self, source, message, timeout=20):
        """Caller holds send_lock; all message types share the same limiter."""
        delay = random.uniform(2.0, 3.0) - (time.monotonic() - self.last_send)
        if delay > 0:
            await asyncio.sleep(delay)
        if self.blocked(source):
            return
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            self.last_send = time.monotonic()
            await self.ws.send(json.dumps({"action": f"send_{source.kind}_msg", "params": {
                "group_id" if source.kind == "group" else "user_id": source.target,
                "message": message,
            }, "echo": echo}))
            response = await asyncio.wait_for(future, timeout=timeout)
            if response.get("status") != "ok" or response.get("retcode") != 0:
                raise RuntimeError("QQ rejected outgoing message")
            self.remember_message_origin((response.get('data') or {}).get('message_id'), source)
        finally:
            self.pending.pop(echo, None)

    def remember_message_origin(self, ident, source):
        if ident is None:
            return
        origins = self.__dict__.setdefault('_message_origins', {})
        origins[str(ident)] = (source.kind, source.target)
        while len(origins) > 1024:
            origins.pop(next(iter(origins)))

    async def send_audio(self, source, audio):
        validate_wav(audio)
        # Base64 avoids dependencies on a shared filesystem with NapCat.
        message = [{'type': 'record', 'data': {
            'file': 'base64://' + base64.b64encode(audio).decode('ascii')}}]
        async with self.send_lock:
            if not self.blocked(source):
                await self._send_message(source, message, timeout=90)

    async def onebot_call(self, action, params):
        echo = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[echo] = future
        try:
            await self.ws.send(json.dumps({'action': action, 'params': params, 'echo': echo}))
            response = await asyncio.wait_for(future, 120)
            if response.get('status') != 'ok' or response.get('retcode') != 0:
                raise ValueError('NapCat 下载附件失败，文件可能已过期')
            return response.get('data') or {}
        finally:
            self.pending.pop(echo, None)

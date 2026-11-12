"""A Bridge on a temporary folder with controlled agents: no network, no QQ, no model calls."""
import asyncio
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.agents.base import Interrupted
from qq_chat.bridge import Bridge, Incoming, Source
from tests.test_agents import FakeAgent


class Backend(FakeAgent):
    def __init__(self):
        super().__init__('backend result')
        self.release.clear()
        self.interrupt_mode = 'stopped'
        self.interrupts = []

    async def interrupt(self, key):
        self.interrupts.append(key)
        if self.interrupt_mode == 'failed':
            raise RuntimeError('RPC failed')
        if self.interrupt_mode == 'waiting':
            return True
        if self.interrupt_mode == 'stopped':
            self.error = Interrupted('confirmed')
        elif self.interrupt_mode == 'failed_outcome':
            self.error = RuntimeError('execution failed')
        self.release.set()
        return True


class BridgeCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        (self.root / 'workspace').mkdir()
        (self.root / 'workspace/SOUL.md').write_text('persona')
        (self.root / 'workspace/OWNER.md').write_text('owner private data')
        self.patcher = patch('qq_chat.bridge.BASE', self.root)
        self.patcher.start()
        self.cfg = {'owner_qq': 10001, 'agents': {'front': 'front', 'guest': 'front',
                    'back': ['work'], 'profiles': {
                        'front': {'type': 'codex_app', 'path': 'unused'},
                        'work': {'type': 'codex_app', 'path': 'unused', 'writes': True}}},
                    'tasks': {'progress': 'silent'}}
        self.bridge = Bridge(self.cfg)
        self.front, self.back = FakeAgent('front reply'), Backend()
        self.front.name, self.back.name = 'front', 'work'
        self.front.profile = self.cfg['agents']['profiles']['front']
        self.back.profile = self.cfg['agents']['profiles']['work']
        self.bridge.agents.update(front=self.front, work=self.back)
        self.owner = Source('private', 10001, 10001)
        self.group = Source('group', 20001, 10001)
        self.guest = Source('group', 20001, 30001, '主人')
        self.sent, self.voices = [], []
        self.fail_send = False

        async def send(source, text):
            if self.fail_send:
                raise RuntimeError('offline')
            self.sent.append((source, text))

        async def voice(source, text):
            self.voices.append((source, text))

        self.bridge.send, self.bridge.enqueue_voice = send, voice

    async def asyncTearDown(self):
        self.front.release.set()
        self.back.release.set()
        await self.bridge.tool_server.close()
        await self.bridge.tasks.close()
        self.bridge.db.close()
        self.patcher.stop()
        self.folder.cleanup()

    async def until(self, predicate):
        async with asyncio.timeout(2):
            while not predicate():
                await asyncio.sleep(.001)

    async def worker_command(self, text, source=None):
        job = asyncio.create_task(self.bridge.worker())
        try:
            await self.bridge.queue.put(Incoming(source or self.owner, text))
            await asyncio.wait_for(self.bridge.queue.join(), 2)
        finally:
            job.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await job

    async def setup_tools(self):
        await self.bridge.tool_server.start()
        self.cfg['agents']['profiles']['front']['type'] = 'claude_code'
        return self.bridge.tool_server

    def token(self, grant):
        return json.loads(Path(grant.config_path).read_text())['mcpServers']['qq']['env']['QQ_TOOL_TOKEN']

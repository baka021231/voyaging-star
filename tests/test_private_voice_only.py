"""Private speech routing and durable model-independent escape commands."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from qq_chat.bridge import Source
from qq_chat.voice_mode import command, is_voice_command
from tests import test_group_voice_only as fixtures
LINE = fixtures.LINE


class PrivateVoiceOnlyTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.GroupVoiceOnlyTests.asyncSetUp
    enable = fixtures.GroupVoiceOnlyTests.enable

    async def test_owner_voice_only_suppresses_stream_and_reads_reply(self):
        self.enable()
        self.bridge.cfg['voice']['private_voice_only'] = True
        reply, streamed = await self.bridge.chat(self.owner, '你好')
        self.assertFalse(streamed)
        self.assertIsNone(self.fake.requests[-1].on_answer)
        self.assertIsNone(self.fake.requests[-1].on_progress)
        await self.bridge.deliver(self.owner, reply, '你好', streamed=streamed)
        await asyncio.wait_for(self.bridge.voice.queue.join(), 2)
        self.assertEqual(self.sent, [])
        self.assertEqual(self.audio, [self.owner])
        self.assertFalse(self.bridge.voice_only(Source('private', 30001, 30001)))
        self.assertFalse(self.bridge.voice_only(Source('private', 30001, self.owner.sender_id)))

    async def test_switch_off_during_turn_still_delivers_final_text(self):
        self.enable()
        self.bridge.cfg['voice']['private_voice_only'] = True
        self.fake.release.clear()
        task = asyncio.create_task(self.bridge.chat(self.owner, '你好'))
        await self.fake.entered.wait()
        self.bridge.cfg['voice']['private_voice_only'] = False
        self.fake.release.set()
        reply, streamed = await task
        self.assertFalse(streamed)
        await self.bridge.deliver(self.owner, reply, '你好', streamed=streamed)
        self.assertEqual(self.sent, [(self.owner, reply)])

    async def test_controls_persist_preserve_other_settings_and_work_without_speech(self):
        self.enable()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'config.json'
            path.write_text(json.dumps({'voice': {'group_voice_only': True}, 'keep': 42}))
            await command(self.bridge, self.owner, '/voice on', root)
            cfg = json.loads(path.read_text())
            self.assertTrue(cfg['voice']['private_voice_only'])
            self.assertTrue(cfg['voice']['group_voice_only'])
            self.assertEqual(cfg['keep'], 42)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(self.bridge.voice_only(self.owner))
            voice, self.bridge.voice = self.bridge.voice, None
            await command(self.bridge, self.owner, '切回文字', root)
            self.assertFalse(json.loads(path.read_text())['voice']['private_voice_only'])
            self.assertFalse(self.bridge.cfg['voice']['private_voice_only'])
            await command(self.bridge, self.owner, '/voice on', root)
            self.assertIn('尚未启用', self.sent[-1][1])
            self.assertFalse(json.loads(path.read_text())['voice']['private_voice_only'])
            self.bridge.voice = voice

    async def test_unauthorized_or_invalid_commands_do_not_modify_config(self):
        self.enable()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'config.json'
            original = '{"voice": {"private_voice_only": false}}'
            path.write_text(original)
            for source in (Source('private', 30001, 30001),
                           Source('private', 30001, self.owner.sender_id),
                           Source('group', 20001, self.owner.sender_id)):
                await command(self.bridge, source, '/voice on', root)
                self.assertEqual(path.read_text(), original)
            for text in ('/voice maybe', '/voice on extra'):
                await command(self.bridge, self.owner, text, root)
                self.assertEqual(path.read_text(), original)
            self.assertTrue(is_voice_command('/VOICE off'))
            self.assertTrue(is_voice_command('切回文字'))
            self.assertFalse(is_voice_command('请解释 /voice off'))

    async def test_failed_save_keeps_live_mode(self):
        self.enable()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'config.json').write_text('{}')
            with patch('qq_chat.voice_mode.atomic_json', side_effect=OSError):
                await command(self.bridge, self.owner, '/voice on', root)
            self.assertFalse(self.bridge.voice_only(self.owner))
            self.assertIn('保存失败', self.sent[-1][1])


class PrivateSpeakTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixtures.GroupSpeakTests.asyncSetUp
    asyncTearDown = fixtures.GroupSpeakTests.asyncTearDown
    setup_tools = fixtures.GroupSpeakTests.setup_tools
    full_codex = fixtures.GroupSpeakTests.full_codex
    enable_voice = fixtures.GroupSpeakTests.enable_voice
    voice_only = fixtures.GroupSpeakTests.voice_only
    speak_during_turn = fixtures.GroupSpeakTests.speak_during_turn

    async def test_private_speak_not_duplicated(self):
        server = await self.voice_only()
        self.cfg['voice']['private_voice_only'] = True
        self.speak_during_turn(server, {'text': LINE, 'style': '开心'})
        source = Source('private', self.cfg['owner_qq'], self.cfg['owner_qq'])
        reply, streamed = await self.bridge.chat(source, '你好')
        self.assertIn('每轮都调用 speak', self.front.requests[-1].runtime_instructions)
        await self.bridge.deliver(source, reply, '你好', streamed=streamed)
        await asyncio.wait_for(self.bridge.voice.queue.join(), 2)
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(self.audio, [source])
        self.assertEqual(self.sent, [])


class PrivateVoiceControlSessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_exit_bypasses_busy_worker_and_rejects_guest(self):
        from unittest.mock import AsyncMock
        from qq_chat.bridge import Bridge
        from tests.test_front_switch import config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cfg = config()
            cfg['voice'] = {'private_voice_only': True, 'enabled': False}
            cfg['private_whitelist'] = [456]
            path = root / 'config.json'
            path.write_text(json.dumps(cfg))
            with patch('qq_chat.bridge.BASE', root):
                bridge = Bridge(cfg)
            bridge.send = AsyncMock()
            entered, busy, events = asyncio.Event(), asyncio.Event(), asyncio.Queue()
            async def worker():
                entered.set()
                await busy.wait()
            bridge.worker = worker
            class Socket:
                def __aiter__(self): return self
                async def __anext__(self): return await events.get()
            def event(number, user):
                return json.dumps({'post_type': 'message', 'message_type': 'private',
                                   'self_id': 999, 'user_id': user, 'message_id': number,
                                   'message': [{'type': 'text', 'data': {'text': '切回文字'}}]})
            with patch('qq_chat.bridge.BASE', root):
                task = asyncio.create_task(bridge.session(Socket()))
                try:
                    await entered.wait()
                    for count, user in enumerate((456, 123), 1):
                        await events.put(event(count, user))
                        async with asyncio.timeout(2):
                            while bridge.send.await_count < count:
                                await asyncio.sleep(.001)
                        enabled = json.loads(path.read_text())['voice']['private_voice_only']
                        self.assertEqual(enabled, user != 123)
                    self.assertFalse(bridge.cfg['voice']['private_voice_only'])
                    self.assertFalse(busy.is_set())
                    self.assertEqual(bridge.queue.qsize(), 0)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    await bridge.tasks.close()
                    bridge.db.close()

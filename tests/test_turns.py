"""One conversation at a time: serial turns, /stop for the running turn, and commands that must not queue."""
import asyncio
import json
import unittest
from unittest.mock import patch

from qq_chat.agents.base import Interrupted
from qq_chat.agents.config import settings
from qq_chat.bridge import Incoming
from tests.support import BridgeCase


class TurnTests(BridgeCase):
    async def test_same_conversation_turns_are_serial(self):
        self.front.release.clear()
        first = asyncio.create_task(self.bridge.chat(self.owner, 'first'))
        await self.front.entered.wait()
        second = asyncio.create_task(self.bridge.chat(self.owner, 'second'))
        try:
            await asyncio.sleep(.01)
            self.assertEqual(len(self.front.requests), 1)
            self.front.release.set()
            await asyncio.wait_for(asyncio.gather(first, second), 2)
            self.assertEqual(len(self.front.requests), 2)
        finally:
            first.cancel(); second.cancel()
            await asyncio.gather(first, second, return_exceptions=True)

    async def test_owner_turn_is_full_and_carries_private_rules(self):
        await self.bridge.chat(self.owner, 'hello')
        req = self.front.requests[-1]
        self.assertEqual(req.level, 'full')
        self.assertIn('owner private data', req.instructions)
        await self.bridge.chat(self.guest, 'hello')
        req = self.front.requests[-1]
        self.assertEqual(req.level, 'none')
        self.assertNotIn('owner private data', req.instructions)

    async def test_cancel_worker_while_waiting_for_the_lock_does_not_leak_queue_count(self):
        started = asyncio.Event()

        async def hold():
            async with self.bridge.conversation_lock(self.owner):
                started.set()
                await asyncio.Future()
        holder = asyncio.create_task(hold())
        await started.wait()
        worker = asyncio.create_task(self.bridge.worker())
        await self.bridge.queue.put(Incoming(self.owner, '/new'))
        await self.until(lambda: self.bridge.queue.empty())
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        try:
            await asyncio.wait_for(self.bridge.queue.join(), 1)
        finally:
            holder.cancel()
            await asyncio.gather(holder, return_exceptions=True)

    async def test_new_waits_for_the_running_turn(self):
        self.front.release.clear()
        turn = asyncio.create_task(self.bridge.chat(self.owner, 'hello'))
        await self.front.entered.wait()
        worker = asyncio.create_task(self.bridge.worker())
        try:
            await self.bridge.queue.put(Incoming(self.owner, '/new'))
            await asyncio.sleep(.01)
            self.assertIsNotNone(self.front.session_id(self.bridge.conversation(self.owner)))
            self.assertFalse(any('已清空' in text for _, text in self.sent))
            self.front.release.set()
            await asyncio.wait_for(self.bridge.queue.join(), 2)
            await turn
            self.assertIsNone(self.front.session_id(self.bridge.conversation(self.owner)))
            self.assertEqual(self.sent[-1][1], '已清空聊天上下文，可以开始新话题。')
        finally:
            worker.cancel()
            await asyncio.gather(worker, return_exceptions=True)

    async def test_stop_distinguishes_completed_and_failed_outcomes(self):
        for fail in (False, True):
            self.front.entered.clear(); self.front.release.clear(); self.front.error = None

            async def interrupt(key):
                if fail:
                    self.front.error = RuntimeError('front failed')
                self.front.release.set()
                return True
            self.front.interrupt = interrupt
            turn = asyncio.create_task(self.bridge.chat(self.owner, 'hello'))
            await self.front.entered.wait()
            await self.bridge.stop_command(self.owner, '/stop')
            await asyncio.gather(turn, return_exceptions=True)
            expected = '停止前已失败' if fail else '停止前已完成'
            self.assertTrue(any(expected in text for _, text in self.sent))
        self.front.error = None

    async def test_stop_timeout_does_not_release_conversation(self):
        self.front.release.clear()

        async def interrupt(key): return True
        self.front.interrupt = interrupt
        turn = asyncio.create_task(self.bridge.chat(self.owner, 'hello'))
        await self.front.entered.wait()
        await self.bridge.stop_command(self.owner, '/stop')
        token = self.bridge._front_tokens[self.bridge.conversation(self.owner)]
        try:
            with patch('qq_chat.turns.asyncio.sleep', return_value=None):
                await self.bridge.front_stop_watchdog(self.owner, token)
            self.assertFalse(turn.done())
            self.assertTrue(any('停止尚未确认' in text for _, text in self.sent))
        finally:
            self.front.release.set()
            await turn

    async def test_stop_without_a_running_turn_rejected_delivery_and_extra_arguments(self):
        await self.bridge.stop_command(self.owner, '/stop')
        self.assertIn('没有正在执行', self.sent[-1][1])
        for text in ('/stop 3', '/stop all'):
            await self.bridge.stop_command(self.owner, text)
            self.assertIn('用法：/stop', self.sent[-1][1])
        self.front.release.clear()
        turn = asyncio.create_task(self.bridge.chat(self.owner, 'hello'))
        await self.front.entered.wait()
        try:
            await self.bridge.stop_command(self.owner, '/stop')  # FakeAgent has no interrupt: not delivered
            self.assertIn('停止失败', self.sent[-1][1])
            self.assertFalse(turn.done())
        finally:
            self.front.release.set()
            await turn

    async def test_stop_and_removed_task_commands_from_guests(self):
        for command in ('/stop', '/stop all'):
            await self.worker_command(command, self.guest)
        self.assertEqual((self.front.requests, self.sent), ([], []))
        for command in ('/task goal', '/tasks'):
            await self.worker_command(command, self.guest)
            self.assertIn('仅限主人', self.sent[-1][1])
        self.assertEqual(self.front.requests, [])

    async def test_stop_bypasses_the_busy_worker(self):
        self.front.release.clear()
        events = asyncio.Queue()

        class Socket:
            def __aiter__(self): return self
            async def __anext__(self): return await events.get()

        def event(ident, text, user=10001):
            return json.dumps({'post_type': 'message', 'message_type': 'private', 'self_id': 40001,
                               'user_id': user, 'message_id': ident, 'message': [{'type': 'text', 'data': {'text': text}}]})

        async def interrupt(key):
            self.front.error = Interrupted('front ended'); self.front.release.set()
            return True
        self.front.interrupt = interrupt
        session = asyncio.create_task(self.bridge.session(Socket()))
        try:
            await events.put(event(1, 'hello'))
            await self.front.entered.wait()
            await events.put(event(2, '/stop'))
            await self.until(lambda: any('已确认停止' in text for _, text in self.sent))
            self.assertEqual(self.bridge.queue.qsize(), 0)
        finally:
            session.cancel()
            await asyncio.gather(session, return_exceptions=True)


class RemovedSeparationConfigTests(unittest.TestCase):
    def test_a_config_that_still_names_a_background_agent_does_not_start(self):
        base = {'agents': {'front': 'work', 'back': ['work'], 'profiles': {
            'work': {'type': 'codex_app', 'path': 'unused', 'writes': True}}}}
        with self.assertRaisesRegex(ValueError, 'agents.back is no longer supported'):
            settings(base)
        base['agents']['back'] = []  # the live config still carries the empty list
        self.assertEqual(settings(base)[0], 'work')

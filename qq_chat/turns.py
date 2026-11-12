"""One conversation at a time: the per-chat lock, stopping the running turn, and delivering a reply."""
import asyncio
from collections import defaultdict
import contextlib
from contextlib import asynccontextmanager
from functools import wraps

from .agents.base import Interrupted


def serialized(method):
    @wraps(method)
    async def wrapped(self, source, *args, **kwargs):
        async with self.conversation_lock(source):
            return await method(self, source, *args, **kwargs)
    return wrapped


class Turns:
    @asynccontextmanager
    async def conversation_lock(self, source):
        key = self.conversation(source)
        locks = self.__dict__.setdefault('locks', defaultdict(asyncio.Lock))
        owners = self.__dict__.setdefault('_lock_owners', {})
        task = asyncio.current_task()
        if owners.get(key) is task:
            yield
            return
        async with locks[key]:
            owners[key] = task
            try:
                yield
            finally:
                owners.pop(key, None)

    def background(self, coro):
        """Run a short helper beside the current command; the set keeps it from being garbage-collected."""
        helpers = self.__dict__.setdefault('_background', set())
        task = asyncio.create_task(coro)
        helpers.add(task)
        task.add_done_callback(helpers.discard)
        return task

    @asynccontextmanager
    async def front_execution(self, source, agent):
        key = self.conversation(source)
        active = self.__dict__.setdefault('_active_front', {})
        tokens = self.__dict__.setdefault('_front_tokens', {})
        stops = self.__dict__.setdefault('_front_stops', {})
        token = object()
        active[key], tokens[key] = agent, token
        try:
            yield
        except Interrupted:
            with contextlib.suppress(Exception):
                await self.send(source, '当前前台轮次已确认停止。')
            raise
        except Exception:
            if stops.get(key) is token:
                with contextlib.suppress(Exception):
                    await self.send(source, '当前前台轮次停止前已失败。')
            raise
        else:
            if stops.get(key) is token:
                with contextlib.suppress(Exception):
                    await self.send(source, '当前前台轮次停止前已完成。')
        finally:
            active.pop(key, None)
            tokens.pop(key, None)
            stops.pop(key, None)

    async def front_stop_watchdog(self, source, token):
        await asyncio.sleep(30)
        if self.__dict__.get('_front_stops', {}).get(self.conversation(source)) is token:
            with contextlib.suppress(Exception):
                await self.send(source, '前台停止尚未确认，仍在等待执行端；当前会话轮次尚未释放。')

    async def stop_front(self, source):
        """/stop without arguments: ask the agent to end the turn that is running in this chat."""
        key = self.conversation(source)
        active = self.__dict__.setdefault('_active_front', {}).get(key)
        if active is None:
            await self.send(source, '当前会话没有正在执行的前台轮次。')
            return
        token = self._front_tokens[key]
        self.__dict__.setdefault('_front_stops', {})[key] = token
        self.background(self.front_stop_watchdog(source, token))
        await self.send(source, '已请求停止当前前台轮次，等待执行端确认。')
        try:
            delivered = await active.interrupt(key)
        except Exception:
            delivered = False
        if not delivered and self._front_stops.get(key) is token:
            self._front_stops.pop(key, None)
            await self.send(source, '停止失败：执行端未接收请求，可能已经结束或尚未开始。')

    async def deliver(self, source, text, user_text, *, streamed=False):
        key = self.conversation(source)
        spoken = self.__dict__.setdefault('_spoken_turns', set())
        spoke = key in spoken
        spoken.discard(key)
        if self.voice_only(source):
            # speak already voiced this turn; otherwise read the reply aloud in the default tone.
            if not spoke and not self.voice.submit_only(source, text):
                await self.send(source, text)
        else:
            if not streamed:
                await self.send(source, text)
            await self.enqueue_voice(source, text)
        saved = self.__dict__.setdefault('_history_replies', {}).pop(key, text)
        self.db.executemany('INSERT INTO history(role,content,conversation) VALUES(?,?,?)',
                            [('user', user_text, key), ('assistant', saved, key)])
        self.db.commit()

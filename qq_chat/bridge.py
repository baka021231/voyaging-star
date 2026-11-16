"""QQ chat bridge: NapCat (OneBot 11) on one side, the agents on the other.

This file is the orchestration: who may do what, how one turn is assembled and run, and the two loops
(session() receives, worker() answers). The parts with a job of their own live next door and Bridge
inherits them: onebot (sending), store (database, group context), media (images, attachments),
commands (owner commands), turns (per-chat lock, /stop, deliver), bili (/watch) and bili_wake (her own time there).
"""
import asyncio
import contextlib
from dataclasses import replace
import json
import logging
import shlex
from pathlib import Path

from . import prompts
from .agents import FULL, NONE, TurnRequest, build_agents
from .agents.base import MissingSession, Interrupted
from .agents.config import legacy_codex_profile, settings
from .agents.state import AgentState
from .bili import Bili
from .bili_wake import BiliWake
from .commands import Commands
from .front_switch import command as switch_front_command
from .media import Media
from .message_references import expand_references, reference_prompt
# Keep these names available from bridge for existing callers.
from .message_policy import (
    Source, Incoming, owner_request, conversation_key, image_request,
    speaker_message, incoming_text,
)
from .moderation import BLOCK_SENDER, blocked_group_sender, bounded_guest_reply, obvious_abuse, update_blacklist
from .onebot import OneBot
from .reply import stream_bubbles
from .store import Store, open_database
from .tool_server import SPEAK, ToolServer
from .turns import Turns, serialized
from .voice import ProcessSynthesizer, VoiceReplies, conversation_name
from .voice_log import VoiceLog
from .voice_mode import command as voice_mode_command, is_voice_command

from websockets.asyncio.client import connect

BASE = Path(__file__).resolve().parents[1]
CONFIG = BASE / "config.json"
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


class Bridge(Turns, OneBot, Store, Media, Commands, Bili, BiliWake):
    def blocked(self, source):
        return blocked_group_sender(source, self.cfg)

    def block_user(self, user, *, blocked=True):
        users = update_blacklist(BASE / 'config.json', user, self.cfg['owner_qq'], blocked=blocked)
        self.cfg['group_user_blacklist'] = users

    def auto_block(self, source):
        """Only the event's real sender can be auto-blocked; owners are exempt."""
        if source.kind != 'group' or owner_request(source, self.cfg['owner_qq']):
            return
        try:
            self.block_user(source.sender_id)
        except (OSError, ValueError, TypeError):
            # Stay silent even if persistence fails; still block until the next restart.
            users = {str(user) for user in self.cfg.get('group_user_blacklist', [])}
            users.add(str(source.sender_id))
            self.cfg['group_user_blacklist'] = sorted(users)
            logging.error('Group blacklist persistence failed; sender blocked in memory')

    def conversation(self, source):
        return conversation_key(source, self.cfg['owner_qq'])

    def __init__(self, cfg):
        self.cfg = cfg
        self.ws = None
        self.pending = {}
        self.send_lock = asyncio.Lock()
        self.last_send = 0.0
        self.queue = asyncio.Queue(maxsize=8)
        voice = cfg.get('voice') or {}
        if voice.get('mode', 'reply') not in ('reply', 'tool'):
            raise ValueError('voice.mode must be reply or tool')
        self.voice = None
        if voice.get('enabled', False):
            self.voice = VoiceReplies(
                ProcessSynthesizer(voice['command'], timeout=float(voice.get('timeout_seconds', 120))),
                self.send_audio, self.send, self.blocked,
                record=VoiceLog(voice['debug_log']) if voice.get('debug_log') else None,
                conversation=conversation_name)
        self.db = open_database(BASE / 'chat.sqlite3', cfg['owner_qq'])
        _, _, profiles = settings(cfg)
        self._agent_state = AgentState(self.db, legacy_codex_profile(cfg),
                                       {name: profile['type'] for name, profile in profiles.items()})
        self.agents = build_agents(cfg, self.db, BASE / "workspace")
        self._history_replies = {}
        self.tool_server = ToolServer(self, BASE)
        self._memory_grants = {}
        self._active_front = {}

    @property
    def agent(self):
        return self.agents[self.cfg.get('agents', {}).get('front', 'codex')]

    def agent_for(self, source):
        config = self.cfg.get('agents', {})
        front = config.get('front', 'codex')
        guest = config.get('guest', front)
        if source.kind == 'group':
            guest = config.get('group_guest', guest)
        name = front if owner_request(source, self.cfg['owner_qq']) else guest
        return self.agents[name]

    @property
    def state(self):
        if '_agent_state' not in self.__dict__:
            self._agent_state = AgentState(self.db, legacy_codex_profile(self.cfg))
        return self._agent_state

    def selection(self, source):
        return self.state.selection(self.conversation(source), self.agent_for(source).name)

    def save_selection(self, source, model, effort):
        self.state.save(self.conversation(source), self.agent_for(source).name, model, effort)

    def speak_enabled(self):
        """tool mode: the agent decides when to speak; replies are not voiced automatically."""
        return (getattr(self, 'voice', None) is not None
                and (self.cfg.get('voice') or {}).get('mode', 'reply') == 'tool')

    def group_speak_enabled(self, source):
        # Voice-only groups speak through the tool too, so the agent picks the tone of every reply.
        return (source.kind == 'group' and self.speak_enabled()
                and ((self.cfg.get('voice') or {}).get('group_enabled', False) is True
                     or self.group_voice_only(source)))

    def group_voice_only(self, source):
        return (source.kind == 'group' and getattr(self, 'voice', None) is not None
                and (self.cfg.get('voice') or {}).get('group_voice_only', False) is True)

    def voice_only(self, source):
        return (self.group_voice_only(source)
                or (source.kind == 'private' and owner_request(source, self.cfg['owner_qq'])
                    and getattr(self, 'voice', None) is not None
                    and (self.cfg.get('voice') or {}).get('private_voice_only', False) is True))

    async def voice_command(self, source, text):
        await voice_mode_command(self, source, text, BASE)

    async def enqueue_voice(self, source, reply):
        voice = getattr(self, 'voice', None)
        if voice is None or self.speak_enabled():
            return
        if not voice.submit(source, reply):
            await self.send(source, '语音队列已满，本次仅发送文字。')

    async def instructions(self, authorized, *, group_moderation=True):
        sections = []
        for name in (('SOUL.md', 'OWNER.md') if authorized else ('SOUL.md',)):
            rules_path = BASE / 'workspace' / name
            if rules_path.is_file():
                content = await asyncio.to_thread(rules_path.read_text, encoding='utf-8')
                sections.append(name + '\n' + content)
                logging.info('QQ workspace %s loaded for current turn', name)
        rules = '\n\n'.join(sections) if sections else None
        return prompts.instructions(
            authorized, rules, group_moderation=group_moderation,
            guest_speech=not authorized and group_moderation and self.group_speak_enabled(Source('group', 0)))

    async def turn_request(self, source, authorized, text=''):
        selection = self.selection(source)
        profile = getattr(self.agent_for(source), 'profile', {})
        request = TurnRequest(
            key=self.conversation(source), text=text, level=FULL if authorized else NONE,
            instructions=await self.instructions(authorized, group_moderation=source.kind == 'group'),
            model=selection[0] if selection else profile.get('model'),
            effort=selection[1] if selection else profile.get('effort'))
        request.runtime_instructions = prompts.NO_MEMORY_GRANT
        if source.kind == 'group' and not authorized:
            request.instructions += prompts.GROUP_IMAGES
        grant = self.__dict__.get('_memory_grants', {}).get(request.key)
        if authorized and grant is not None and 'memory_search' in grant.names:
            memory = prompts.memory_tool(shlex.join([
                'python3', '-I', str(BASE / 'tools' / 'memory_search.py'), '--grant', grant.config_path, '--query']))
            request.instructions += memory
            request.runtime_instructions = prompts.MEMORY_THIS_TURN + memory
        speak_granted = ((authorized or self.group_speak_enabled(source))
                         and grant is not None and SPEAK in grant.names)
        if speak_granted:
            if not authorized and profile.get('type') == 'codex_app':
                # Guests on Codex get speak as a native tool instead of a terminal command.
                request.dynamic_tools, request.on_tool_call = self.tool_server.native_binding(grant)
                speak = prompts.speak_tool(None, guest=True)
            else:
                speak = prompts.speak_tool(shlex.join([
                    'python3', '-I', str(BASE / 'tools' / 'speak.py'), '--grant', grant.config_path]),
                    guest=not authorized)
            request.instructions += speak
            request.runtime_instructions += prompts.SPEAK_THIS_TURN + speak
        if self.voice_only(source):
            request.runtime_instructions += prompts.VOICE_ONLY_SPEAK if speak_granted else prompts.VOICE_ONLY_AUTO
        if source.kind == 'private' and authorized:
            request.runtime_instructions += prompts.PRIVATE_VOICE_SWITCH
        if authorized:
            request.runtime_instructions += self.watch_status() + self.bili_recent(BASE, request.key)
        # The bridge never told her the time, so in private turns she often ran `date` before speaking.
        request.runtime_instructions = prompts.current_time() + '\n' + request.runtime_instructions
        return request

    async def thread_for(self, source, authorized):
        return await self.agent_for(source).prepare(await self.turn_request(source, authorized))

    async def relay_progress(self, source, queue):
        while True:
            first = await queue.get()
            if first is None:
                return
            await asyncio.sleep(1)
            batch = [first]
            done = False
            while not queue.empty():
                message = queue.get_nowait()
                if message is None:
                    done = True
                    break
                if message != batch[-1]:
                    batch.append(message)
            try:
                await self.send(source, '【进度】\n' + '\n'.join(batch))
            except Exception:
                logging.warning('Progress delivery failed')
            if done:
                return

    async def relay_answer(self, source, queue):
        async for message in stream_bubbles(queue):
            await self.send(source, message)

    async def run_turn(self, source, thread, text, authorized, image_paths, *, request=None):
        """Run one agent turn; keep progress and QQ streaming policy here."""
        progress_queue = asyncio.Queue(maxsize=128)

        def progress(message):
            if not progress_queue.full():
                progress_queue.put_nowait(message)

        answer_queue = asyncio.Queue()

        def answer_delta(delta):
            if delta:
                answer_queue.put_nowait(delta)

        stream_text = authorized and not self.voice_only(source)
        reporter = asyncio.create_task(self.relay_progress(source, progress_queue)) if stream_text else None
        answer_sender = asyncio.create_task(self.relay_answer(source, answer_queue)) if stream_text else None
        selection = self.selection(source)
        if request is None:
            request = await self.turn_request(source, authorized, text)
        request.text, request.session_id = text, thread
        profile = getattr(self.agent_for(source), 'profile', {})
        request.model = selection[0] if selection else profile.get('model')
        request.effort = selection[1] if selection else profile.get('effort')
        if (stream_text and profile.get('type') == 'codex_app'
                and self.__dict__.get('_memory_grants', {}).get(request.key) is not None):
            from .memory_progress import MemoryProgress
            quiet_memory = MemoryProgress(progress)
            request.on_progress = quiet_memory.progress
            request.on_activity = quiet_memory.activity
        else:
            request.on_progress = progress if stream_text else None
        request.on_answer = answer_delta if stream_text else None
        request.show_tools = bool(self.cfg.get('show_tool_progress', False))
        request.images = tuple(image_paths)
        if image_paths and authorized:
            request.runtime_instructions += prompts.image_files(image_paths)
        try:
            reply = await self.agent_for(source).run(request)
            if answer_sender:
                answer_queue.put_nowait(None)
                await asyncio.wait_for(asyncio.shield(answer_sender), 120)
        finally:
            if answer_sender:
                answer_sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await answer_sender
            if reporter:
                try:
                    await asyncio.wait_for(progress_queue.put(None), 1)
                    await asyncio.wait_for(asyncio.shield(reporter), 5)
                except asyncio.TimeoutError:
                    reporter.cancel()
                finally:
                    reporter.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await reporter
        return reply

    @serialized
    async def chat(self, source, text, context_id=None, image_paths=()):
        agent = self.agent_for(source)
        server = getattr(self, 'tool_server', None)
        names = [name for name, on in (('memory_search', self.cfg.get('memory', {}).get('enabled', True)),
                                       (SPEAK, self.speak_enabled())) if on]
        owner = owner_request(source, self.cfg['owner_qq'])
        if not owner:
            names = [SPEAK] if self.group_speak_enabled(source) else []
        if names and server and server.server is not None:
            async with server.grant(source, names=names) as grant:
                key = self.conversation(source)
                self._memory_grants[key] = grant
                try:
                    async with self.front_execution(source, agent):
                        result = await self._chat(source, text, context_id, image_paths)
                    if self.voice_only(source) and server.speech_counts.get(grant.token):
                        # Already said in her chosen tone; deliver() must not read the reply again.
                        self.__dict__.setdefault('_spoken_turns', set()).add(key)
                    return result
                finally:
                    self._memory_grants.pop(key, None)
        async with self.front_execution(source, agent):
            return await self._chat(source, text, context_id, image_paths)

    async def _chat(self, source, text, context_id=None, image_paths=()):
        authorized = owner_request(source, self.cfg['owner_qq'])
        key = self.conversation(source)
        self.__dict__.setdefault('_history_replies', {}).pop(key, None)
        agent = self.agent_for(source)
        for attempt in range(2):
            request = await self.turn_request(source, authorized)
            thread = await agent.prepare(request)
            turn_text = self.turn_text(source, thread, text, context_id)
            try:
                reply = await self.run_turn(source, thread, turn_text, authorized, image_paths, request=request)
                break
            except MissingSession:
                if attempt:
                    raise
                # The CLI confirms that no prior session ran. Recompose bounded history.
                await agent.reset(key)
                self.db.commit()
        if authorized:
            self.bili_recent_told(BASE, key)
        self.db.execute("INSERT OR IGNORE INTO bootstrap VALUES (?)", (thread,))
        self.advance_group_context(source, thread, context_id)
        self.db.commit()
        if authorized and request.history_text is not None:
            self._history_replies[key] = request.history_text
        return reply, request.on_answer is not None

    async def front_command(self, source, text):
        await switch_front_command(self, source, text, BASE,
                                   authorized=owner_request(source, self.cfg['owner_qq']))

    async def worker(self):
        while True:
            incoming = await self.queue.get()
            source, text = incoming.source, incoming.text
            guard = self.conversation_lock(source)
            acquired = False
            try:
                await guard.__aenter__()
                acquired = True
                head = text.split(maxsplit=1)[0].casefold() if text.strip() else ''
                if is_voice_command(text):
                    await self.voice_command(source, text)
                    continue
                if head == '/stop':
                    await self.stop_command(source, text)
                    continue
                if head == '/front':
                    await self.front_command(source, text)
                    continue
                if self.blocked(source):
                    continue
                if source.kind == 'group' and not owner_request(source, self.cfg['owner_qq']) and obvious_abuse(text):
                    self.auto_block(source)
                    continue
                # Management commands bypass the model, so authorize them here.
                # Deny unknown slash commands too, to keep future additions closed by default.
                if (text.startswith('/') and text.split(maxsplit=1)[0].casefold() not in ('/ping', '/help')
                        and not owner_request(source, self.cfg['owner_qq'])):
                    await self.send(source, '这类管理命令仅限主人使用；你可以继续聊天，或使用 /ping、/help。')
                    continue
                if text.split(maxsplit=1)[:1] in (['/model'], ['/models'], ['/effort']):
                    await self.model_commands(source, text)
                    continue
                if head == '/watch':
                    await self.watch_command(source, text, BASE)
                    continue
                reference_records, reference_images = [], ()
                if incoming.references:
                    reference_records, reference_images = await expand_references(
                        self.onebot_call, source, incoming.references,
                        allow_images=image_request(source, self.cfg['owner_qq']),
                        origins=self.__dict__.get('_message_origins', {}),
                        image_limit=max(0, 5 - len(incoming.images)))
                if incoming.attachments:
                    try:
                        text = await self.receive_attachments(incoming)
                    except (ValueError, OSError, asyncio.TimeoutError) as error:
                        logging.warning('Attachment reception failed: %s', type(error).__name__)
                        await self.send(source, '附件接收失败：' + (str(error) if isinstance(error, ValueError) else '下载或保存失败，请重新发送。'))
                        continue
                image_paths = ()
                if incoming.images:
                    try:
                        image_paths = tuple(await self.receive_images(incoming))
                    except (ValueError, OSError, asyncio.TimeoutError) as error:
                        logging.warning('Image reception failed: %s', type(error).__name__)
                        await self.send(source, '图片接收失败：' + (str(error) if isinstance(error, ValueError) else '下载或保存失败，请重新发送。'))
                        continue
                # Only the pictures of this message are marked in her words; those inside a quoted or
                # forwarded message are already noted in the reference records.
                text = prompts.with_images(text, len(image_paths))
                if reference_images:
                    image_paths += await self.receive_reference_images(incoming, reference_images, reference_records)
                if await self.plain_command(source, text, incoming.context_id):
                    continue
                if len(text) > 6000:
                    await self.send(source, "这条消息较长，请拆成每条不超过6000字。")
                    continue
                if reference_records and not text:
                    text = '请结合随本条消息的引用或合并转发内容自然回应。'
                text = speaker_message(source, text, self.cfg['owner_qq'])
                if reference_records:
                    text = reference_prompt(reference_records) + text
                reply, streamed = await self.chat(source, text, incoming.context_id, image_paths=image_paths)
                if source.kind == 'group' and not owner_request(source, self.cfg['owner_qq']):
                    if reply.strip() == BLOCK_SENDER:
                        self.auto_block(source)
                        continue
                    reply = bounded_guest_reply(reply)
                await self.deliver(source, reply, text, streamed=streamed)
                logging.info("Chat reply delivered")
            except Interrupted:
                pass  # front_execution already reports the confirmed outcome.
            except Exception as error:
                logging.exception("Chat delivery failed")
                with contextlib.suppress(Exception):
                    await self.send(source, "这次回复未能完成，请稍后重试；可先发送 /ping 检查 QQ 连接。")
            finally:
                if acquired:
                    await guard.__aexit__(None, None, None)
                self.queue.task_done()

    async def session(self, ws):
        self.ws = ws
        if self.voice is not None and (self.cfg.get('voice') or {}).get('preload', True):
            # Disconnects kill the speech process, so load it again on every connection.
            self.voice.warm()
        worker = asyncio.create_task(self.worker())
        controls = set()
        try:
            async for payload in ws:
                event = json.loads(payload)
                if event.get("echo") in self.pending:
                    future = self.pending[event["echo"]]
                    if not future.done():
                        future.set_result(event)
                    continue
                incoming = incoming_text(event, self.cfg["owner_qq"], self.cfg.get("allowed_groups", []),
                                         allow_all_groups=self.cfg.get('allow_all_groups', True), require_trigger=False,
                                         group_user_blacklist=self.cfg.get('group_user_blacklist', []),
                                         private_whitelist=self.cfg.get('private_whitelist', []))
                if incoming is None:
                    continue
                message_id = event.get("message_id")
                if message_id is None:
                    continue
                self.remember_message_origin(message_id, incoming.source)
                if not self.first_delivery(str(event.get("self_id")) + ":" + str(message_id)):
                    continue
                if incoming.source.kind == 'group':
                    triggered = incoming_text(event, self.cfg['owner_qq'], self.cfg.get('allowed_groups', []),
                                              allow_all_groups=self.cfg.get('allow_all_groups', True),
                                              group_user_blacklist=self.cfg.get('group_user_blacklist', []))
                    if triggered and not owner_request(incoming.source, self.cfg['owner_qq']) and obvious_abuse(incoming.text):
                        self.auto_block(incoming.source)
                        continue
                    context_id = self.remember_group(incoming.source, incoming.text or '[引用或合并转发消息]')
                    if triggered is None:
                        continue
                    incoming = replace(incoming, context_id=context_id)
                head = incoming.text.split(maxsplit=1)[0].casefold() if incoming.text.strip() else ''
                if is_voice_command(incoming.text):
                    control = asyncio.create_task(self.voice_command(incoming.source, incoming.text))
                    controls.add(control)
                    control.add_done_callback(controls.discard)
                    continue
                if head == '/front':
                    control = asyncio.create_task(self.front_command(incoming.source, incoming.text))
                    controls.add(control)
                    control.add_done_callback(controls.discard)
                    continue
                if head == '/watch' and owner_request(incoming.source, self.cfg['owner_qq']):
                    # Not queued: it only starts or stops a background watch, so it works while she is busy.
                    control = asyncio.create_task(self.watch_command(incoming.source, incoming.text, BASE))
                    controls.add(control)
                    control.add_done_callback(controls.discard)
                    continue
                if head == '/stop':
                    if owner_request(incoming.source, self.cfg['owner_qq']):
                        control = asyncio.create_task(self.stop_command(incoming.source, incoming.text))
                        controls.add(control)
                        control.add_done_callback(controls.discard)
                    continue
                try:
                    self.queue.put_nowait(incoming)
                except asyncio.QueueFull:
                    logging.warning("Queue full; message skipped")
                    continue
                logging.info("Allowed %s message queued", incoming.source.kind)
        finally:
            for control in controls:
                control.cancel()
            await asyncio.gather(*controls, return_exceptions=True)
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            if self.voice is not None:
                await self.voice.close()
            for future in self.pending.values():
                if not future.done():
                    future.cancel()
            self.pending.clear()
            self.ws = None

    async def run(self):
        front, guest, _ = settings(self.cfg)
        config = self.cfg.get('agents', {})
        for name in dict.fromkeys((front, guest, config.get('group_guest', guest))):
            await self.agents[name].start()
        if self.cfg.get('memory', {}).get('enabled', True) or self.speak_enabled():
            await self.tool_server.start()
        logging.info("App Server initialized")
        # Her own time on Bilibili; does nothing unless bili.wake is configured.
        self.background(self.wake_loop(BASE))
        while True:
            try:
                async with connect(self.cfg["ws_url"], additional_headers={
                    "Authorization": "Bearer " + self.cfg["onebot_token"]},
                    max_size=2 ** 20, proxy=None) as ws:
                    logging.info("Connected to NapCat")
                    await self.session(ws)
            except Exception as error:
                logging.warning("Connection unavailable (%s); retrying", type(error).__name__)
                await asyncio.sleep(5)


def main():
    config = json.loads(CONFIG.read_text())
    if not str(config.get("owner_qq", "")).isdigit():
        raise SystemExit("Set owner_qq before starting the bridge")
    (BASE / "workspace").mkdir(exist_ok=True)
    asyncio.run(Bridge(config).run())

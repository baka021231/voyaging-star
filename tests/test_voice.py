import asyncio
import base64
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
import wave

from qq_chat.bridge import Bridge, Incoming, Source
from qq_chat.voice import (ProcessSynthesizer, VoiceReplies, SPEAK_MAX_CHARS, conversation_name, speech_text,
                            speech_chunks, validate_wav)


def wav_bytes():
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * 3200)
    return output.getvalue()


class SpeechTextTests(unittest.TestCase):
    def test_prose_code_and_links(self):
        result = speech_text('你好~\n```python\nsecret = 1\n```\n[文档](https://example.com/a) **结束**')
        self.assertNotIn('secret', result)
        self.assertNotIn('http', result)
        self.assertIn('代码见文字消息', result)
        self.assertIn('文档', result)
        text = '你好，这是测试。' * 100
        parts = list(speech_chunks(text))
        self.assertEqual(''.join(parts), text)
        self.assertTrue(all(len(p) <= SPEAK_MAX_CHARS for p in parts))

    def test_audio_validation_rejects_truncated_and_wrong_format(self):
        validate_wav(wav_bytes())
        for data in (b'bad', wav_bytes()[:-2], b'x' * 100):
            with self.assertRaises((ValueError, wave.Error, EOFError)):
                validate_wav(data)


class ProcessTests(unittest.IsolatedAsyncioTestCase):
    async def test_stderr_is_preserved_without_blocking_warmup_or_protocol(self):
        encoded = base64.b64encode(wav_bytes()).decode()
        script = ('import sys,json\n'
                  'for line in sys.stdin:\n'
                  ' request=json.loads(line)\n'
                  ' sys.stderr.write("diagnostic:" + "x" * 200000 + "\\n"); sys.stderr.flush()\n'
                  f' reply={{"ready":True}} if request.get("warmup") else {{"wav":{encoded!r}}}\n'
                  ' print(json.dumps(reply),flush=True)\n')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'private/backend.log'
            backend = ProcessSynthesizer([sys.executable, '-u', '-c', script], timeout=5,
                                         stderr_log=path)
            try:
                self.assertTrue(await backend.warm())
                process = backend.process
                self.assertEqual(await backend.synthesize('你好'), wav_bytes())
                self.assertIs(backend.process, process)
            finally:
                await backend.close()
            self.assertEqual(path.read_text().count('diagnostic:'), 2)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            # A restarted child appends rather than erasing earlier diagnostics.
            try:
                self.assertEqual(await backend.synthesize('再来一句'), wav_bytes())
            finally:
                await backend.close()
            self.assertEqual(path.read_text().count('diagnostic:'), 3)

    async def test_direct_synthesis_keeps_diagnostics_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'backend.log'
            script = 'import sys; print("backend failure",file=sys.stderr,flush=True); print("{}",flush=True)'
            backend = ProcessSynthesizer([sys.executable, '-u', '-c', script], timeout=5,
                                         stderr_log=path)
            with self.assertRaises(KeyError):
                await backend.synthesize('失败')
            self.assertIsNone(backend.process)
            self.assertIn('backend failure', path.read_text())

    async def test_persistent_protocol_and_cleanup(self):
        encoded = base64.b64encode(wav_bytes()).decode()
        script = ('import sys,json\n'
                  'for line in sys.stdin:\n'
                  ' request=json.loads(line)\n'
                  ' assert request["text"]\n'
                  f' print(json.dumps({{"wav": {encoded!r}}}),flush=True)\n')
        backend = ProcessSynthesizer([sys.executable, '-u', '-c', script], timeout=5)
        try:
            self.assertEqual(await backend.synthesize('你好'), wav_bytes())
            process = backend.process
            self.assertEqual(await backend.synthesize('第二句'), wav_bytes())
            self.assertIs(backend.process, process)
        finally:
            await backend.close()
        self.assertIsNotNone(process.returncode)

    async def test_backend_metrics_are_kept_for_the_last_line_only(self):
        encoded = base64.b64encode(wav_bytes()).decode()
        script = ('import sys,json\n'
                  'for line in sys.stdin:\n'
                  ' text=json.loads(line)["text"]\n'
                  f' reply={{"wav": {encoded!r}}}\n'
                  ' if text=="有": reply["metrics"]={"scene_reference":"clip_1"}\n'
                  ' print(json.dumps(reply),flush=True)\n')
        backend = ProcessSynthesizer([sys.executable, '-u', '-c', script], timeout=5)
        try:
            self.assertIsNone(backend.metrics)
            await backend.synthesize('有')
            self.assertEqual(backend.metrics, {'scene_reference': 'clip_1'})
            await backend.synthesize('无')  # a text-only backend reports nothing; no stale clip id
            self.assertIsNone(backend.metrics)
        finally:
            await backend.close()

    async def test_timeout_kills_child_and_can_retry(self):
        backend = ProcessSynthesizer([sys.executable, '-c', 'import time; time.sleep(10)'], timeout=.05)
        with self.assertRaises(TimeoutError):
            await backend.synthesize('超时')
        self.assertIsNone(backend.process)
        backend.command = [sys.executable, '-c', 'print("{\\\"error\\\":\\\"failed\\\"}")']
        with self.assertRaises(KeyError):
            await backend.synthesize('失败')
        self.assertIsNone(backend.process)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_queue_order_source_isolation_failure_and_cancellation(self):
        sent, notices = [], []
        entered, release = asyncio.Event(), asyncio.Event()
        class Backend:
            closed = False
            async def synthesize(self, text):
                entered.set()
                await release.wait()
                if text == '失败':
                    raise ValueError('test')
                return wav_bytes()
            async def close(self):
                self.closed = True
        async def send(source, audio): sent.append(source)
        async def notice(source, text): notices.append((source, text))
        backend = Backend()
        worker = VoiceReplies(backend, send, notice, lambda source: False, capacity=1)
        first, second = Source('group', 111, 123), Source('private', 123, 123)
        try:
            self.assertTrue(worker.submit(first, '第一句'))
            await entered.wait()
            self.assertTrue(worker.submit(second, '失败'))
            self.assertFalse(worker.submit(second, '队列满'))
            self.assertEqual(sent, [])  # submission did not wait for synthesis
            release.set()
            await asyncio.wait_for(worker.queue.join(), 2)
            self.assertEqual(sent, [first])
            self.assertEqual(notices[0][0], second)
        finally:
            await worker.close()
        self.assertTrue(backend.closed)
        self.assertIsNone(worker.task)

    async def test_record_keeps_each_line_with_backend_metrics_and_never_blocks_speech(self):
        kept, sent = [], []
        class Backend:
            metrics = None
            async def synthesize(self, text, **options):
                self.metrics = {'scene_reference': 'clip_' + text[0]}
                return wav_bytes()
            async def close(self): pass
        async def send(source, audio):
            if len(sent) == 1:
                sent.append(None)
                raise RuntimeError('QQ rejected')
            sent.append(source)
        async def notice(source, text): pass
        def record(*line):
            kept.append(line)
            raise OSError('disk full')
        source = Source('private', 123, 123)
        worker = VoiceReplies(Backend(), send, notice, lambda s: False, record=record)
        try:
            with self.assertLogs(level='WARNING'):
                worker.say(source, '第一句话说得长一点', '开心', '中', scene='得意')
                worker.say(source, '第二句话发送会失败', '日常', '中')
                worker.submit(source, '第三句照常发出')
                await asyncio.wait_for(worker.queue.join(), 2)
        finally:
            await worker.close()
        self.assertEqual(sent, [source, None, source])  # a failing record never stopped the next line
        self.assertEqual([(text, options, metrics, ok) for _, text, options, _, metrics, ok in kept], [
            ('第一句话说得长一点', {'style': '开心', 'intensity': '中', 'scene': '得意'},
             {'scene_reference': 'clip_第'}, True),
            ('第二句话发送会失败', {'style': '日常', 'intensity': '中'}, {'scene_reference': 'clip_第'}, False),
            ('第三句照常发出', {}, {'scene_reference': 'clip_第'}, True)])
        self.assertTrue(all(audio == wav_bytes() for _, _, _, audio, _, _ in kept))

    async def test_spoken_lines_carry_an_opaque_chat_name_when_wired(self):
        calls = []
        class Backend:
            async def synthesize(self, text, **options):
                calls.append(options)
                return wav_bytes()
            async def close(self): pass
        async def send(source, audio): pass
        group, private = Source('group', 4455667788, 99), Source('private', 99, 99)
        worker = VoiceReplies(Backend(), send, send, lambda s: False, conversation=conversation_name)
        try:
            worker.say(group, '第一句话说得长一点', '开心', '中', scene='得意')
            worker.say(Source('group', 4455667788, 12345), '同一个群里另一个人', '日常', '中')
            worker.say(private, '私聊里的一句话呀', '安慰', '低')
            worker.submit(group, '自动朗读的不带名字')
            await asyncio.wait_for(worker.queue.join(), 2)
        finally:
            await worker.close()
        names = [c.get('conversation') for c in calls]
        self.assertEqual(names[0], names[1])  # one chat, whoever she is answering
        self.assertNotEqual(names[0], names[2])
        self.assertIsNone(names[3])
        self.assertRegex(names[0], r'^group-[0-9a-f]{8}$')
        self.assertNotIn('4455667788', ''.join(names[:3]))

    async def test_blocked_while_synthesizing_is_never_sent(self):
        entered, release = asyncio.Event(), asyncio.Event()
        blocked, sent = set(), []
        class Backend:
            async def synthesize(self, text):
                entered.set()
                await release.wait()
                return wav_bytes()
            async def close(self): pass
        async def send(source, value): sent.append(value)
        source = Source('group', 111, 789)
        worker = VoiceReplies(Backend(), send, send, lambda s: s.sender_id in blocked)
        try:
            worker.submit(source, '你好')
            await entered.wait()
            blocked.add(789)
            release.set()
            await asyncio.wait_for(worker.queue.join(), 2)
            self.assertEqual(sent, [])
        finally:
            await worker.close()

    async def test_onebot_record_routing_and_ack(self):
        with tempfile.TemporaryDirectory() as directory, patch('qq_chat.bridge.BASE', Path(directory)):
            bridge = Bridge({'owner_qq': 123, 'codex_path': 'unused'})
            sent = []
            class Socket:
                async def send(self, value):
                    message = json.loads(value)
                    sent.append(message)
                    bridge.pending[message['echo']].set_result({'status': 'ok', 'retcode': 0})
            bridge.ws = Socket()
            try:
                with patch('qq_chat.bridge.random.uniform', return_value=0):
                    await bridge.send_audio(Source('group', 111, 123), wav_bytes())
                    await bridge.send_audio(Source('private', 123, 123), wav_bytes())
                self.assertEqual([s['action'] for s in sent], ['send_group_msg', 'send_private_msg'])
                self.assertEqual(sent[0]['params']['group_id'], 111)
                self.assertEqual(sent[1]['params']['user_id'], 123)
                record = sent[0]['params']['message'][0]
                self.assertEqual(record['type'], 'record')
                self.assertEqual(base64.b64decode(record['data']['file'][9:]), wav_bytes())
                self.assertEqual(bridge.pending, {})
            finally:
                bridge.db.close()

    async def test_only_final_moderated_reply_gets_voice_after_text(self):
        with tempfile.TemporaryDirectory() as directory, patch('qq_chat.bridge.BASE', Path(directory)):
            bridge = Bridge({'owner_qq': 123, 'codex_path': 'unused'})
            order = []
            class Voice:
                def submit(self, source, text):
                    order.append(('voice', text))
                    return True
            bridge.voice = Voice()
            async def send(source, text): order.append(('text', text))
            async def chat(*args, **kwargs): return '字' * 700, False
            bridge.send, bridge.chat = send, chat
            task = asyncio.create_task(bridge.worker())
            try:
                source = Source('group', 111, 789)
                await bridge.queue.put(Incoming(source, '小爱，你好'))
                await asyncio.wait_for(bridge.queue.join(), 2)
                self.assertEqual([item[0] for item in order], ['text', 'voice'])
                self.assertEqual(order[0][1], order[1][1])
                self.assertLessEqual(len(order[1][1]), 501)
                order.clear()
                await bridge.queue.put(Incoming(source, '/ping'))
                await asyncio.wait_for(bridge.queue.join(), 2)
                self.assertEqual([item[0] for item in order], ['text'])
            finally:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError): await task
                bridge.db.close()

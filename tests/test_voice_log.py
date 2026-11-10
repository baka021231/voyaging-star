import io
import json
from pathlib import Path
import tempfile
import unittest
import wave

from qq_chat.message_policy import Source
from qq_chat.voice_log import VoiceLog
from tools import voice_log


def wav_bytes(frames=32000):
    output = io.BytesIO()
    with wave.open(output, 'wb') as audio:
        audio.setparams((1, 2, 32000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * frames)
    return output.getvalue()


class VoiceLogTests(unittest.TestCase):
    def test_line_audio_and_owner_marks(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder) / 'log'
            record = VoiceLog(folder)
            metrics = {'scene_reference': 'clip_1', 'scene_score': 0.65, 'speed': 1.0,
                       'scene_candidates': [{'id': 'clip_1', 'score': 0.65, 'recent': False},
                                            {'id': 'clip_2', 'score': 0.66, 'recent': True}]}
            record(Source('group', 4455667788, 99), '在呢，这回想聊什么呀？',
                   {'style': '开心', 'intensity': '中', 'scene': '俏皮地应声'}, wav_bytes(), metrics, True)
            record(Source('private', 99, 99), '自动朗读的一句', {}, wav_bytes(16000), None, False)
            first, second = [json.loads(line) for line in (folder / 'log.jsonl').read_text('utf-8').splitlines()]
            self.assertEqual((first['text'], first['style'], first['scene'], first['reference'], first['score'],
                              first['seconds'], first['sent']),
                             ('在呢，这回想聊什么呀？', '开心', '俏皮地应声', 'clip_1', 0.65, 1.0, True))
            self.assertEqual(first['candidates'], metrics['scene_candidates'])
            self.assertEqual((second['style'], second['scene'], second['reference'], second['seconds'], second['sent']),
                             (None, None, None, 0.5, False))
            # Conversations can be told apart, but no QQ number is written down.
            self.assertNotEqual(first['conversation'], second['conversation'])
            self.assertTrue(first['conversation'].startswith('group-'))
            self.assertNotIn('4455667788', (folder / 'log.jsonl').read_text('utf-8'))
            self.assertEqual((folder / 'audio' / (first['id'] + '.wav')).read_bytes(), wav_bytes())

            output = io.StringIO()
            voice_log.main(['--folder', str(folder), 'mark', '2', '语气', '太重'], output)
            voice_log.main(['--folder', str(folder), '5'], output)
            listing = output.getvalue()
            self.assertIn('已标记 ' + first['id'], listing)  # 1 is the newest, so 2 is the first line
            self.assertIn('✗ 语气 太重', listing)
            self.assertIn('clip_2 0.66（最近用过）', listing)
            self.assertLess(listing.index('自动朗读的一句'), listing.index('在呢，这回想聊什么呀？'))
            with self.assertRaises(SystemExit):
                voice_log.main(['--folder', str(folder), 'mark', '9', '没有这条'], output)


if __name__ == '__main__':
    unittest.main()

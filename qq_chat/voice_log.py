"""Opt-in record of every spoken line, for tuning how reference clips are chosen.

Off unless voice.debug_log names a folder. Unlike the shared logs, it keeps the
spoken text, the scene and the audio itself, so the folder has to stay on this
machine (workspace/ is not in Git). When tuning is over, remove the setting and
delete the folder.

  <folder>/log.jsonl     one line per spoken line
  <folder>/audio/<id>.wav  what was sent
  <folder>/marks.jsonl   the owner's "this one sounded wrong" notes (tools/voice_log.py)
"""
import hashlib
import io
import json
from pathlib import Path
import time
import wave


class VoiceLog:
    def __init__(self, folder):
        self.folder = Path(folder)
        (self.folder / 'audio').mkdir(parents=True, exist_ok=True)

    def __call__(self, source, text, options, audio, metrics, sent):
        now = time.time()
        stamp = time.strftime('%Y%m%d-%H%M%S', time.localtime(now)) + f'-{int(now % 1 * 1000):03d}'
        ident, again = stamp, 1
        while (self.folder / 'audio' / (ident + '.wav')).exists():  # never overwrite an earlier line's audio
            ident, again = f'{stamp}-{again}', again + 1
        (self.folder / 'audio' / (ident + '.wav')).write_bytes(audio)
        with wave.open(io.BytesIO(audio), 'rb') as wav:
            seconds = round(wav.getnframes() / wav.getframerate(), 2)
        metrics = metrics if isinstance(metrics, dict) else {}
        # Enough to tell "same conversation, 40 seconds later" without writing QQ numbers down.
        chat = hashlib.sha256(f'{source.kind}:{source.target}'.encode()).hexdigest()[:8]
        row = {
            'id': ident,
            'time': time.strftime('%Y-%m-%dT%H:%M:%S%z', time.localtime(now)),
            'conversation': f'{source.kind}-{chat}',
            'text': text,
            'style': options.get('style'),
            'intensity': options.get('intensity'),
            'scene': options.get('scene'),
            'reference': metrics.get('scene_reference'),
            'score': metrics.get('scene_score'),
            'candidates': metrics.get('scene_candidates'),
            'speed': metrics.get('speed'),
            'emotion_vector': metrics.get('emotion_vector'),
            'seconds': seconds,
            'synthesis_seconds': metrics.get('synthesis_seconds'),
            'sent': sent,
        }
        with open(self.folder / 'log.jsonl', 'a', encoding='utf-8') as log:
            log.write(json.dumps(row, ensure_ascii=False) + '\n')

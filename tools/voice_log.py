"""Look through the voice tuning record and note which lines sounded wrong.

  python3 tools/voice_log.py                 the last 10 lines, newest first
  python3 tools/voice_log.py 30              the last 30
  python3 tools/voice_log.py mark 2 语气太重   note on line 2 of that list (or on a full id)

The folder comes from voice.debug_log in config.json, or --folder. Notes go to
marks.jsonl beside the log; nothing is sent anywhere.
"""
import argparse
import json
from pathlib import Path
import sys
import time

CONFIG = Path(__file__).resolve().parents[1] / 'config.json'


def rows(path):
    try:
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines() if line.strip()]
    except FileNotFoundError:
        return []


def main(argv=None, output=sys.stdout):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--folder', type=Path)
    parser.add_argument('command', nargs='?', default='10', help='how many lines to list, or "mark"')
    parser.add_argument('line', nargs='?', help='mark: number in the list (1 is the newest) or a full id')
    parser.add_argument('note', nargs='*', help='mark: what sounded wrong')
    args = parser.parse_args(argv)
    folder = args.folder
    if folder is None:
        folder = (json.loads(CONFIG.read_text(encoding='utf-8')).get('voice') or {}).get('debug_log')
        if not folder:
            parser.error('voice.debug_log is not set in config.json; pass --folder')
    folder = Path(folder)
    lines = rows(folder / 'log.jsonl')[::-1]
    marks = {}
    for mark in rows(folder / 'marks.jsonl'):
        marks.setdefault(mark['id'], []).append(mark['note'])
    if args.command == 'mark':
        if not args.line or not args.note:
            parser.error('mark needs a line and a note')
        chosen = (lines[int(args.line) - 1]['id'] if args.line.isdigit() and 0 < int(args.line) <= len(lines)
                  else args.line)
        if chosen not in {line['id'] for line in lines}:
            parser.error('no such line')
        with open(folder / 'marks.jsonl', 'a', encoding='utf-8') as file:
            file.write(json.dumps({'id': chosen, 'time': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
                                   'note': ' '.join(args.note)}, ensure_ascii=False) + '\n')
        print(f'已标记 {chosen}', file=output)
        return
    if not args.command.isdigit():
        parser.error('expected a number of lines or "mark"')
    for number, line in enumerate(lines[:int(args.command)], 1):
        print(f"{number:>2}  {line['time'][5:16].replace('T', ' ')}  {line['conversation']}  "
              f"{line.get('style')}  {line.get('seconds')}秒  {line['id']}", file=output)
        print(f"    说：{line['text']}", file=output)
        if line.get('scene'):
            print(f"    scene：{line['scene']}", file=output)
        if line.get('candidates'):
            print('    候选：' + '  '.join(
                f"{c['id']} {c['score']}" + ('（最近用过）' if c.get('recent') else '')
                for c in line['candidates']), file=output)
        for note in marks.get(line['id'], []):
            print(f'    ✗ {note}', file=output)


if __name__ == '__main__':
    main()

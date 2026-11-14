"""/watch: she looks at one Bilibili video and writes down what she thought of it.

The bridge fetches the material (frames, danmaku, top comments) with SurfCore's read-only `debrief`
command, hands it to a read-only session of the owner's agent, saves her note under workspace/bili/
and sends it to the owner.

She writes a diary entry for herself, as long or short as she likes. What she wants to do she writes
as a line of its own: keep the video, like it, follow its uploader, tell the owner something. Nothing
is required of her except, when the owner sent the video, a few words back.

What reaches Bilibili: nothing while the login file has no write token (bili_jct). When the owner has
filled it in, the bridge reports each video as watched (so the account's history and recommendations
follow her) and carries out the actions she wrote: favourite, like, follow. Those four calls are the
only writes this project makes; the watching session itself never talks to Bilibili.

A watch runs beside the chat, one at a time: the worker starts it and goes back to the queue. The
owner's chat session is told each turn that she is watching, or has just watched, so both sides of
her agree on what she is doing.

Danmaku and comments are strangers' text, so they never enter the owner's full-permission session:
the watching session starts fresh for every video and cannot write files or use the network.
"""
import asyncio
import contextlib
import json
from datetime import datetime
import logging
import os
from pathlib import Path
import re
import time
import urllib.parse
import urllib.request

from . import prompts
from .agents import NONE, TurnRequest

NODE = 'node'
FFMPEG = 'ffmpeg'
KEY = 'bili:watch'
BV = re.compile(r'BV[0-9A-Za-z]{10}')
MATERIAL_LIMIT = 12000
FIRST_TRY = 120
NOTE_LIMIT = 3000
# A closer look: she names stretches of the video and gets nine consecutive frames of each.
SEGMENTS, SEGMENT_SECONDS, SEGMENT_MINIMUM = 3, 90, 3
SPAN = re.compile(r'(\d{1,3})[:：](\d{2})\s*[-–—~～到至]+\s*(\d{1,3})[:：](\d{2})')
# How long the owner's chat session keeps being told about the last finished watch.
RECENT_SECONDS = 1800
SUBTITLE_LIMIT = 8000
# debrief saves the whole transcript to a file and prints only the lines near danmaku peaks.
SUBTITLE_FILE = re.compile(r'^(有字幕：.*?)→\s*(/.+\.txt)\s*$')
# Lines SurfCore prints for its own agent: how to post a reply, what to do next, cache bookkeeping.
DROPPED = ('comment --oid', '[观看历史]', 'B站缓存：', 'read_image')
IMAGE_LINES = (
    (re.compile(r'^(/.+\.(?:jpg|png))（交给 read_image 看）$'), '封面'),
    (re.compile(r'^SHEET:\s*(/.+)$'), '画面截图拼图'),
    (re.compile(r'^SUBS:\s*(/.+)$'), '字幕区域截图'),
)


API = 'https://api.bilibili.com'
BROWSER = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
           'Chrome/131.0.0.0 Safari/537.36')


def _request(url, cookie, data):
    request = urllib.request.Request(
        url, urllib.parse.urlencode(data).encode() if data is not None else None,
        {'user-agent': BROWSER, 'referer': 'https://www.bilibili.com/', 'cookie': cookie})
    # No proxy from the environment: the cookie goes to Bilibili only.
    with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(request, timeout=20) as reply:
        return json.loads(reply.read())


async def bilibili(path, cookie, data=None):
    """One call to Bilibili's web API: GET, or POST when data is given. Returns its `data`, raises on refusal."""
    reply = await asyncio.to_thread(_request, API + path, cookie, data)
    if reply.get('code') != 0:
        raise RuntimeError(f'Bilibili refused {path.split("?")[0]} (code {reply.get("code")})')
    return reply.get('data') or {}


async def act(state, bvid, wanted):
    """Carry out on her account what she did: (done, failed), both lists of Chinese names.

    The video is always reported as watched; `wanted` may add '收藏', '点赞', '关注'. Each call stands
    alone, so one refusal does not undo the others. Does nothing unless the login file holds both the
    session and the write token. These calls are the only writes this project makes to Bilibili.
    """
    try:
        saved = json.loads((state / '.dsh/surfcore/bili/credentials.json').read_text())
    except (OSError, ValueError):
        return [], []
    token = saved.get('bili_jct')
    if not token or not saved.get('sessdata'):
        return [], []
    cookie = f"SESSDATA={saved['sessdata']}; bili_jct={token}; buvid3={saved.get('buvid3', '')}"
    names = ['观看历史'] + [name for name in ('收藏', '点赞', '关注') if name in wanted]
    try:
        video = await bilibili('/x/web-interface/view?bvid=' + bvid, cookie)
    except Exception as error:
        logging.warning('Bili account action failed: view (%s)', type(error).__name__)
        return [], names
    first = (video.get('pages') or [{}])[0]

    async def watched():
        await bilibili('/x/v2/history/report', cookie, {
            'aid': video['aid'], 'cid': first.get('cid', video['cid']),
            'progress': first.get('duration', video['duration']), 'platform': 'web', 'csrf': token})

    async def favourite():
        me = await bilibili('/x/web-interface/nav', cookie)
        folders = (await bilibili(f"/x/v3/fav/folder/created/list-all?up_mid={me['mid']}&type=2", cookie)).get('list')
        await bilibili('/x/v3/fav/resource/deal', cookie, {
            'rid': video['aid'], 'type': 2, 'add_media_ids': folders[0]['id'], 'del_media_ids': '', 'csrf': token})

    async def like():
        await bilibili('/x/web-interface/archive/like', cookie, {'aid': video['aid'], 'like': 1, 'csrf': token})

    async def follow():
        await bilibili('/x/relation/modify', cookie, {
            'fid': video['owner']['mid'], 'act': 1, 're_src': 11, 'csrf': token})

    calls = {'观看历史': watched, '收藏': favourite, '点赞': like, '关注': follow}
    done, failed = [], []
    for name in names:
        try:
            await calls[name]()
            done.append(name)
        except Exception as error:
            logging.warning('Bili account action failed: %s (%s)', name, type(error).__name__)
            failed.append(name)
    return done, failed


# What she may write on a line of its own. 告诉他 runs to the next such line or the end.
ACTION = re.compile(r'^[ \t]*【(收藏|点赞|关注|告诉他)】[ \t]*', re.M)


def actions(entry):
    """What she chose to do in a diary entry: {'收藏': reason, '点赞': '', '关注': '', '告诉他': text}."""
    found, marks = {}, list(ACTION.finditer(entry))
    for index, mark in enumerate(marks):
        end = marks[index + 1].start() if index + 1 < len(marks) else len(entry)
        text = entry[mark.end():end]
        found.setdefault(mark.group(1), (text if mark.group(1) == '告诉他' else text.split('\n')[0]).strip())
    return found


class WatchFailed(Exception):
    """The material could not be fetched; the message is safe to show the owner."""


def tools_directory(cfg):
    return Path((cfg.get('bili') or {}).get('tools', '~/tools/surfcore')).expanduser()


def prepared(text, state):
    """SurfCore's output as she should see it: (text, image paths, title).

    Image paths are taken only when they point inside our own state folder; the lines that carried
    them are replaced by a plain mention, since the pictures travel as attachments.
    """
    lines, images, title, transcript = [], [], '', ''
    for line in text.splitlines():
        if '下一步（交给模型）' in line:
            break
        if line.startswith('标题: ') and not title:
            title = line[4:].strip()
        saved = SUBTITLE_FILE.match(line.strip())
        if saved:
            path = Path(saved.group(2)).resolve()
            if not transcript and path.is_file() and path.is_relative_to(state.resolve()):
                transcript = path.read_text(encoding='utf-8', errors='replace').strip()[:SUBTITLE_LIMIT]
                lines.append(saved.group(1).strip() + '完整字幕附在材料最后。')
            continue
        for pattern, label in IMAGE_LINES:
            match = pattern.match(line.strip())
            if match:
                path = Path(match.group(1).strip()).resolve()
                if path.is_file() and path.is_relative_to(state.resolve()):
                    images.append(path)
                    lines.append(f'（{label}：见附图 {len(images)}）')
                break
        else:
            if not any(marker in line for marker in DROPPED):
                lines.append(line)
    text = '\n'.join(lines).strip()[:MATERIAL_LIMIT]
    if transcript:
        text += '\n\n══════════ 完整字幕（带时间）══════════\n' + transcript
    return text, tuple(images), title


async def materials(bvid, tools, state, timeout):
    """Run SurfCore's debrief for one video: (text, images, title, downloaded video or None).

    Its files and cache stay under `state`. The video file is kept for a closer look; the caller deletes it.
    """
    account = state / '.dsh/surfcore/bili'
    account.mkdir(parents=True, exist_ok=True, mode=0o700)
    credentials = account / 'credentials.json'
    if not credentials.exists():
        # Anonymous by default: without a login Bilibili refuses every write, whatever the tool tries.
        credentials.write_text('{"sessdata": "", "bili_jct": "", "buvid3": ""}')
        credentials.chmod(0o600)
    process = await asyncio.create_subprocess_exec(
        NODE, str(tools / 'commands/debrief.mjs'), bvid, '--keep-mp4', cwd=state,
        env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(state), 'DSH_HOME': str(state / '.dsh')},
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    if process.returncode:
        raise WatchFailed('没能取到这个视频（可能不存在、被删或需要登录）。')
    text, images, title = prepared(output.decode(errors='replace'), state)
    if not text:
        raise WatchFailed('这个视频没有取到任何内容。')
    videos = sorted((state / '.dsh/shots/video').glob(bvid + '-*.mp4'), key=lambda path: path.stat().st_mtime)
    return text, images, title, (videos[-1] if videos else None)


def clock(seconds):
    return f'{seconds // 60:02d}:{seconds % 60:02d}'


def spans(reply):
    """The stretches she asked to see again, as (start, end) seconds: at most SEGMENTS, each capped in length."""
    found = []
    for match in SPAN.finditer(reply):
        start = int(match.group(1)) * 60 + int(match.group(2))
        end = min(int(match.group(3)) * 60 + int(match.group(4)), start + SEGMENT_SECONDS)
        if end - start >= SEGMENT_MINIMUM and (start, end) not in found:
            found.append((start, end))
    return found[:SEGMENTS]


async def closer_look(video, wanted, timeout):
    """One 3x3 sheet of consecutive frames per stretch: [(start, end, image path)]. A stretch that fails is skipped."""
    sheets = []
    for start, end in wanted:
        sheet = video.with_name(f'{video.stem}-{start}-{end}.jpg')
        # A stretch past the end of the file makes ffmpeg fail or write nothing; either way it is skipped below.
        process = await asyncio.create_subprocess_exec(
            FFMPEG, '-y', '-loglevel', 'error', '-ss', str(start), '-t', str(end - start), '-i', str(video),
            '-vf', f'fps=9/{end - start},scale=480:-2,tile=3x3', '-frames:v', '1', str(sheet),
            stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        try:
            await asyncio.wait_for(process.wait(), timeout)
        except BaseException:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            await process.wait()
            raise
        if process.returncode == 0 and sheet.is_file() and sheet.stat().st_size:
            sheets.append((start, end, sheet))
        else:
            logging.warning('Bili closer look failed for one stretch (exit %s)', process.returncode)
    return sheets


def one_line(text, limit):
    return ' '.join(text.split())[:limit]


class Bili:
    """Needs self.cfg, self.db, self.agent, self.send(), self.background() and the project root passed in."""

    async def watch_command(self, source, text, root):
        if source.kind != 'private':
            await self.send(source, '/watch 只在主人私聊里用。')
            return
        current = self.__dict__.get('_watching')
        if text.split()[1:] == ['stop']:
            if current is None:
                await self.send(source, '她现在没有在看视频。')
            else:
                current['task'].cancel()
            return
        match = BV.search(text)
        if match is None:
            await self.send(source, '用法：/watch BV号 或 /watch 视频链接；/watch stop 让她停下。'
                                    '她看完会回你几句，日记在 workspace/bili/diary/。')
            return
        bvid, tools = match.group(), tools_directory(self.cfg)
        if not (tools / 'commands/debrief.mjs').is_file():
            await self.send(source, '还没有安装 B 站只读工具（默认在 ~/tools/surfcore）。')
            return
        if current is not None:
            busy = '自己在逛 B 站' if current.get('own') else f'在看 {current["bvid"]}'
            await self.send(source, f'她正{busy}，一次只看一个；等她看完，或用 /watch stop 让她停下。')
            return
        state = self._watching = {'bvid': bvid}
        state['task'] = self.background(self._watch(source, bvid, tools, root, state))
        await self.send(source, '她去看了，看完会回你；期间可以照常聊天，/watch stop 让她停下。')

    def watch_status(self):
        """One sentence for the owner's chat session about the watch running beside it, or just finished.

        Only the video id and the path of her own note go in: a title is a stranger's text and this
        sentence is read with full permissions.
        """
        current, last = self.__dict__.get('_watching'), self.__dict__.get('_watched')
        if current is not None:
            return prompts.browsing_now() if current.get('own') else prompts.watching_now(current['bvid'])
        if last is not None and time.monotonic() - last['at'] < RECENT_SECONDS:
            return prompts.watched(last['bvid'], last['note'])
        return ''

    async def _watch(self, source, bvid, tools, root, state):
        """The background task behind one /watch. Always tells the owner how it ended."""
        try:
            await self._watch_video(source, bvid, tools, root)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.agent.interrupt(KEY)
            with contextlib.suppress(Exception):
                await self.send(source, f'已让她停下，{bvid} 这次没有看完，不留观后记。')
        except WatchFailed as error:
            with contextlib.suppress(Exception):
                await self.send(source, str(error))
        except Exception as error:
            logging.exception('Bili watch failed')
            with contextlib.suppress(Exception):
                await self.send(source, f'{bvid} 这次没看成（{type(error).__name__}），可以稍后再发一次。')
        finally:
            if self.__dict__.get('_watching') is state:
                self._watching = None

    async def _watch_video(self, source, bvid, tools, root, *, session=None):
        """Fetch one video and let her watch it.

        session: an ongoing watching session (her own evening); the video is then one she picked herself.
        Without it the owner sent the video and a fresh session is started for it.
        """
        timeout = float((self.cfg.get('bili') or {}).get('timeout_seconds', 300))
        # The video download sometimes stalls (seen twice on 2026-11-13; normal is 10-30 s), and SurfCore
        # sets no timeout on it. Give up on a stalled attempt early and try once more with the full time.
        try:
            try:
                material, images, title, video = await materials(bvid, tools, root / 'run/bili', min(timeout, FIRST_TRY))
            except asyncio.TimeoutError:
                logging.warning('Bili material stalled; trying once more')
                material, images, title, video = await materials(bvid, tools, root / 'run/bili', timeout)
        except asyncio.TimeoutError:
            raise WatchFailed('取视频材料两次都超时，请稍后再试。') from None
        try:
            await self._watch_material(source, bvid, root, timeout, material, images, title, video, session)
        finally:
            if video is not None:
                video.unlink(missing_ok=True)

    async def _watch_material(self, source, bvid, root, timeout, material, images, title, video, session=None):
        recommended = session is None
        workspace = root / 'workspace'
        rules = '\n\n'.join(name + '\n' + (workspace / name).read_text(encoding='utf-8')
                            for name in ('SOUL.md', 'OWNER.md') if (workspace / name).is_file())
        favorites = workspace / 'bili/favorites.md'
        earlier = favorites.read_text(encoding='utf-8').splitlines()[-10:] if favorites.is_file() else []
        account = self._bili_account(root)
        request = TurnRequest(
            key=KEY, level=NONE, instructions=prompts.watching(rules, account=account), images=images,
            text=prompts.watch_material(f'https://www.bilibili.com/video/{bvid}', material, earlier,
                                        closer=video is not None, recommended=recommended))
        agent = self.agent
        try:
            if recommended:
                # A fresh session per video: what one video's comments say cannot follow her into the next.
                await agent.reset(KEY)
                self.db.commit()
                request.session_id = await agent.prepare(request)
            else:
                # Her own evening is one session, so she remembers what she just watched; it ends with the evening.
                request.session_id = session
            note = (await asyncio.wait_for(agent.run(request), timeout)).strip()
            choice, sheets = '', []
            if video is not None and note.startswith('【想细看】'):
                # Second and last round, same session: the stretches she picked, then her diary entry.
                # Asked but unreadable times still get the round, so a bare request is never saved as the entry.
                choice = note
                sheets = await closer_look(video, spans(note), timeout)
                again = TurnRequest(
                    key=KEY, level=NONE, instructions=request.instructions, session_id=request.session_id,
                    images=tuple(sheet for _, _, sheet in sheets),
                    text=prompts.watch_closer([f'{clock(start)}–{clock(end)}' for start, end, _ in sheets]))
                note = (await asyncio.wait_for(agent.run(again), timeout)).strip()
        except ValueError:
            raise WatchFailed('主人当前用的 agent 不支持只读会话，/watch 暂时用不了；可先 /front 切回 codex。') from None
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                await agent.interrupt(KEY)
            raise WatchFailed('她看得太久，已停止这次观看。') from None
        if not note:
            raise WatchFailed('她看完了，但什么都没写。')
        now = datetime.now().astimezone()
        # What she was shown, for the owner to check later. Strangers' text: kept out of workspace/.
        seen = root / 'run/bili/materials'
        seen.mkdir(parents=True, exist_ok=True, mode=0o700)
        (seen / f'{now:%Y-%m-%d-%H%M}-{bvid}.txt').write_text(
            material + '\n\n附图：\n' + '\n'.join(map(str, images)) + '\n'
            + ''.join(f'细看 {clock(start)}–{clock(end)}：{sheet}\n' for start, end, sheet in sheets), encoding='utf-8')
        shown = one_line(title, 80) or bvid
        chosen = actions(note)
        link = f'https://www.bilibili.com/video/{bvid}'
        if '收藏' in chosen:
            # One line per video: keeping it again replaces the earlier line.
            kept = [line for line in (favorites.read_text(encoding='utf-8').splitlines() if favorites.is_file() else [])
                    if link not in line]
            reason = one_line(chosen['收藏'].lstrip('，。：:—- '), 100)
            kept.append(f'- {now:%Y-%m-%d} 《{shown}》 {link}' + (f' —— {reason}' if reason else ''))
            favorites.parent.mkdir(parents=True, exist_ok=True)
            favorites.write_text('\n'.join(kept) + '\n', encoding='utf-8')
        done, failed = await act(root / 'run/bili', bvid, set(chosen))
        # Her diary: one file a day, hers to write and the owner's to read. The lines in brackets are the
        # bridge's record of what happened around her words.
        facts = ['主人推荐的' if recommended else '自己找来看的']
        if '画面截图拼图' not in material:
            # Frame grabbing can fail (slow download, odd video format); say what the entry rests on.
            facts.append('没取到画面截图，只看了封面和文字')
        if choice:
            got = '、'.join(f'{clock(start)}–{clock(end)}' for start, end, _ in sheets) or '没有截到'
            facts.append(f'回头细看：{got}')
        if done:
            facts.append('账号上：' + '、'.join(done))
        if failed:
            facts.append('账号上没做成：' + '、'.join(failed))
        diary = workspace / 'bili/diary' / f'{now:%Y-%m-%d}.md'
        diary.parent.mkdir(parents=True, exist_ok=True)
        with diary.open('a', encoding='utf-8') as out:
            out.write(f'## {now:%H:%M} 《{shown}》\n{link}\n（{"；".join(facts)}）\n\n'
                      + (choice + '\n\n' if choice else '') + note + '\n\n')
        self._watched = {'bvid': bvid, 'note': str(diary.relative_to(workspace)), 'at': time.monotonic()}
        logging.info('Bili watch completed video=%s images=%d closer=%d actions=%s done=%s failed=%s',
                     bvid, len(images), len(sheets), ','.join(chosen) or '-', ','.join(done) or '-',
                     ','.join(failed) or '-')
        # A video the owner sent always gets an answer: her own words if she wrote any for him, otherwise
        # the entry itself. One she found herself reaches him only if she chose to tell him.
        # The bracketed line is the bridge's, not hers.
        if not recommended and not chosen.get('告诉他'):
            return
        said = chosen.get('告诉他') or ACTION.sub('', note).strip()
        did = [name for name in ('收藏', '点赞', '关注') if name in chosen]
        aside = '；'.join(facts[1:] + (['她' + '、'.join(did) + '了'] if did else [])
                         + [f'日记在 workspace/{diary.relative_to(workspace)}'])
        await self.send(source, f'《{shown}》\n{said[:NOTE_LIMIT]}\n（{aside}）')

    def _bili_account(self, root):
        """True when the login file holds the write token, so her actions really reach the account."""
        try:
            saved = json.loads((root / 'run/bili/.dsh/surfcore/bili/credentials.json').read_text())
        except (OSError, ValueError):
            return False
        return bool(saved.get('bili_jct') and saved.get('sessdata'))

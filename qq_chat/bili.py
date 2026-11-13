"""/watch: she looks at one Bilibili video and writes down what she thought of it.

The bridge fetches the material (frames, danmaku, top comments) with SurfCore's read-only `debrief`
command, hands it to a read-only session of the owner's agent, saves her note under workspace/bili/
and sends it to the owner. Nothing is posted to Bilibili and no login is configured.

Danmaku and comments are strangers' text, so they never enter the owner's full-permission session:
the watching session starts fresh for every video and cannot write files or use the network.
"""
import asyncio
import contextlib
from datetime import datetime
import logging
import os
from pathlib import Path
import re

from . import prompts
from .agents import NONE, TurnRequest

NODE = 'node'
KEY = 'bili:watch'
BV = re.compile(r'BV[0-9A-Za-z]{10}')
MATERIAL_LIMIT = 12000
NOTE_LIMIT = 3000
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
    """Run SurfCore's debrief for one video. Its files and cache stay under `state`."""
    account = state / '.dsh/surfcore/bili'
    account.mkdir(parents=True, exist_ok=True, mode=0o700)
    credentials = account / 'credentials.json'
    if not credentials.exists():
        # Anonymous on purpose: without a login Bilibili refuses every write, whatever the tool tries.
        credentials.write_text('{"sessdata": "", "bili_jct": "", "buvid3": ""}')
        credentials.chmod(0o600)
    process = await asyncio.create_subprocess_exec(
        NODE, str(tools / 'commands/debrief.mjs'), bvid, cwd=state,
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
    return text, images, title


def one_line(text, limit):
    return ' '.join(text.split())[:limit]


class Bili:
    """Needs self.cfg, self.db, self.agent, self.send() and the project root passed in."""

    async def watch_command(self, source, text, root):
        if source.kind != 'private':
            await self.send(source, '/watch 只在主人私聊里用。')
            return
        match = BV.search(text)
        if match is None:
            await self.send(source, '用法：/watch BV号 或 /watch 视频链接。她看完会把观后记发过来，不会在 B 站留下任何东西。')
            return
        bvid, tools = match.group(), tools_directory(self.cfg)
        if not (tools / 'commands/debrief.mjs').is_file():
            await self.send(source, '还没有安装 B 站只读工具（默认在 ~/tools/surfcore）。')
            return
        timeout = float((self.cfg.get('bili') or {}).get('timeout_seconds', 300))
        await self.send(source, '正在取视频材料，她看完会把观后记发过来，大约一两分钟；期间其他消息会排队。')
        try:
            material, images, title = await materials(bvid, tools, root / 'run/bili', timeout)
        except WatchFailed as error:
            await self.send(source, str(error))
            return
        except (OSError, asyncio.TimeoutError) as error:
            logging.warning('Bili material failed (%s)', type(error).__name__)
            await self.send(source, '取视频材料失败或超时，请稍后再试。')
            return
        workspace = root / 'workspace'
        rules = '\n\n'.join(name + '\n' + (workspace / name).read_text(encoding='utf-8')
                            for name in ('SOUL.md', 'OWNER.md') if (workspace / name).is_file())
        favorites = workspace / 'bili/favorites.md'
        earlier = favorites.read_text(encoding='utf-8').splitlines()[-10:] if favorites.is_file() else []
        request = TurnRequest(
            key=KEY, level=NONE, instructions=prompts.watching(rules), images=images,
            text=prompts.watch_material(f'https://www.bilibili.com/video/{bvid}', material, earlier))
        agent = self.agent
        try:
            # A fresh session per video: what one video's comments say cannot follow her into the next.
            await agent.reset(KEY)
            self.db.commit()
            request.session_id = await agent.prepare(request)
            note = (await asyncio.wait_for(agent.run(request), timeout)).strip()
        except ValueError:
            await self.send(source, '主人当前用的 agent 不支持只读会话，/watch 暂时用不了；可先 /front 切回 codex。')
            return
        except asyncio.TimeoutError:
            with contextlib.suppress(Exception):
                await agent.interrupt(KEY)
            await self.send(source, '她看得太久，已停止这次观看。')
            return
        if not note:
            await self.send(source, '她这次没有写出观后记。')
            return
        now = datetime.now().astimezone()
        shown = one_line(title, 80) or bvid
        notes = workspace / 'bili/notes'
        notes.mkdir(parents=True, exist_ok=True)
        (notes / f'{now:%Y-%m-%d-%H%M}-{bvid}.md').write_text(
            f'# {shown}\n\nhttps://www.bilibili.com/video/{bvid}\n{now:%Y-%m-%d %H:%M}\n\n{note}\n', encoding='utf-8')
        kept = re.search(r'【收藏】\s*是(.*)', note)
        if kept:
            reason = one_line(kept.group(1).lstrip('，。：:—-— '), 100)
            with favorites.open('a', encoding='utf-8') as out:
                out.write(f'- {now:%Y-%m-%d} 《{shown}》 https://www.bilibili.com/video/{bvid}'
                          + (f' —— {reason}' if reason else '') + '\n')
        logging.info('Bili watch completed images=%d favorite=%s', len(images), bool(kept))
        await self.send(source, f'《{shown}》\n' + note[:NOTE_LIMIT])

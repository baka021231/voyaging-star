"""Her own time on Bilibili: she wakes at a random moment inside a window and decides what to watch.

Nobody sends her a video here. The bridge collects a few candidates (searches for words from her
interest list, plus the account's homepage recommendations) and shows her only their titles and
descriptions. She watches the ones she wants, asks for another batch, or stops: how long she stays and
how much she watches is hers to decide. What she watches goes through the same read-only watching as
/watch, in one session for the whole evening, and into the same diary. The owner hears from her only
if she writes something for him.

There is a ceiling she normally does not reach (batches, videos, minutes per evening). It exists
because a model does not get tired, every video is a whole file downloaded from Bilibili, and each
round costs more as the session grows. Reaching it ends the evening gently, with a last turn to write.

Configured by `bili.wake` in config.json; without it she never wakes on her own. It is read again
every tick, so changing it needs no restart:
    {"windows": [["20:00", "24:00"]], "skip_chance": 0.3, "screen": 5, "batches": 6, "watch": 5, "minutes": 40}
screen: titles per batch. batches, watch, minutes: the evening's ceiling.
"""
import asyncio
import contextlib
from datetime import datetime
import json
import logging
import os
import random
import re
import time

from . import bili, prompts
from .agents import NONE, TurnRequest
from .config_file import atomic_json
from .message_policy import Source

TICK_SECONDS = 30
RECENT_DIARY = 1500
INTERESTS_LIMIT = 30
# Longer videos are not offered: the whole file is downloaded to watch one (about 5 MB a minute).
LONGEST_MINUTES = 20
BROWSER_PICK = re.compile(r'^[ \t]*【想看】(.*)$', re.M)
ANOTHER = re.compile(r'^[ \t]*【换一批】', re.M)
ENOUGH = re.compile(r'^[ \t]*【不看了?】', re.M)
INTEREST = re.compile(r'^[ \t]*【(想多看|不想再看)】[ \t]*(.+)$', re.M)
RESULT = re.compile(r'^\[\d+\] (.*)\n +BV=(BV[0-9A-Za-z]{10}) +UP=(.*?) +播放=(\S+) +弹幕=\S+ +时长=(\S+)[^\n]*'
                    r'(?:\n +简介: ([^\n]*))?(?:\n +标签: ([^\n]*))?', re.M)


def now():
    return datetime.now().astimezone()


def minutes(text):
    hour, minute = text.split(':')
    return int(hour) * 60 + int(minute)


def settings(cfg):
    """The wake settings with defaults, or None when she is not to wake on her own."""
    wake = (cfg.get('bili') or {}).get('wake')
    if not wake or not wake.get('windows'):
        return None
    return {'windows': [(start, end) for start, end in wake['windows']],
            'skip_chance': float(wake.get('skip_chance', 0.3)),
            'screen': int(wake.get('screen', 5)), 'batches': int(wake.get('batches', 6)),
            'watch': int(wake.get('watch', 5)), 'minutes': float(wake.get('minutes', 40))}


def plan(windows, at):
    """One slot per window for today: a random minute inside it, or missed when the window is over.

    A window we are already inside gets a minute between now and its end. Windows do not cross midnight.
    """
    slots = []
    for start, end in windows:
        first, last = minutes(start), minutes(end) - 1
        if at > last:
            slots.append({'window': f'{start}-{end}', 'at': None, 'status': 'missed'})
        else:
            slots.append({'window': f'{start}-{end}', 'at': random.randint(max(first, at), last), 'status': 'pending'})
    return slots


def interests(workspace):
    path = workspace / 'bili/interests.md'
    if not path.is_file():
        return []
    return [line[2:].strip() for line in path.read_text(encoding='utf-8').splitlines()
            if line.startswith('- ') and line[2:].strip()]


def change_interests(workspace, reply):
    """Apply her 【想多看】/【不想再看】 lines to the interest list. Returns what changed, for the diary heading."""
    wanted = INTEREST.findall(reply)
    if not wanted:
        return []
    path = workspace / 'bili/interests.md'
    lines = path.read_text(encoding='utf-8').splitlines() if path.is_file() else ['# 我在 B 站想看的东西', '']
    changed = []
    for kind, word in wanted:
        word = ' '.join(word.split())[:20]
        have = [line for line in lines if line.startswith('- ') and line[2:].strip() == word]
        if kind == '想多看' and not have and sum(line.startswith('- ') for line in lines) < INTERESTS_LIMIT:
            lines.append('- ' + word)
            changed.append('兴趣加了“' + word + '”')
        elif kind == '不想再看' and have:
            lines = [line for line in lines if line not in have]
            changed.append('兴趣去掉了“' + word + '”')
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return changed


def too_long(length):
    """'14:05' or '1:02:33' against LONGEST_MINUTES; an unreadable length counts as too long."""
    try:
        parts = [int(part) for part in str(length).split(':')]
    except ValueError:
        return True
    total = sum(part * 60 ** index for index, part in enumerate(reversed(parts)))
    return total > LONGEST_MINUTES * 60


def seen(state):
    """Video ids she has already watched, from SurfCore's local history."""
    found = set()
    with contextlib.suppress(OSError):
        for line in (state / '.dsh/surfcore/bili/watch-history.jsonl').read_text(encoding='utf-8').splitlines():
            with contextlib.suppress(ValueError):
                found.add(json.loads(line).get('bvid'))
    return found


async def search(word, tools, state, page=1, timeout=60):
    """SurfCore's search for one word: [{bvid, title, up, plays, length, about, tags}]."""
    process = await asyncio.create_subprocess_exec(
        bili.NODE, str(tools / 'commands/search.mjs'), word, str(page), cwd=state,
        env={'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': str(state), 'DSH_HOME': str(state / '.dsh')},
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout)
    except BaseException:
        with contextlib.suppress(ProcessLookupError):
            process.kill()
        await process.wait()
        raise
    return [{'bvid': bvid, 'title': title, 'up': up, 'plays': plays, 'length': length,
             'about': about or '', 'tags': tags or '', 'from': '搜“' + word + '”'}
            for title, bvid, up, plays, length, about, tags in RESULT.findall(output.decode(errors='replace'))]


async def recommended(state):
    """The account's homepage recommendations; empty without a login."""
    try:
        saved = json.loads((state / '.dsh/surfcore/bili/credentials.json').read_text())
    except (OSError, ValueError):
        return []
    if not saved.get('sessdata'):
        return []
    cookie = f"SESSDATA={saved['sessdata']}; buvid3={saved.get('buvid3', '')}"
    data = await bili.bilibili('/x/web-interface/index/top/rcmd?fresh_type=3&ps=10', cookie)
    return [{'bvid': item['bvid'], 'title': item.get('title', ''), 'up': (item.get('owner') or {}).get('name', ''),
             'plays': (item.get('stat') or {}).get('view', '?'),
             'length': f"{item.get('duration', 0) // 60}:{item.get('duration', 0) % 60:02d}",
             'about': '', 'tags': '', 'from': '首页推荐'}
            for item in data.get('item') or [] if item.get('bvid')]


async def candidates(words, tools, state, limit, skip=(), batch=0):
    """Up to `limit` videos she has not watched: two words from her interests, the rest from recommendations.

    skip: ids already shown tonight. batch: which batch of the evening this is; later batches move on
    through `words` (the caller shuffles them once per evening) and, once all were used, to the next
    page of results, so the batches differ.
    """
    watched, picked = seen(state) | set(skip), []

    def take(items, count):
        for item in items:
            if (count and item['bvid'] not in watched and not too_long(item['length'])
                    and all(item['bvid'] != other['bvid'] for other in picked)):
                picked.append(item)
                count -= 1

    for offset in range(min(2, len(words))):
        position = batch * 2 + offset
        try:
            found = await search(words[position % len(words)], tools, state, 1 + position // len(words))
            take(found, 2 if len(picked) < 2 else 1)
        except (OSError, asyncio.TimeoutError) as error:
            logging.warning('Bili search failed (%s)', type(error).__name__)
    try:
        take(await recommended(state), limit - len(picked))
    except Exception as error:
        logging.warning('Bili recommendations failed (%s)', type(error).__name__)
    random.shuffle(picked)
    return picked[:limit]


def listing(items):
    return '\n\n'.join(
        f"{item['bvid']} 《{item['title'][:80]}》\n  UP：{item['up']}　时长：{item['length']}　播放：{item['plays']}"
        f"　来源：{item['from']}"
        + (f"\n  简介：{item['about'][:120]}" if item['about'] else '')
        + (f"\n  标签：{item['tags'][:100]}" if item['tags'] else '')
        for item in items)


class BiliWake:
    """Needs the Bili mixin, self.cfg, self.db, self.agent, self.send() and self.background()."""

    async def wake_loop(self, root):
        """Runs for the life of the bridge; a failed tick is logged and the next one tries again."""
        while True:
            try:
                await self.wake_tick(root)
            except Exception:
                logging.exception('Bili wake tick failed')
            await asyncio.sleep(TICK_SECONDS)

    async def wake_tick(self, root):
        # Read from the file each tick, so the owner can change the windows without a restart.
        try:
            wake = settings(json.loads((root / 'config.json').read_text()))
        except (OSError, ValueError):
            wake = settings(self.cfg)
        if wake is None:
            return
        moment = now()
        today, at = f'{moment:%Y-%m-%d}', moment.hour * 60 + moment.minute
        path = root / 'run/bili/wake.json'
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        windows = [f'{start}-{end}' for start, end in wake['windows']]
        if state.get('date') != today or [slot['window'] for slot in state.get('slots', [])] != windows:
            # Planned once a day (and again when the windows change), then kept, so a restart neither
            # moves the time nor wakes her twice.
            state = {'date': today, 'slots': plan(wake['windows'], at)}
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_json(path, state)
            logging.info('Bili wake planned: %s', ', '.join(
                f"{slot['window']}→{slot['at'] // 60:02d}:{slot['at'] % 60:02d}" if slot['at'] is not None
                else f"{slot['window']}→missed" for slot in state['slots']))
        for slot in state['slots']:
            if slot['status'] != 'pending' or slot['at'] > at:
                continue
            if at >= minutes(slot['window'].split('-')[1]):
                slot['status'] = 'missed'
            elif self.__dict__.get('_watching') is not None:
                continue   # busy with a video the owner sent; try again next tick, until the window ends
            elif random.random() < wake['skip_chance']:
                # "Not tonight" is part of being someone: no model turn, one line in the diary.
                slot['status'] = 'skipped'
                self._diary_line(root, moment, '醒了一下', '今天没去逛 B 站')
                logging.info('Bili wake skipped')
            else:
                slot['status'] = 'woke'
                watching = self._watching = {'bvid': '', 'own': True}
                watching['task'] = self.background(self._evening(root, wake, watching))
            atomic_json(path, state)
            return

    def _diary_line(self, root, moment, title, fact, text=''):
        diary = root / 'workspace/bili/diary' / f'{moment:%Y-%m-%d}.md'
        diary.parent.mkdir(parents=True, exist_ok=True)
        with diary.open('a', encoding='utf-8') as out:
            out.write(f'## {moment:%H:%M} {title}\n（{fact}）\n\n' + (text + '\n\n' if text else ''))
        return diary

    async def _evening(self, root, wake, watching):
        """One evening of her own. Failures are logged, never sent: nobody asked her to do this."""
        owner = int(self.cfg['owner_qq'])
        source = Source('private', owner, owner)
        try:
            await self._browse(root, wake, source)
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.agent.interrupt(bili.KEY)
            with contextlib.suppress(Exception):
                await self.send(source, '已让她停下，这次逛 B 站到此为止。')
        except bili.WatchFailed as error:
            logging.warning('Bili evening ended early: %s', error)
        except Exception:
            logging.exception('Bili evening failed')
        finally:
            if self.__dict__.get('_watching') is watching:
                self._watching = None

    async def _browse(self, root, wake, source):
        workspace, state, tools = root / 'workspace', root / 'run/bili', bili.tools_directory(self.cfg)
        timeout = float((self.cfg.get('bili') or {}).get('timeout_seconds', 300))
        words = interests(workspace)
        random.shuffle(words)
        rules = '\n\n'.join(name + '\n' + (workspace / name).read_text(encoding='utf-8')
                            for name in ('SOUL.md', 'OWNER.md') if (workspace / name).is_file())
        favorites = workspace / 'bili/favorites.md'
        earlier = favorites.read_text(encoding='utf-8').splitlines()[-10:] if favorites.is_file() else []
        days = sorted((workspace / 'bili/diary').glob('*.md')) if (workspace / 'bili/diary').is_dir() else []
        recent = '\n'.join(day.read_text(encoding='utf-8') for day in days[-2:])[-RECENT_DIARY:]
        instructions = prompts.watching(rules, account=self._bili_account(root))
        agent, session = self.agent, None
        started, shown, watched, batches = time.monotonic(), set(), 0, 0

        async def turn(text):
            """One round of the evening's single session, started on first use."""
            nonlocal session
            request = TurnRequest(key=bili.KEY, level=NONE, instructions=instructions, text=text, session_id=session)
            if session is None:
                # Started fresh: she remembers what she watched tonight, and strangers' text from tonight
                # does not follow her into tomorrow.
                await agent.reset(bili.KEY)
                self.db.commit()
                session = request.session_id = await agent.prepare(request)
            try:
                return (await asyncio.wait_for(agent.run(request), timeout)).strip()
            except ValueError:
                raise bili.WatchFailed('the owner agent has no read-only level') from None

        async def tell(reply, diary):
            told = bili.actions(reply).get('告诉他')
            if told:
                await self.send(source, f'{told[:bili.NOTE_LIMIT]}\n'
                                        f'（她在自己逛 B 站；日记在 workspace/{diary.relative_to(workspace)}）')

        def spent():
            return time.monotonic() - started >= wake['minutes'] * 60

        ended = None
        while ended is None:
            items = await candidates(words, tools, state, wake['screen'], shown, batches) if words else []
            moment = now()
            if not items:
                ended = '没有没看过的视频了'
                break
            batches += 1
            shown.update(item['bvid'] for item in items)
            reply = await turn(prompts.wake(listing(items), words, recent, earlier) if session is None
                               else prompts.wake_more(listing(items)))
            offered, picks = {item['bvid'] for item in items}, []
            for line in BROWSER_PICK.findall(reply):
                for bvid in bili.BV.findall(line):
                    if bvid in offered and bvid not in picks:
                        picks.append(bvid)
            picks = picks[:max(0, wake['watch'] - watched)]
            # What was in front of her and what she took, for the owner to check later. Strangers' text:
            # kept out of workspace/, next to the material of the videos themselves.
            offer = state / 'materials' / f'{moment:%Y-%m-%d-%H%M}-offer-{batches}.txt'
            offer.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            offer.write_text('她的兴趣清单：' + '、'.join(words) + '\n\n' + listing(items) + '\n\n她挑了：'
                             + ('、'.join(picks) or '没有') + '\n', encoding='utf-8')
            changed = change_interests(workspace, reply)
            fact = '；'.join([f'自己的时间；第 {batches} 批，看了 {len(items)} 个视频的标题和简介',
                             f'挑了 {len(picks)} 个'] + changed)
            diary = self._diary_line(root, moment, '醒了' if batches == 1 else '又看了一批', fact, reply)
            logging.info('Bili evening: batch=%d offered=%d picked=%d', batches, len(items), len(picks))
            await tell(reply, diary)
            for bvid in picks:
                if spent():
                    break
                self.__dict__['_watching']['bvid'] = bvid
                try:
                    await self._watch_video(source, bvid, tools, root, session=session)
                    watched += 1
                except bili.WatchFailed as error:
                    # One video that cannot be fetched does not end her evening.
                    self._diary_line(root, now(), '没看成', f'{bvid}：{error}')
            if ENOUGH.search(reply) or not (picks or ANOTHER.search(reply)):
                # She said so, or asked for nothing more: a reply with neither a pick nor a request
                # for another batch is taken as being done, never as wanting more.
                ended = '她自己说不逛了'
            elif watched >= wake['watch'] or batches >= wake['batches'] or spent():
                ended = '到了今天的上限'
                # Not cut off mid-thought: one last turn, told in a way that belongs to her evening.
                closing = await turn(prompts.wake_enough())
                await tell(closing, self._diary_line(root, now(), '收尾', '到了今天的上限，没有再给她新的视频', closing))
        minutes_spent = round((time.monotonic() - started) / 60)
        if batches == 0:
            self._diary_line(root, now(), '醒了', '想逛 B 站，但这会儿没找到没看过的视频')
        else:
            self._diary_line(root, now(), '不逛了', f'{ended}；这次看了 {batches} 批标题、{watched} 个视频，'
                                                 f'大约 {minutes_spent} 分钟')
        logging.info('Bili evening ended: reason=%s batches=%d watched=%d minutes=%d',
                     ended, batches, watched, minutes_spent)

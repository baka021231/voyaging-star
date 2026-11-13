"""Commands the bridge answers itself, without a model turn."""
import asyncio
import logging
import sqlite3

from .message_policy import owner_request
from .turns import serialized

HELP = ("Aemeath：主人在私聊或群聊可请求Linux操作，其他人仅文字聊天；Windows文件只读。群聊可单独@机器人，或喊Aemeath、爱弥斯、ams、小爱（英文不分大小写）。图片识别向群友和主人开放，每条最多5张、每张20 MiB，支持PNG、JPEG、WebP、GIF。文件附件仅主人私聊接收。/ping、/help 所有人可用；/voice on 开启主人私聊仅语音、/voice off 或“切回文字”关闭、/voice 查看状态；/new 创建新会话、/session 查看会话ID、/models 查看可选模型、/model 查看当前模型、/model 模型名 切换、/model default 恢复默认、/front 查看主人用的 agent、/front 配置名 切换并自动重启（所有主人会话生效，访客不变）、/effort 查看强度、/effort 档位 切换强度、/effort default 恢复模型默认强度、/stop 停止当前这一轮、/watch BV号 让她在后台看一个B站视频并写观后记（仅私聊，不在B站留痕，期间可照常聊天）、/watch stop 让她停下、/blacklist 查看群聊用户黑名单、/block QQ号 拉黑、/unblock QQ号 解除拉黑仅限主人。黑名单用户的群消息静默忽略。")


class Commands:
    """Needs self.cfg, self.db, self.send(), the agent selection helpers, self.block_user() and self.reset_group_context()."""

    async def model_commands(self, source, text):
        """/model, /models and /effort; a failure leaves the old setting in place and says so."""
        try:
            if text.split()[0] == '/effort':
                await self.effort_command(source, text)
            else:
                await self.model_command(source, text)
        except (RuntimeError, OSError, ValueError, sqlite3.Error, asyncio.TimeoutError):
            logging.warning('Model management failed')
            await self.send(source, '模型操作未完成，请稍后重试；用 /model 或 /effort 可重新检查当前状态。')

    async def plain_command(self, source, text, context_id):
        """Commands answered without the model. True when text was one of them.

        The worker has already refused slash commands from anyone but the owner (except /ping and /help).
        """
        if text in ("/ping", "连接测试"):
            await self.send(source, "QQ 聊天连接正常。")
        elif text == "/new":
            await self.agent_for(source).reset(self.conversation(source))
            self.db.execute("DELETE FROM history WHERE conversation=?", (self.conversation(source),))
            self.reset_group_context(source, context_id)
            self.db.commit()
            await self.send(source, '已清空聊天上下文，可以开始新话题。')
        elif text == "/session":
            thread = self.agent_for(source).session_id(self.conversation(source))
            await self.send(source, "当前会话：" + (thread if thread else "尚未创建；发送一条消息即可创建。"))
        elif text == '/blacklist':
            users = self.cfg.get('group_user_blacklist', [])
            await self.send(source, '群聊用户黑名单：' + ('、'.join(map(str, users)) if users else '空'))
        elif text.split(maxsplit=1)[:1] in (['/block'], ['/unblock']):
            parts = text.split()
            if len(parts) != 2:
                await self.send(source, '用法：/block QQ号 或 /unblock QQ号')
                return True
            try:
                self.block_user(parts[1], blocked=parts[0] == '/block')
            except ValueError as error:
                await self.send(source, str(error))
                return True
            await self.send(source, '已加入群聊用户黑名单。' if parts[0] == '/block' else '已解除群聊用户拉黑。')
        elif text == "/help":
            await self.send(source, HELP)
        else:
            return False
        return True

    @serialized
    async def effort_command(self, source, text):
        if not owner_request(source, self.cfg['owner_qq']):
            await self.send(source, '推理强度管理仅限主人使用。')
            return
        parts = text.split()
        if len(parts) not in (1, 2) or parts[0] != '/effort':
            await self.send(source, '用法：/effort 查看当前设置与可选档位；/effort 档位 切换；/effort default 恢复模型默认强度。')
            return
        selection = self.selection(source)
        if selection:
            name, current = selection
        else:
            thread = await self.thread_for(source, True)
            name, current = self.agent.model_state(thread)
        models = {item['model']: item for item in await self.agent.models()}
        info = models.get(name, {})
        levels = [item['reasoningEffort'] for item in info.get('supportedReasoningEfforts', [])
                  if isinstance(item, dict) and isinstance(item.get('reasoningEffort'), str)]
        if len(parts) == 1:
            await self.send(source, '当前会话模型：' + (name or '未知') + '；effort：' + (current or '接口未返回，暂无法确认')
                            + '。可选档位：' + ('、'.join(levels) or '接口未提供') + '。')
            return
        target = info.get('defaultReasoningEffort') if parts[1] == 'default' else parts[1]
        if not name or target not in levels:
            await self.send(source, '该模型不支持这个强度，或接口未提供可校验的档位。请用 /effort 查看；原设置未改变。')
            return
        self.save_selection(source, name, target)
        await self.send(source, '当前会话 effort 已设为 ' + target + '，下一条消息生效，模型和聊天上下文保留。')

    @serialized
    async def model_command(self, source, text):
        # Defense in depth: never query the catalog or change a thread for a guest.
        if not owner_request(source, self.cfg['owner_qq']):
            await self.send(source, '模型管理仅限主人使用。')
            return
        parts = text.split()
        if parts == ['/models']:
            models = {item['model']: item for item in await self.agent.models()}
            names = [name + ('（仅文字）' if 'image' not in info.get('inputModalities', ['text', 'image']) else '')
                     for name, info in models.items()]
            await self.send(source, '可选模型：\n' + '\n'.join(names) if names else '当前没有可选模型。')
            return
        if parts == ['/model']:
            selection = self.selection(source)
            if selection:
                await self.send(source, '当前会话模型设置：' + selection[0] + '，下一轮使用。')
                return
            thread = await self.thread_for(source, True)
            model, _ = self.agent.model_state(thread)
            await self.send(source, '当前会话模型：' + (model or '接口未返回实际模型，暂时无法确认。'))
            return
        if len(parts) != 2 or parts[0] != '/model':
            await self.send(source, '用法：/models 列表；/model 查看；/model 模型名 切换；/model default 恢复默认。')
            return
        models = {item['model']: item for item in await self.agent.models()}
        name = await self.agent.default_model() if parts[1] == 'default' else parts[1]
        if name not in models:
            await self.send(source, '这个模型不在当前可选列表中，请先用 /models 查看。')
            return
        effort = models[name].get('defaultReasoningEffort')
        self.save_selection(source, name, effort)
        notice = '该模型仅支持文字。' if 'image' not in models[name].get('inputModalities', ['text', 'image']) else ''
        await self.send(source, '当前会话已选择 ' + name + '，下一条消息使用该模型，聊天上下文保留。' + notice)

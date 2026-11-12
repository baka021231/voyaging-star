"""Owner-private speech mode controls, independent of model and speech queues."""
import json
import logging

from .config_file import atomic_json, config_lock
from .message_policy import owner_request


ALIASES = {'切回文字': 'off', '关闭仅语音模式': 'off', '恢复文字模式': 'off',
           '开启仅语音模式': 'on'}


def is_voice_command(text):
    parts = text.strip().split()
    return text.strip() in ALIASES or bool(parts and parts[0].casefold() == '/voice')


async def command(bridge, source, text, root):
    if source.kind != 'private' or not owner_request(source, bridge.cfg['owner_qq']):
        await bridge.send(source, '这个开关仅限主人在私聊使用。')
        return
    parts = text.strip().split()
    mode = ALIASES.get(text.strip())
    if mode is None:
        if len(parts) == 1:
            enabled = (bridge.cfg.get('voice') or {}).get('private_voice_only', False) is True
            await bridge.send(source, '私聊仅语音：' + ('开启' if enabled else '关闭')
                              + '。/voice on 开启；/voice off 或“切回文字”关闭。')
            return
        if len(parts) != 2 or parts[1].casefold() not in ('on', 'off'):
            await bridge.send(source, '用法：/voice on 开启；/voice off 关闭；/voice 查看状态。')
            return
        mode = parts[1].casefold()
    if mode == 'on' and getattr(bridge, 'voice', None) is None:
        await bridge.send(source, '语音服务尚未启用，暂不能开启仅语音。文字回复保留。')
        return
    try:
        # Merge under the same lock as the other config writers; keep unrelated settings.
        with config_lock(root):
            path = root / 'config.json'
            cfg = json.loads(path.read_text())
            cfg.setdefault('voice', {})['private_voice_only'] = mode == 'on'
            atomic_json(path, cfg)
        bridge.cfg.setdefault('voice', {})['private_voice_only'] = mode == 'on'
    except (OSError, ValueError, TypeError):
        logging.warning('Private speech mode persistence failed')
        await bridge.send(source, '语音模式保存失败，原设置未改变。请稍后重试。')
        return
    # Always text: the escape hatch must work even when speech synthesis is broken.
    await bridge.send(source, '私聊仅语音已开启，发“切回文字”或 /voice off 就能关闭。'
                      if mode == 'on' else '已切回文字回复啦。之前排队的语音仍可能发出。')

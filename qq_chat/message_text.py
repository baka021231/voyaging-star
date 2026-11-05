"""Render OneBot mentions as reference text, never as sender authority."""


def mention_text(data, bot_id=None):
    target = str(data.get('qq', ''))
    if target == 'all':
        return '[提及全体成员]'
    if not target.isascii() or not target.isdigit() or len(target) > 20:
        return '[提及对象无法识别]'
    if bot_id is not None and target == str(bot_id):
        return f'[提及本机器人，QQ：{target}]'
    return f'[提及QQ：{target}]'


def message_text(parts, bot_id=None):
    text = []
    for part in parts:
        data = part.get('data', {})
        if part.get('type') == 'text':
            text.append(data.get('text', ''))
        elif part.get('type') == 'at':
            text.append(mention_text(data, bot_id))
    return ''.join(text).strip()

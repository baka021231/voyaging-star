"""QQ message data, parsing and identity policy without bridge runtime state."""
from dataclasses import dataclass
import unicodedata

from .message_references import reference_segments
from .message_text import message_text
from .moderation import blocked_group_sender


@dataclass(frozen=True)
class Source:
    kind: str
    target: int
    sender_id: int | None = None
    sender_name: str = ''

    @property
    def conversation(self):
        return f"{self.kind}:{self.target}"


@dataclass(frozen=True)
class Incoming:
    source: Source
    text: str
    attachments: tuple = ()
    context_id: int | None = None
    images: tuple = ()
    references: tuple = ()


def owner_request(source, owner):
    return (str(source.sender_id) == str(owner)
            and (source.kind == 'group' or (source.kind == 'private' and str(source.target) == str(owner))))


def conversation_key(source, owner):
    # Keep tool-enabled owner turns separate from other group members' history.
    if source.kind == 'group' and owner_request(source, owner):
        return source.conversation + ':owner'
    return source.conversation


def image_request(source, owner):
    """Group vision accepts message images without granting owner tools."""
    return source.kind == 'group' or owner_request(source, owner)


def speaker_message(source, text, owner):
    # Names are display data; authority comes only from the numeric sender ID.
    name = ''.join(c if c not in '[]|' and not unicodedata.category(c).startswith('C') else ' '
                   for c in source.sender_name).strip()[:64] or str(source.sender_id)
    role = '主人' if str(source.sender_id) == str(owner) else ('群友' if source.kind == 'group' else '访客')
    scene = '私聊' if source.kind == 'private' else '群聊'
    return f'[发言人：{name} | QQ：{source.sender_id} | 身份：{role} | 场景：{scene}]\n{text}'


def incoming_text(event, owner, allowed_groups=(), allow_all_groups=False, require_trigger=True,
                  group_user_blacklist=(), private_whitelist=()):
    if event.get("post_type") != "message":
        return None
    user = str(event.get("user_id", ""))
    bot = str(event.get("self_id", ""))
    if not user.isdigit() or not bot.isdigit() or int(user) == int(bot):
        return None
    kind = event.get("message_type")
    sender = event.get('sender') or {}
    name = str(sender.get('card') or sender.get('nickname') or user)
    if kind == "private":
        if user != str(owner) and user not in {str(qq) for qq in private_whitelist}:
            return None
        source = Source("private", int(user), int(user), name)
    elif kind == "group":
        group = str(event.get("group_id", ""))
        if not group.isdigit() or (not allow_all_groups and group not in {str(g) for g in allowed_groups}):
            return None
        source = Source("group", int(group), int(user), name)
        if blocked_group_sender(source, {'owner_qq': owner, 'group_user_blacklist': group_user_blacklist}):
            return None
    else:
        return None
    message = event.get("message", [])
    if not isinstance(message, list):
        # Request array-format messages from NapCat, avoiding CQ code interpretation.
        return None
    text = "".join(part.get("data", {}).get("text", "") for part in message if part.get("type") == "text").strip()
    mentioned = any(
        part.get("type") == "at" and str(part.get("data", {}).get("qq", "")) == bot
        for part in message
    )
    if require_trigger and kind == "group" and not (mentioned or any(alias in text.casefold() for alias in ('aemeath', '爱弥斯', 'ams', '小爱'))):
        return None
    text = message_text(message, bot)
    attachments = tuple(part.get('data', {}) for part in message
                        if part.get('type') == 'file') if kind == 'private' and owner_request(source, owner) else ()
    images = tuple(part.get('data', {}) for part in message
                   if part.get('type') == 'image') if image_request(source, owner) else ()
    references = reference_segments(message)
    return Incoming(source, text, attachments, images=images, references=references) if text or attachments or images or references else None


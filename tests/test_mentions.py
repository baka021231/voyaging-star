"""Mention-only triggering and target preservation across direct/quoted messages."""
import unittest
from qq_chat.bridge import incoming_text, owner_request
from qq_chat.message_references import expand_references
from qq_chat.message_text import mention_text


def part(kind, **data):
    return {'type': kind, 'data': data}


def event(parts):
    return {'post_type': 'message', 'message_type': 'group', 'group_id': 999,
            'self_id': 456, 'user_id': 789, 'message': parts}


class MentionTests(unittest.TestCase):
    def test_bare_bot_mention_triggers_without_owner_authority(self):
        incoming = incoming_text(event([part('at', qq='456')]), 123, allow_all_groups=True)
        self.assertEqual(incoming.text, '[提及本机器人，QQ：456]')
        self.assertFalse(owner_request(incoming.source, 123))
        self.assertIsNone(incoming_text(event([part('at', qq='456')]), 123))
        self.assertIsNone(incoming_text(event([part('at', qq='456')]), 123,
                                       allow_all_groups=True, group_user_blacklist=[789]))

    def test_other_mentions_preserve_order_without_triggering_bot(self):
        parts = [part('at', qq='123'), part('text', text='你好'), part('at', qq='all')]
        self.assertIsNone(incoming_text(event(parts), 123, allow_all_groups=True))
        incoming = incoming_text(event(parts), 123, allow_all_groups=True, require_trigger=False)
        self.assertEqual(incoming.text, '[提及QQ：123]你好[提及全体成员]')
        parts.insert(0, part('text', text='小爱，看'))
        self.assertIn('[提及QQ：123]', incoming_text(event(parts), 123, allow_all_groups=True).text)
        self.assertEqual(mention_text({'qq': '小爱 /new'}), '[提及对象无法识别]')


class QuotedMentionTests(unittest.IsolatedAsyncioTestCase):
    async def test_quote_and_forward_preserve_mention_target(self):
        from qq_chat.bridge import Source
        async def call(action, params):
            return {'group_id': 999, 'sender': {'nickname': 'a'},
                    'message': [part('text', text='问'), part('at', qq='123'), part('text', text='吧')]}
        records, images = await expand_references(call, Source('group', 999, 789), [part('reply', id='77')])
        self.assertIn('问[提及QQ：123]吧', str(records))
        records, images = await expand_references(call, Source('group', 999, 789),
            [part('forward', content=[{'name': 'a', 'content': [part('at', qq='all')]}])])
        self.assertIn('[提及全体成员]', str(records))

"""The per-turn capability channel itself: who gets a grant, how long it lives, what the socket accepts."""
import asyncio
from pathlib import Path
import shlex

from tests.support import BridgeCase


class ToolServerTests(BridgeCase):
    async def test_grant_permissions_revoke_expiry_and_foreign_source(self):
        server = await self.setup_tools()
        self.assertEqual(server.folder.stat().st_mode & 0o777, 0o700)
        self.assertEqual(server.socket.stat().st_mode & 0o777, 0o600)
        for names in ((), ('memory_search', 'shell'), ('dispatch_task',), ('poke',)):
            with self.subTest(names=names), self.assertRaises(ValueError):
                async with server.grant(self.owner, names=names): pass
        with self.assertRaises(ValueError):
            async with server.grant(self.guest, names=('memory_search',)): pass
        async with server.grant(self.owner, names=('memory_search',)) as grant:
            token = self.token(grant)
            self.assertEqual(Path(grant.config_path).stat().st_mode & 0o777, 0o600)
            self.assertEqual([t['name'] for t in await server.call(token, 'tools/list', {})], ['memory_search'])
            with self.assertRaises(ValueError): await server.call('forged', 'tools/list', {})
            source, key, _ = server.grants[token]; server.grants[token] = (source, key, 0)
            with self.assertRaises(ValueError): await server.call(token, 'tools/list', {})
        self.assertFalse(Path(grant.config_path).exists())
        with self.assertRaises(ValueError): await server.call(token, 'tools/list', {})

    async def test_calls_cannot_supply_another_chat_or_unknown_tool(self):
        server = await self.setup_tools()
        async with server.grant(self.owner, names=('memory_search',)) as grant:
            for name, args in [('memory_search', {'query': 'x', 'conversation': 'other'}),
                               ('memory_search', {'query': 'x', 'group_id': 20001}),
                               ('memory_search', {}), ('memory_search', 'x'),
                               ('poke', {}), ('dispatch_task', {'goal': 'goal'}), ('speak', {'text': 'x', 'style': '日常'})]:
                with self.subTest(name=name, args=args), self.assertRaises(ValueError):
                    await server.call(self.token(grant), name, args)

    async def test_cancelling_the_turn_revokes_token_and_removes_the_grant_file(self):
        server = await self.setup_tools(); self.front.release.clear()
        turn = asyncio.create_task(self.bridge.chat(self.owner, 'hello'))
        await self.front.entered.wait()
        line = next(l for l in self.front.requests[-1].runtime_instructions.splitlines() if ' --grant ' in l)
        words = shlex.split(line)
        config = words[words.index('--grant') + 1]
        self.assertTrue(server.grants)
        self.assertTrue(Path(config).exists())
        turn.cancel(); await asyncio.gather(turn, return_exceptions=True)
        self.assertFalse(server.grants)
        self.assertFalse(Path(config).exists())

    async def test_rejections_do_not_log_token_or_argument_text(self):
        server = await self.setup_tools()
        async with server.grant(self.owner, names=('memory_search',)) as grant:
            token = self.token(grant)
            reader, writer = await asyncio.open_unix_connection(server.socket)
            with self.assertLogs(level='WARNING') as logs:
                writer.write(('{"token": "%s", "name": "secret-tool-name", "arguments": {"query": "secret-argument"}}\n' % token).encode())
                await writer.drain()
                await reader.readline()
            writer.close(); await writer.wait_closed()
            output = '\n'.join(logs.output)
            self.assertIn('Tool rejected name=unknown', output)
            for private in (token, 'secret-tool-name', 'secret-argument'):
                self.assertNotIn(private, output)

    async def test_socket_rejects_replacing_regular_file(self):
        server = self.bridge.tool_server; server.folder.mkdir()
        server.socket.write_text('preserve')
        with self.assertRaises(ValueError): await server.start()
        self.assertEqual(server.socket.read_text(), 'preserve')

"""Offline tests: no Discord login, Blizzard token issuance or live requests."""
import asyncio
import os
import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, MagicMock, Mock, patch
from urllib.parse import unquote

import discord
from discord.ext import commands
import forever_character as f
import guild_applications as g
from test_guild_applications import history, msg, record


class Api(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.api = f.ForeverAPI()
        env = patch.dict(os.environ, {'BLIZZARD_CLIENT_ID': 'dummy', 'BLIZZARD_CLIENT_SECRET': 'dummy'})
        env.start()
        self.addCleanup(env.stop)
        patcher = patch.object(f.aiohttp, 'ClientSession')
        self.session = patcher.start()
        self.addCleanup(patcher.stop)
        self.http = MagicMock()
        self.session.return_value.__aenter__.return_value = self.http
        self.http.post.return_value = self.reply(200, {'access_token': 'dummy-token', 'expires_in': 3600})

    @staticmethod
    def reply(status, data):
        ctx = MagicMock()
        ctx.__aenter__.return_value = N(status=status, json=AsyncMock(return_value=data))
        return ctx

    def test_slug_order_normalization_and_unsafe_input(self):
        self.assertEqual(f.slug('  Asha  Brightvale '), 'asha-brightvale')
        self.assertEqual(unquote(f.slug('아샤 브라이트베일')), '아샤-브라이트베일')
        self.assertEqual(f.slug('Pröudmoore-WRYNN'), 'pr%C3%B6udmoore-wrynn')
        for raw in ('테스트', '../pro file', '<@1> hi', 'one two three', 'a %20', 'work` around', '-', ''):
            with self.subTest(raw=raw), self.assertRaises(f.LookupProblem):
                f.slug(raw)

    async def test_client_credentials_and_cached_token_kr_paths(self):
        self.http.get.side_effect = [self.reply(200, {'name': '아샤'}), self.reply(200, {'equipped_items': []})]
        await self.api.lookup('아샤 달빛')
        await self.api.lookup('아샤 달빛', equipment=True)
        self.http.post.assert_called_once()
        self.assertEqual(self.http.post.call_args.args, ('https://oauth.battle.net/token',))
        self.assertEqual(self.http.post.call_args.kwargs['data'], {'grant_type': 'client_credentials'})
        self.assertEqual(self.http.post.call_args.kwargs['auth'].login, 'dummy')
        base = 'https://kr.api.blizzard.com/profile/wow/character/%EC%95%84%EC%83%A4-%EB%8B%AC%EB%B9%9B'
        for call, suffix in zip(self.http.get.call_args_list, ('', '/equipment')):
            self.assertEqual(call.args[0], base + suffix)
            self.assertEqual(call.kwargs['params'], {'namespace': 'profile-forever-kr', 'locale': 'ko_KR'})
            self.assertEqual(call.kwargs['headers']['Authorization'], 'Bearer dummy-token')
            self.assertFalse(call.kwargs['allow_redirects'])
        self.assertLess(self.session.call_args.kwargs['timeout'].total, 15)

    async def test_401_refresh_and_rate_limit(self):
        self.http.get.side_effect = [self.reply(401, {}), self.reply(429, {})]
        with self.assertRaisesRegex(f.LookupProblem, '요청이 많습니다'):
            await self.api.lookup('Test Mage')
        self.assertEqual(self.http.post.call_count, 2)

    async def test_missing_env_no_http(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(f.LookupProblem, '설정되지'):
            await self.api.lookup('Test Mage')
        self.http.post.assert_not_called()
        self.http.get.assert_not_called()

    async def test_error_paths(self):
        for status, body, expected in [(404, {}, '찾지'), (200, [], '응답'), (503, {}, '정보를 받지')]:
            self.http.get.return_value = self.reply(status, body)
            with self.subTest(status=status), self.assertRaisesRegex(f.LookupProblem, expected):
                await self.api.lookup('Test Mage')
        self.http.get.side_effect = asyncio.TimeoutError
        with self.assertRaisesRegex(f.LookupProblem, '잠시 뒤'):
            await self.api.lookup('Test Mage')


class PrivateFlows(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.logs = N(permissions_for=lambda _: N(view_channel=True, read_message_history=True),
                      history=history(record(2, '다른 사람'), record(1, '가짜 기록', bot_id=999),
                                      record(1, '아샤 달빛'), record(1, '옛 기록')))
        guild = N(get_channel=lambda _: self.logs, me=N())
        self.it = N(guild_id=99, guild=guild, channel_id=g.CHANNEL_ID, user=N(id=1, roles=[]),
                    response=AsyncMock(), edit_original_response=AsyncMock(), message=msg(g.DETAILS, 1))
        self.feature = g.GuildApplications(N(user=N(id=1)), 99, 45, 22)
        self.feature.api.lookup = AsyncMock(return_value={'name': '아샤', 'race': {'name': '인간'}, 'faction': {'type': 'ALLIANCE'}})

    async def test_guild_install_no_user_argument(self):
        bot = commands.Bot(command_prefix='!', intents=discord.Intents.none())
        g.install(bot, 99, 45, 22)
        guild = discord.Object(id=99)
        bot.tree.copy_global_to(guild=guild)
        cmd = bot.tree.get_command('내캐릭터', guild=guild)
        self.assertTrue(cmd.guild_only)
        self.assertEqual(cmd.parameters, [])
        await bot.close()

    async def test_personal_private_lookup_and_owner_only_button(self):
        await self.feature.mine(self.it)
        self.feature.api.lookup.assert_awaited_once_with('아샤 달빛')
        self.it.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        answer = self.it.edit_original_response.await_args.kwargs
        self.assertNotIn('다른 사람', answer['content'])
        self.assertIn('진영이 다릅니다', answer['content'])
        self.assertEqual(answer['allowed_mentions'].to_dict(), {'parse': []})
        view = answer['view']
        intruder = N(user=N(id=2), response=AsyncMock())
        self.assertFalse(await view.interaction_check(intruder))
        self.assertTrue(intruder.response.send_message.await_args.kwargs['ephemeral'])
        self.assertTrue(await view.interaction_check(self.it))
        self.feature.api.lookup.return_value = {'equipped_items': [{'slot': {'name': '머리'}, 'name': '모자', 'level': {'value': 47}}]}
        self.it.response.defer.reset_mock()
        await view.children[0].callback(self.it)
        self.it.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        self.assertIn('머리: 모자 · 47', self.it.edit_original_response.await_args.kwargs['embed'].description)
        self.feature.api.lookup.assert_awaited_with('아샤 달빛', equipment=True)

    async def test_dm_or_wrong_guild_denied_before_lookup(self):
        self.feature.signup = AsyncMock()
        for guild_id in (None, 123):
            self.it.guild_id = guild_id
            await self.feature.mine(self.it)
            self.assertTrue(self.it.response.send_message.await_args.kwargs['ephemeral'])
        self.feature.signup.assert_not_awaited()

    async def test_streamer_private_fallback_and_copy_name_without_prefix(self):
        self.it.user = N(id=42, roles=[N(id=22)])
        self.feature.api.lookup.side_effect = f.LookupProblem('프로필을 찾지 못했습니다.')
        await self.feature.details(self.it)
        result = self.it.edit_original_response.await_args.kwargs
        self.assertIn('```\n아샤 달빛\n```', result['content'])
        self.assertIn('호드', result['content'])
        self.assertIn('직접 초대', result['content'])
        self.assertIsNone(result['view'])
        self.it.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_role_and_message_validation_precede_log_lookup(self):
        self.logs.history = Mock(side_effect=AssertionError('log access'))
        await self.feature.details(self.it)
        self.it.user.roles = [N(id=22)]
        self.it.message.author.id = 999
        await self.feature.details(self.it)
        self.assertEqual(self.it.response.send_message.await_count, 2)
        self.assertTrue(self.it.response.send_message.await_args.kwargs['ephemeral'])
        self.feature.api.lookup.assert_not_awaited()

    async def test_no_record_or_log_permissions_no_api(self):
        self.logs.history = history()
        await self.feature.mine(self.it)
        self.assertIn('등업 기록이 없습니다', self.it.edit_original_response.await_args.kwargs['content'])
        self.logs.permissions_for = lambda _: N(view_channel=False, read_message_history=True)
        await self.feature.mine(self.it)
        self.assertIn('권한', self.it.edit_original_response.await_args.kwargs['content'])
        self.feature.api.lookup.assert_not_awaited()

    def test_bad_record_cannot_break_copy_block(self):
        self.assertEqual(f.registered(('호드', '```\n@everyone')).count('```'), 2)
        self.assertNotIn('다릅니다', f.summary({'faction': {'type': 'HORDE'}}, 'Horde'))
        self.assertNotIn('다릅니다', f.summary({}, '호드'))
        self.assertIn('없습니다', f.equipment_embed({}).description)


if __name__ == '__main__':
    unittest.main()

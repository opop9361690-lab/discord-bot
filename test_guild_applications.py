import unittest
from types import SimpleNamespace as N
from unittest.mock import AsyncMock, Mock

import discord
import guild_applications as g


def history(*messages):
    async def read(**kwargs):
        for m in messages:
            yield m
    return read


def record(user_id, name='테스트법사', bot_id=1):
    e = discord.Embed()
    for k, v in [('신청자 계정', f'<@!{user_id}> (예시)'), ('진영', '호드'), ('캐릭터명', name)]:
        e.add_field(name=k, value=v)
    return N(author=N(id=bot_id), embeds=[e])


def msg(button, uid=55):
    return N(content=g.PANEL, components=[N(children=[N(custom_id=button)])], author=N(id=1),
             embeds=[discord.Embed(description=f'신청자: <@{uid}>')])


class Flow(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.role = N(id=7, mention='<@&7>', mentionable=True)
        self.perms = N(view_channel=True, send_messages=True, embed_links=True,
                       read_message_history=True, mention_everyone=False)
        self.channel = N(send=AsyncMock(), history=history(), permissions_for=lambda _: self.perms)
        self.logs = N(history=history(record(555), record(55, bot_id=8), record(55), record(55, '옛이름')),
                      permissions_for=lambda _: self.perms)
        self.guild = N(me=N(), get_channel={4:self.logs, g.CHANNEL_ID:self.channel}.get,
                       get_role=lambda _: self.role)
        self.bot = N(user=N(id=1), get_guild=lambda _: self.guild)
        self.feature = g.GuildApplications(self.bot, 3, 4, 7)
        self.it = N(guild_id=3, channel_id=g.CHANNEL_ID, guild=self.guild, channel=self.channel,
                    user=N(id=55, roles=[]), response=AsyncMock(), edit_original_response=AsyncMock(),
                    message=msg(g.DETAILS))

    async def test_new_public_message_silent_no_mentions_no_character_data(self):
        await self.feature.apply(self.it)
        opts = self.channel.send.await_args.kwargs
        self.assertEqual(opts['allowed_mentions'].to_dict(), {'parse':[]})
        self.assertTrue(opts['silent'])
        self.assertNotIn('@', opts['content'])
        self.assertEqual(opts['embed'].description, '신청자: <@55>')
        self.assertTrue(opts['view'].is_persistent())
        self.it.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)

    async def test_duplicate_after_restart(self):
        self.channel.history = history(msg(g.DETAILS))
        await self.feature.apply(self.it)
        self.channel.send.assert_not_awaited()
        self.assertIn('중복', self.it.edit_original_response.await_args.kwargs['content'])

    async def test_no_ping_permission(self):
        self.role.mentionable = False
        await self.feature.apply(self.it)
        self.channel.send.assert_awaited_once()

    async def test_other_member_not_blocked(self):
        self.channel.history = history(msg(g.DETAILS, 555))
        await self.feature.apply(self.it)
        self.channel.send.assert_awaited_once()

    async def test_wrong_channel(self):
        self.it.channel_id = 0
        await self.feature.apply(self.it)
        self.channel.send.assert_not_awaited()

    async def test_private_info_restricted(self):
        self.logs.history = Mock(side_effect=AssertionError('no log access'))
        await self.feature.details(self.it)
        self.it.response.send_message.assert_awaited_once()
        self.assertTrue(self.it.response.send_message.await_args.kwargs['ephemeral'])

    async def test_exact_id_latest_trusted_record_only(self):
        self.it.user.roles = [self.role]
        await self.feature.details(self.it)
        self.it.response.defer.assert_awaited_once_with(ephemeral=True, thinking=True)
        text = self.it.edit_original_response.await_args.kwargs['content']
        self.assertIn('테스트법사', text)
        self.assertIn('호드', text)
        self.assertNotIn('옛이름', text)
        self.assertIsNone(g.profile(record(555), 1, 55))
        self.assertIsNone(g.profile(record(55, bot_id=8), 1, 55))

    async def test_missing_record(self):
        self.it.user.roles = [self.role]
        self.logs.history = history()
        await self.feature.details(self.it)
        self.assertIn('개별 확인', self.it.edit_original_response.await_args.kwargs['content'])

    async def test_existing_panel_migrates_in_place(self):
        old = msg(g.APPLY)
        old.content, old.edit = '이전 안내', AsyncMock()
        self.channel.history = history(old)
        await self.feature.ensure_panel()
        old.edit.assert_awaited_once()
        self.assertEqual(old.edit.await_args.kwargs['content'], g.PANEL)
        self.channel.send.assert_not_awaited()

    async def test_reconnect_no_duplicate_panel(self):
        await self.feature.ensure_panel()
        self.assertTrue(self.channel.send.await_args.kwargs['view'].is_persistent())
        self.channel.history = history(msg(g.APPLY))
        await self.feature.ensure_panel()
        self.channel.send.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()

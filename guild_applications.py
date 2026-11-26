"""One-click WoW guild applications. Discord history is the durable source of truth."""
import asyncio
import logging
import re
from datetime import timedelta

import discord
from discord import app_commands

from forever_character import EquipmentView, ForeverAPI, LookupProblem, registered, summary


CHANNEL_ID = 1572975822489526343  # 겜코 와우포에버 / ┆길드가입신청
APPLY = 'wowguild:apply:v1'
DETAILS = 'wowguild:details:v1'
COOLDOWN = timedelta(minutes=15)
PANEL = ('**와우 길드 가입 신청**\n버튼을 누르면 이 채널에 신청이 새 메시지로 남습니다. '
         '스트리머에게 별도 알림은 보내지 않습니다.\n'
         '스트리머가 등업 때 입력한 진영과 캐릭터명을 확인한 뒤 게임 안에서 초대합니다.')
logger = logging.getLogger(__name__)


def has_button(message, custom_id):
    return any(getattr(item, 'custom_id', None) == custom_id
               for row in message.components for item in getattr(row, 'children', ()))


def applicant(message, bot_id):
    if message.author.id != bot_id or not has_button(message, DETAILS) or not message.embeds:
        return None
    match = re.fullmatch(r'신청자: <@(\d+)>', message.embeds[0].description or '')
    return int(match[1]) if match else None


def profile(message, bot_id, member_id):
    if message.author.id != bot_id:
        return None
    for embed in message.embeds:
        fields = {f.name: f.value for f in embed.fields}
        if (re.match(rf'^<@!?{member_id}>(?:\s|$)', fields.get('신청자 계정', ''))
                and fields.get('진영') and fields.get('캐릭터명')):
            return fields['진영'], fields['캐릭터명']
    return None


async def latest_profile(channel, bot_id, member_id):
    async for message in channel.history(limit=None, oldest_first=False):
        data = profile(message, bot_id, member_id)
        if data:
            return data
    return None


class ApplyView(discord.ui.View):
    def __init__(self, feature):
        super().__init__(timeout=None)
        self.feature = feature

    @discord.ui.button(label='길드 가입 신청', style=discord.ButtonStyle.success, custom_id=APPLY)
    async def apply(self, interaction, button):
        await self.feature.apply(interaction)


class DetailsView(discord.ui.View):
    def __init__(self, feature):
        super().__init__(timeout=None)
        self.feature = feature

    @discord.ui.button(label='초대 정보 확인', style=discord.ButtonStyle.secondary, custom_id=DETAILS)
    async def details(self, interaction, button):
        await self.feature.details(interaction)


class GuildApplications:
    def __init__(self, bot, guild_id, log_id, role_id):
        self.bot, self.guild_id = bot, guild_id
        self.log_id, self.role_id = log_id, role_id
        self.api = ForeverAPI()
        self.in_flight = set()
        self.panel_lock = asyncio.Lock()

    def right_place(self, interaction):
        return interaction.guild_id == self.guild_id and interaction.channel_id == CHANNEL_ID

    async def ensure_panel(self):
        async with self.panel_lock:
            guild = self.bot.get_guild(self.guild_id)
            channel = guild and guild.get_channel(CHANNEL_ID)
            if not channel:
                logger.warning('길드가입신청 채널을 찾을 수 없습니다.')
                return
            if not channel.permissions_for(guild.me).read_message_history:
                logger.warning('길드가입신청 채널의 메시지 기록 보기 권한이 필요합니다.')
                return
            try:
                async for msg in channel.history(limit=None, oldest_first=True):
                    if msg.author.id == self.bot.user.id and has_button(msg, APPLY):
                        if msg.content != PANEL:
                            await msg.edit(content=PANEL, allowed_mentions=discord.AllowedMentions.none())
                        return
                await channel.send(PANEL, silent=True,
                                   view=ApplyView(self), allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                logger.exception('길드가입신청 패널 확인/설치 실패')

    async def apply(self, it):
        if not self.right_place(it):
            return await it.response.send_message('길드가입신청 채널에서 신청해 주세요.', ephemeral=True)
        await it.response.defer(ephemeral=True, thinking=True)
        member_id = it.user.id
        if member_id in self.in_flight:
            return await it.edit_original_response(content='이미 신청을 접수하고 있습니다.')
        self.in_flight.add(member_id)
        try:
            role = it.guild.get_role(self.role_id)
            perms = it.channel.permissions_for(it.guild.me)
            if not (perms.view_channel and perms.send_messages and perms.embed_links and perms.read_message_history):
                result = '봇의 신청 채널 권한이 부족해 접수하지 않았습니다. 운영진에게 알려 주세요.'
            elif not role:
                result = '초대 정보를 확인할 스트리머 역할이 없어 접수하지 않았습니다. 운영진에게 알려 주세요.'
            else:
                cutoff = discord.utils.utcnow() - COOLDOWN
                async for msg in it.channel.history(limit=None, after=cutoff):
                    if applicant(msg, self.bot.user.id) == member_id:
                        result = '이미 신청이 접수됐습니다. 15분 동안은 중복 신청을 받지 않습니다.'
                        break
                else:
                    notice = discord.Embed(title='길드 초대 요청', description=f'신청자: <@{member_id}>')
                    await it.channel.send(content='새 길드 가입 신청이 있습니다.', silent=True,
                                          embed=notice, view=DetailsView(self),
                                          allowed_mentions=discord.AllowedMentions.none())
                    result = '길드 가입 신청을 접수했습니다. 스트리머가 확인한 뒤 게임 안에서 초대합니다.'
        except discord.HTTPException:
            logger.exception('길드 초대 요청 처리 실패')
            result = '접수 여부를 확인하지 못했습니다. 채널에 초대 요청이 없다면 잠시 뒤 다시 눌러 주세요.'
        finally:
            self.in_flight.discard(member_id)
        await it.edit_original_response(content=result)

    async def signup(self, it, uid):
        logs = it.guild.get_channel(self.log_id)
        if not logs:
            raise LookupProblem('등업 로그 채널을 찾을 수 없습니다. 운영진에게 확인해 주세요.')
        perms = logs.permissions_for(it.guild.me)
        if not (perms.view_channel and perms.read_message_history):
            raise LookupProblem('봇에 등업 로그 읽기 권한이 없습니다. 운영진에게 알려 주세요.')
        try:
            return await asyncio.wait_for(latest_profile(logs, self.bot.user.id, uid), timeout=90)
        except (discord.HTTPException, asyncio.TimeoutError) as exc:
            raise LookupProblem('등업 기록을 지금 읽을 수 없습니다. 잠시 뒤 다시 확인해 주세요.') from exc

    async def character_info(self, it, uid, *, invite=False):
        view = None
        try:
            data = await self.signup(it, uid)
            if not data:
                result = ('등업 기록이 없습니다. 신청자에게 전체 이름과 진영을 개별 확인한 뒤 초대해 주세요.'
                          if invite else '내 계정의 등업 기록이 없습니다. 등업 신청 또는 운영진 확인이 필요합니다.')
            else:
                faction, name = data
                result = registered(data)
                try:
                    result += '\n' + summary(await self.api.lookup(name), faction)
                    if not invite:
                        view = EquipmentView(uid, name, self.api)
                except LookupProblem as exc:
                    result += f'\nAPI 조회: {exc}'
                if invite:
                    result += '\n신청자에게 전체 이름과 진영을 개별 확인한 뒤 게임 안에서 직접 초대해 주세요.'
        except LookupProblem as exc:
            result = str(exc)
            if invite:
                result += ' 신청자에게 전체 이름과 진영을 개별 확인해 주세요.'
        await it.edit_original_response(content=result, view=view, allowed_mentions=discord.AllowedMentions.none())

    async def mine(self, it):
        if it.guild_id != self.guild_id:
            return await it.response.send_message('이 서버에서만 사용할 수 있습니다.', ephemeral=True)
        await it.response.defer(ephemeral=True, thinking=True)
        await self.character_info(it, it.user.id)

    async def details(self, it):
        if not self.right_place(it) or self.role_id not in [r.id for r in it.user.roles]:
            return await it.response.send_message('스트리머 역할만 초대 정보를 확인할 수 있습니다.', ephemeral=True)
        uid = applicant(it.message, self.bot.user.id)
        if uid is None:
            return await it.response.send_message('신청 정보를 확인할 수 없습니다.', ephemeral=True)
        await it.response.defer(ephemeral=True, thinking=True)
        await self.character_info(it, uid, invite=True)


def install(bot, guild_id, log_id, role_id):
    feature = GuildApplications(bot, guild_id, log_id, role_id)
    bot.add_view(ApplyView(feature))
    bot.add_view(DetailsView(feature))
    bot.add_listener(feature.ensure_panel, 'on_ready')

    @bot.tree.command(name='내캐릭터', description='내 등업 기록에 등록한 한국 포에버 캐릭터와 장비를 나만 봅니다.')
    @app_commands.guild_only()
    async def my_character(interaction: discord.Interaction):
        await feature.mine(interaction)

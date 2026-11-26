"""Single-message KR PvP market board. No Discord or backend work unless enabled."""
import asyncio
import logging
import os
import re
from datetime import datetime, timedelta, timezone

LOG = logging.getLogger(__name__)
HOUR = 3600
KST = timezone(timedelta(hours=9))
CHANNEL_NAME = '경매장시세'
DEFAULT_GUILD = 1462029945034375181
# Stable ownership marker, independent of display data and software version.
MARKER = 'KR PvP 경매장 시세판'
FACTIONS = (('alliance', '얼라이언스'), ('horde', '호드'))
STATUS = {'ready': '정상', 'partial': '관측 부족', 'stale': '오래된 자료',
          'unavailable': '자료 없음'}


def clean(value, limit):
    text = ' '.join(str(value or '').split())[:limit]
    return text.translate(str.maketrans('@<>*_~|[]`', '＠＜＞＊＿～｜［］ˋ'))


def stamp(epoch):
    if epoch is None:
        return '관측 없음'
    return datetime.fromtimestamp(epoch, KST).strftime('%Y-%m-%d %H:%M:%S KST')


def money(copper):
    if type(copper) is not int or copper <= 0:
        return '매물 미확인'
    gold, rest = divmod(copper, 10000)
    silver, copper = divmod(rest, 100)
    return f'{gold:,}골 {silver}실 {copper}코'


def faction_value(cell, house):
    state = house.get('status')
    if state not in ('ready', 'partial'):
        return STATUS.get(state, '자료 없음') + ' · 가격/수량 미표시'
    units = cell.get('units')
    qty = '관측 부족'
    if type(units) is int and units >= 0 and (state == 'ready' or units > 0):
        qty = f'{units:,}개' + (' (부분 관측)' if state == 'partial' else '')
    return f"개당 최저 {money(cell.get('min_unit_copper'))}\n24시간 추정 {qty}"


def render(board):
    """Each non-inline item heading starts a fresh pair of inline faction columns.

    Discord wraps inline fields on narrow clients; each retains its faction label.
    """
    houses = board.get('houses') or {}
    lines = [f"상태: {STATUS.get(board.get('status'), '자료 없음')}",
             f"자료 기준: {stamp(board.get('as_of'))}"]
    for key, label in FACTIONS:
        house = houses.get(key) or {}
        state = STATUS.get(house.get('status'), '자료 없음')
        lines.append(f"{label}: {stamp(house.get('as_of'))} · {state}")
    lines += ['24시간 추정 판매수량 순위 (양 진영 합산). 실거래 확정 수량이 아닙니다.',
              clean(board.get('note'), 320)]
    embeds = [{'title': 'KR PvP 경매장 · 진영별 시세', 'description': '\n'.join(filter(None, lines)),
               'color': 0x365B79}]
    for key, title in [('raid', '레이드 준비물'), ('top', '전체 품목')]:
        fields = []
        for rank, row in enumerate((board.get(key) or [])[:5], 1):
            item = clean(row.get('name'), 64) or f"아이템 #{int(row['item_id'])}"
            fields.append({'name': f'{rank}. {item}', 'value': '\u200b', 'inline': False})
            for side, label in FACTIONS:
                fields.append({'name': label, 'inline': True,
                               'value': faction_value(row.get(side) or {}, houses.get(side) or {})})
        section = {'title': f'{title} TOP 5', 'color': 0x365B79}
        if fields:
            section['fields'] = fields
        else:
            section['description'] = '순위를 표시할 관측 자료가 없습니다.'
        embeds.append(section)
    return embeds


def owns(message, bot_id):
    return (message.author.id == bot_id and message.webhook_id is None
            and message.content == MARKER)


class Publisher:
    def __init__(self, bot, guild_id, channel_id):
        self.bot, self.guild_id, self.channel_id = bot, guild_id, channel_id
        self.message_id = None
        self.lock = asyncio.Lock()

    async def channel(self):
        import discord
        ch = await self.bot.fetch_channel(self.channel_id)
        if (not isinstance(ch, discord.TextChannel) or ch.type != discord.ChannelType.text
                or ch.id != self.channel_id or ch.guild.id != self.guild_id
                or not ch.name.endswith(CHANNEL_NAME)):
            raise ValueError('configured channel is not the expected guild text channel')
        perm = ch.permissions_for(ch.guild.me)
        # Without Read Message History Discord may return an EMPTY history, not an error.
        if not (perm.view_channel and perm.read_message_history and perm.send_messages
                and perm.embed_links and getattr(perm, 'pin_messages', perm.manage_messages)):
            raise PermissionError('missing board permissions')
        return ch

    async def publish(self, ch, data):
        import discord
        async with self.lock:
            bot_id = self.bot.user.id
            msg = None
            if self.message_id is not None:
                try:
                    msg = await ch.fetch_message(self.message_id)
                except discord.NotFound as exc:
                    if exc.code != 10008:  # Unknown Message, not Unknown Channel, etc.
                        raise
                if msg is not None and not owns(msg, bot_id):
                    raise ValueError('saved board no longer matches ownership marker')
            if msg is None:
                # Restart, deleted message, lost send response, or send-before-pin crash.
                # An incomplete/failed history scan must never be treated as an empty channel.
                async for candidate in ch.history(limit=None):
                    if owns(candidate, bot_id):
                        msg = candidate
                        break
            opts = dict(embeds=[discord.Embed.from_dict(d) for d in render(data)],
                        allowed_mentions=discord.AllowedMentions.none())
            if msg is None:
                # discord.py sets enforce_nonce on keyed sends. Also guards short
                # overlaps between workers and HTTP retries with a lost response.
                msg = await ch.send(MARKER, silent=True, nonce=str(self.channel_id), **opts)
            else:
                await msg.edit(**opts)
            self.message_id = msg.id  # A failed pin must not create another post.
            if not msg.pinned:
                await msg.pin(reason='경매장 시세판 고정')


def make_service():
    from forever_market import MarketService
    return MarketService.from_env()


async def run(bot, guild_id, channel_id):
    pub = Publisher(bot, guild_id, channel_id)
    market = None
    while not bot.is_closed():
        try:
            await bot.wait_until_ready()
            ch = await pub.channel()
            try:
                if market is None:
                    market = make_service()
                failed = False
                try:
                    await market.refresh()
                except Exception as exc:
                    LOG.warning('market refresh deferred (%s)', type(exc).__name__)
                    failed = True
                board = await market.board()
                if failed:
                    board = dict(board, note='이번 수집 실패. 마지막 관측 자료입니다. ' + str(board.get('note') or ''))
            except Exception as exc:
                LOG.warning('market data unavailable (%s)', type(exc).__name__)
                board = {'status': 'unavailable', 'note': '수집 상태를 확인할 수 없습니다.'}
            await pub.publish(ch, board)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning('market board update deferred (%s)', type(exc).__name__)
        await asyncio.sleep(HOUR)


def install(bot, guild_id=DEFAULT_GUILD):
    """Synchronous setup_hook entry point. Register once; never block bot startup."""
    raw = os.environ.get('AUCTION_CHANNEL_ID', '').strip()
    if not re.fullmatch(r'[1-9][0-9]{0,19}', raw) or int(raw) >= 2**64:
        return
    attr = '_forever_market_board_ready'
    if getattr(bot, attr, None) is not None:
        return
    task = None

    async def ready():
        nonlocal task
        if task is None or task.done():
            task = asyncio.create_task(run(bot, guild_id, int(raw)), name='auction-market-board')

    bot.add_listener(ready, 'on_ready')
    setattr(bot, attr, ready)

"""Offline contracts for the Discord board; no client library, credentials or network."""
import asyncio
import os
import sys
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import market_board as m


def sample():
    house = {'status': 'ready', 'as_of': 0}
    row = {'item_id': 1, 'name': '@everyone **물약**',
           'alliance': {'min_unit_copper': 10203, 'units': 20},
           'horde': {'min_unit_copper': None, 'units': 0}}
    return {'as_of': 0, 'status': 'partial', 'houses': {'alliance': house,
            'horde': dict(house, status='partial')}, 'note': '<@1>',
            'top': [row] * 6, 'raid': [row] * 6}


class DisplayTest(unittest.TestCase):
    def test_aligned_rows_mobile_labels_top_five_and_time(self):
        overview, raid, top = m.render(sample())
        self.assertIn('1970-01-01 09:00:00 KST', overview['description'])
        self.assertNotIn('@', str([overview, raid, top]))
        self.assertIn('레이드', raid['title'])
        self.assertIn('전체', top['title'])
        for group in (raid, top):
            fields = group['fields']
            self.assertEqual(len(fields), 15)
            for i in range(0, 15, 3):
                heading, a, h = fields[i:i+3]
                self.assertFalse(heading['inline'])
                self.assertTrue(a['inline'] and h['inline'])
                self.assertEqual([a['name'], h['name']], ['얼라이언스', '호드'])
                self.assertIn('1골 2실 3코', a['value'])
                self.assertIn('20개', a['value'])
                self.assertIn('관측 부족', h['value'])
                self.assertNotIn('0개', h['value'])

    def test_stale_unavailable_and_partial_do_not_become_zero(self):
        cell = {'units': 0, 'min_unit_copper': 12345}
        for state in ['stale', 'unavailable', 'partial', None]:
            self.assertNotIn('0개', m.faction_value(cell, {'status': state}))
        self.assertIn('0개', m.faction_value(cell, {'status': 'ready'}))
        self.assertIn('부분 관측', m.faction_value(dict(cell, units=4), {'status': 'partial'}))
        self.assertNotIn('골', m.faction_value(cell, {'status': 'stale'}))
        self.assertIn('관측 없음', m.render({})[0]['description'])
        self.assertNotIn('fields', m.render({})[2])


class NotFound(Exception):
    def __init__(self, code):
        self.code = code


class Channel:
    pass


class PublisherTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.discord = NS(NotFound=NotFound, Embed=NS(from_dict=lambda d: d),
                          AllowedMentions=NS(none=lambda: 'none'), TextChannel=Channel,
                          ChannelType=NS(text='text'))
        p = patch.dict(sys.modules, discord=self.discord)
        p.start()
        self.addCleanup(p.stop)
        self.ch = ch = Channel()
        self.bot = NS(user=NS(id=11), fetch_channel=AsyncMock(return_value=ch))
        self.pub = m.Publisher(self.bot, 12, 13)
        ch.id, ch.name, ch.type, ch.guild = 13, '┆경매장시세', 'text', NS(id=12, me=object())
        self.perm = NS(view_channel=True, read_message_history=True, send_messages=True,
                       manage_messages=True, embed_links=True, pin_messages=True)
        ch.permissions_for = MagicMock(return_value=self.perm)
        self.messages = []

        async def history(*, limit):
            self.assertIsNone(limit)
            for msg in reversed(self.messages):
                yield msg

        async def send(content, **kwargs):
            msg = NS(id=len(self.messages)+1, content=content, author=self.bot.user,
                     pinned=False, webhook_id=None, edit=AsyncMock())
            msg.pin = AsyncMock(side_effect=lambda **kw: setattr(msg, 'pinned', True))
            self.messages.append(msg)
            return msg

        ch.history = history
        ch.send = AsyncMock(side_effect=send)
        ch.fetch_message = AsyncMock(side_effect=lambda n: next(x for x in self.messages if x.id == n))

    async def test_publish_edit_restart_and_silence(self):
        self.assertIs(await self.pub.channel(), self.ch)
        await self.pub.publish(self.ch, sample())
        msg, = self.messages
        self.assertTrue(msg.pinned)
        opts = self.ch.send.await_args.kwargs
        self.assertTrue(opts['silent'])
        self.assertEqual(opts['nonce'], '13')
        self.assertEqual(opts['allowed_mentions'], 'none')
        await self.pub.publish(self.ch, sample())
        await m.Publisher(self.bot, 12, 13).publish(self.ch, sample())
        self.ch.send.assert_awaited_once()
        msg.pin.assert_awaited_once()
        self.assertEqual(msg.edit.await_count, 2)
        self.assertEqual(msg.edit.await_args.kwargs['allowed_mentions'], 'none')

    async def test_lost_send_response_and_pin_failure_recover_without_posts(self):
        normal_send = self.ch.send.side_effect

        async def lost(*a, **kw):
            await normal_send(*a, **kw)
            raise TimeoutError()

        self.ch.send.side_effect = lost
        with self.assertRaises(TimeoutError):
            await self.pub.publish(self.ch, {})
        msg, = self.messages
        normal_pin = msg.pin.side_effect
        msg.pin.side_effect = PermissionError()
        with self.assertRaises(PermissionError):
            await self.pub.publish(self.ch, {})
        msg.pin.side_effect = normal_pin
        await m.Publisher(self.bot, 12, 13).publish(self.ch, {})
        self.assertTrue(msg.pinned)
        self.ch.send.assert_awaited_once()

    async def test_history_lookup_edit_failure_never_sends_copy(self):
        async def truncated(*, limit):
            yield NS(author=NS(id=123), content=m.MARKER, webhook_id=None)
            raise PermissionError()
        self.ch.history = truncated
        with self.assertRaises(PermissionError):
            await self.pub.publish(self.ch, {})
        self.ch.send.assert_not_awaited()
        msg = await self.ch.send(m.MARKER)
        self.pub.message_id = msg.id
        msg.edit.side_effect = TimeoutError()
        with self.assertRaises(TimeoutError):
            await self.pub.publish(self.ch, {})
        self.ch.fetch_message.side_effect = NotFound(10003)
        with self.assertRaises(NotFound):
            await self.pub.publish(self.ch, {})
        self.ch.send.assert_awaited_once()

    async def test_foreign_marker_and_other_bot_messages_not_edited(self):
        for extra in [dict(author=NS(id=222)), dict(webhook_id=22), dict(content='unrelated')]:
            msg = await self.ch.send(m.MARKER)
            for k, v in extra.items():
                setattr(msg, k, v)
        await self.pub.publish(self.ch, {})
        self.assertEqual(len(self.messages), 4)
        for msg in self.messages[:3]:
            msg.edit.assert_not_awaited()
            msg.pin.assert_not_awaited()

    async def test_invalid_destination_and_permissions_fail_closed(self):
        for obj, field, bad in [(self.ch, 'type', 'news'), (self.ch, 'name', '다른채널'),
                                (self.ch, 'id', 999), (self.ch.guild, 'id', 999),
                                (self.perm, 'read_message_history', False),
                                (self.perm, 'pin_messages', False)]:
            with patch.object(obj, field, bad), self.assertRaises((ValueError, PermissionError)):
                await self.pub.channel()
        self.ch.send.assert_not_awaited()

    async def test_install_disabled_and_reconnect_single_worker(self):
        bot = NS(add_listener=MagicMock())
        for val in ['', '0', '-3', 'abc', str(2**64)]:
            with patch.dict(os.environ, AUCTION_CHANNEL_ID=val):
                m.install(bot)
        bot.add_listener.assert_not_called()
        with patch.dict(os.environ, AUCTION_CHANNEL_ID='13'):
            m.install(bot, 12)
            m.install(bot, 12)
        bot.add_listener.assert_called_once()
        gate = asyncio.Event()
        async def wait(*args):
            await gate.wait()
        with patch.object(m, 'run', new=AsyncMock(side_effect=wait)) as worker:
            ready = bot.add_listener.call_args.args[0]
            await asyncio.gather(ready(), ready())
            await asyncio.sleep(0)
            worker.assert_awaited_once_with(bot, 12, 13)
            gate.set()
            await asyncio.sleep(0)

    async def test_hourly_worker_fails_soft_with_no_fabricated_sales(self):
        market = NS(refresh=AsyncMock(side_effect=TimeoutError()), board=AsyncMock(side_effect=ValueError()))
        self.bot.wait_until_ready = AsyncMock()
        self.bot.is_closed = MagicMock(side_effect=[False, True])
        with patch.object(m, 'make_service', return_value=market), patch.object(m.asyncio, 'sleep', new_callable=AsyncMock) as wait:
            await m.run(self.bot, 12, 13)
        wait.assert_awaited_once_with(3600)
        self.ch.send.assert_awaited_once()
        self.assertIn('자료 없음', self.ch.send.await_args.kwargs['embeds'][0]['description'])


if __name__ == '__main__':
    unittest.main()

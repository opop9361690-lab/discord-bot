"""Offline tests. No secrets, Discord client, aiohttp, or network required."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, MagicMock, patch

import youtube_alerts as y

C, V, NOW = 'UC' + 'x'*22, 'a'*11, 1700000000


@contextmanager
def workdir():
    root = Path(os.environ.get('YOUTUBE_TEST_TMP') or tempfile.gettempdir())
    d = root / ('yt-test-' + uuid.uuid4().hex)
    d.mkdir()
    try:
        yield d
    finally:
        shutil.rmtree(d)


def iso(n):
    return datetime.fromtimestamp(n, timezone.utc).isoformat()


def row(kind='none', at=NOW, **ls):
    return {'id': V, 'status': {'privacyStatus': 'public'},
            'snippet': {'channelId': C, 'title': '@everyone **a**',
                        'publishedAt': iso(at), 'liveBroadcastContent': kind},
            'liveStreamingDetails': ls}


class Rules(unittest.TestCase):
    def setUp(self):
        self.s = y.fresh_state(C)

    def runrow(self, r, start=NOW-10, now=NOW):
        return y.reconcile(self.s, [r], C, start, now)

    def test_upload_and_duplicate(self):
        self.assertEqual(self.runrow(row())[0][2], 'upload')
        self.s['done'][V] = NOW
        self.assertFalse(self.runrow(row()))

    def test_cold_baseline_and_expiry(self):
        self.assertFalse(self.runrow(row(), NOW))
        self.assertFalse(self.runrow(row(at=NOW-4000), NOW-5000))
        self.assertFalse(self.runrow(row(at=NOW+30)))
        self.assertFalse(self.runrow(row('live', actualStartTime=iso(NOW-100)), NOW))

    def test_upcoming_retained_until_live(self):
        self.assertFalse(self.runrow(row('upcoming', scheduledStartTime=iso(NOW-20))))
        self.assertIn(V, self.s['watch'])
        self.assertFalse(self.runrow(row('live')))
        self.assertFalse(self.runrow(row()))
        self.assertEqual(self.runrow(row('live', at=NOW-9000, actualStartTime=iso(NOW)))[0][2], 'live')

    def test_restart_pending_revalidated_and_ended_dropped(self):
        r = row('live', actualStartTime=iso(NOW))
        self.runrow(r)
        self.assertTrue(self.runrow(r, NOW+2, NOW+5))
        r['liveStreamingDetails']['actualEndTime'] = iso(NOW+3)
        self.assertFalse(self.runrow(r, NOW+2, NOW+5))
        self.assertFalse(self.s['pending'])
        self.assertIn(V, self.s['done'])

    def test_private_foreign_archived(self):
        r = row(); r['status']['privacyStatus'] = 'unlisted'
        self.assertFalse(self.runrow(r))
        r = row(); r['snippet']['channelId'] = 'other'
        self.assertFalse(self.runrow(r))
        self.assertFalse(self.runrow(row('none', actualEndTime=iso(NOW))))

    def test_local_state_scope_corruption_and_expiry(self):
        self.runrow(row())
        with workdir() as t:
            p = Path(t)/'state.json'; y.save_state(p, self.s)
            self.assertEqual(y.load_state(p, C), self.s)
            self.assertFalse(y.load_state(p, 'other')['pending'])
            p.write_text('{', encoding='utf-8')
            self.assertFalse(y.load_state(p, C)['pending'])
        self.assertFalse(self.runrow(row(), NOW-5, NOW+4000))
        self.assertFalse(self.s['pending'])


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_listener_env_and_multiple_ready(self):
        b = NS(add_listener=MagicMock()); gate = asyncio.Event()
        async def worker(*a): await gate.wait()
        with patch.object(y, '_run', new=AsyncMock(side_effect=worker)) as run:
            y.install(b); y.install(b); b.add_listener.assert_called_once()
            ready, name = b.add_listener.call_args.args
            self.assertEqual(name, 'on_ready')
            with patch.dict(os.environ, {}, clear=True):
                await ready(); run.assert_not_called()
                os.environ.update(YOUTUBE_CHANNEL_ID=C, DISCORD_NOTIFY_CHANNEL_ID='123', YOUTUBE_API_KEY='dummy')
                await asyncio.gather(ready(), ready()); await asyncio.sleep(0)
                await ready(); run.assert_awaited_once(); gate.set(); await asyncio.sleep(0)

    async def test_sources_and_official_batches(self):
        async def body(sess, url, params, key=None):
            if url == y.RSS:
                return f'<feed xmlns:yt="{y.YT[1:-1]}"><yt:videoId>{V}</yt:videoId></feed>'.encode()
            self.assertEqual(params['playlistId'], 'UU'+C[2:]); self.assertEqual(key, 'dummy')
            return json.dumps({'items': [{'contentDetails': {'videoId': 'z'*11}}]}).encode()
        with patch.object(y, 'read_body', side_effect=body):
            ids, ok = await y.discover(None, C, 'dummy'); self.assertTrue(ok)
            self.assertEqual(ids, {V, 'z'*11})
        with patch.object(y, 'read_body', side_effect=[ValueError(), b'{"items":[]}']):
            self.assertEqual(await y.discover(None, C, 'dummy'), (set(), True))
        with patch.object(y, 'read_body', return_value=b'{"items":[]}') as rd:
            await y.details(None, {str(i).zfill(11) for i in range(51)}, 'dummy')
            self.assertEqual(rd.await_count, 2)
            self.assertEqual([len(a.args[2]['id'].split(',')) for a in rd.await_args_list], [50, 1])

    async def test_poll_retries_send_rechecks_hidden_stream_and_blocks_mentions(self):
        bot = NS(is_closed=MagicMock(side_effect=[False]*4+[True]), wait_until_ready=AsyncMock())
        send = AsyncMock(side_effect=[RuntimeError('transient'), None]); bot.get_channel = lambda _: NS(send=send)
        dc = NS(utils=NS(escape_markdown=lambda s:s), AllowedMentions=NS(none=lambda:'no mentions'))
        ah = NS(ClientSession=lambda **kw: MagicMock(), ClientTimeout=lambda **kw: None)
        dates = iter([NOW, NOW+2, NOW+4, NOW+5, NOW+6])
        r0 = row('upcoming', scheduledStartTime=iso(NOW+1)); r1 = row('live', actualStartTime=iso(NOW+1))
        async def fetch(_s, ids, _k):
            fetch.n += 1
            if fetch.n <= 3: self.assertIn(V, ids)
            else: self.assertNotIn(V, ids)
            return ([r0] if fetch.n == 1 else [r1]) if ids else []
        fetch.n = 0
        with workdir() as t, patch.dict(sys.modules, aiohttp=ah, discord=dc), \
             patch.object(y, 'time', NS(time=lambda:next(dates))), patch.object(y.asyncio, 'sleep', new=AsyncMock()), \
             patch.object(y, 'details', side_effect=fetch), patch.object(y, 'discover', side_effect=[({V}, True)]+[(set(), True)]*3):
            p = Path(t)/'state'; await y._run(bot, C, 123, 'dummy', p)
            self.assertEqual(send.await_count, 2)
            self.assertEqual(send.await_args.kwargs['allowed_mentions'], 'no mentions')
            self.assertIn('라이브 시작', send.await_args.args[0]); self.assertIn(V, y.load_state(p, C+':123')['done'])


if __name__ == '__main__':
    unittest.main()

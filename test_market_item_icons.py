"""Offline tests: no credentials, network, files, or discord.py required."""
import asyncio
import io
import json
import threading
import time
import unittest
import urllib.error
from email.message import Message
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import market_item_icons as m

URL = 'https://render.worldofwarcraft.com/forever-kr/icons/56/inv_potion_76.jpg'
JPEG = b'\xff\xd8\xff' + b'example' + b'\xff\xd9'


def board(*ids):
    return {'top': [{'item_id': i} for i in ids]}


class Reply(io.BytesIO):
    def __init__(self, data, kind='image/jpeg', status=200, **headers):
        super().__init__(data)
        self.status = status
        self.headers = Message()
        self.headers['Content-Type'] = kind
        for k, v in headers.items():
            self.headers[k] = str(v)


def doc(item, url=URL):
    return Reply(json.dumps({'id': item, 'assets': [{'key': 'icon', 'value': url}]}).encode(), 'application/json')


class Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = SimpleNamespace(api=m.Blizzard('fake-id', 'fake-secret'))
        self.calls = []
        self.on_media = lambda req, i: doc(i)
        self.on_cdn = lambda: Reply(JPEG)
        def open_request(req, timeout):
            self.calls.append(req)
            assert 0 < timeout <= m.IO_TIMEOUT
            u = urlsplit(req.full_url)
            if u.hostname == 'oauth.battle.net':
                assert req.get_header('Authorization').startswith('Basic ')
                return Reply(b'{"access_token":"mock","expires_in":3600}', 'application/json')
            if u.hostname == 'kr.api.blizzard.com':
                assert req.get_header('Authorization') == 'Bearer mock'
                assert u.path.startswith('/data/wow/media/item/') and 'locale=ko_KR' in u.query
                return self.on_media(req, int(u.path.split('/')[-1]))
            assert u.hostname == m.HOST and not req.has_header('Authorization')
            return self.on_cdn()
        self.opener = patch('urllib.request.OpenerDirector.open', side_effect=open_request)
        self.opener.start()
        self.addCleanup(self.opener.stop)

    async def test_one_request_per_visible_item_and_shared_download(self):
        b = {**board(2, 1, True, '3', -1, 91), 'raid': board(1, 2, 3, 3, 3, 90)['top']}
        result = await asyncio.gather(*(m.item_icons(self.service, b) for _ in range(5)))
        self.assertEqual(result, [{1: JPEG, 2: JPEG, 3: JPEG}] * 5)
        self.assertEqual(len(self.calls), 1 + 3 + 1)
        self.assertEqual(self.service.api.token, '')  # unrelated market refresh token unchanged
        self.assertEqual(await m.item_icons(self.service, board(1)), {1: JPEG})
        self.assertEqual(len(self.calls), 5)

    async def test_missing_setup_or_empty_board(self):
        self.assertEqual(await m.item_icons(None, board(1)), {})
        self.assertEqual(await m.item_icons(self.service, {'raid': [None]}), {})
        self.assertFalse(self.calls)

    async def test_404_fallback_and_partial_failure(self):
        def route(req, item):
            if item == 3:
                return doc(4)  # never associate the wrong item's image
            if item == 2 or 'static-forever-kr' in req.full_url:
                raise urllib.error.HTTPError(req.full_url, 404 if item == 1 else 503, 'failure', {}, None)
            return doc(item)
        self.on_media = route
        self.assertEqual(await m.item_icons(self.service, board(1, 2, 3)), {1: JPEG})
        self.assertEqual(sum('static-classic1x-kr' in r.full_url for r in self.calls), 1)

    async def test_expiry_and_cache_bound(self):
        with patch.object(m, 'CACHE_SIZE', 2):
            self.assertEqual(await m.item_icons(self.service, board(1, 2)), {1: JPEG, 2: JPEG})
            await m.item_icons(self.service, board(3))
            state = self.service._market_item_icon_state
            self.assertEqual(list(state.cache), [2, 3])
            self.assertLessEqual(state.cache[2][0] - time.monotonic(), m.HIT_TTL)
            self.on_cdn = lambda: Reply(b'not an image')
            state.cache[2] = (0, JPEG)
            self.assertEqual(await m.item_icons(self.service, board(2)), {})
            count = len(self.calls)
            self.assertEqual(await m.item_icons(self.service, board(2)), {})
            self.assertEqual(len(self.calls), count)
            self.assertLessEqual(state.cache[2][0] - time.monotonic(), m.MISS_TTL)

    async def test_deadline_returns_partial_without_starting_another_worker(self):
        release, entered = threading.Event(), threading.Event()
        def work(_api, item, _urls, _deadline):
            if item == 2:
                entered.set()
                release.wait(3)
            return JPEG
        with patch.object(m, '_resolve', side_effect=work) as fetch, patch.object(m, 'BUDGET', .15):
            try:
                result = await asyncio.wait_for(m.item_icons(self.service, board(1, 2)), 1.5)
                self.assertTrue(entered.is_set())
                self.assertEqual(result, {1: JPEG})
                self.assertEqual(await m.item_icons(self.service, board(2)), {})
                self.assertEqual(fetch.call_count, 2)
            finally:
                release.set()
                await self.service._market_item_icon_state.pending

    async def test_image_and_metadata_validation(self):
        cases = [lambda: Reply(b'not-a-jpeg'), lambda: Reply(JPEG, 'text/html'),
                 lambda: Reply(JPEG, status=302), lambda: Reply(JPEG, **{'Content-Encoding': 'gzip'}),
                 lambda: Reply(JPEG, **{'Content-Length': m.ICON_LIMIT+1}),
                 lambda: Reply(JPEG, **{'Content-Length': len(JPEG)+1}),
                 lambda: Reply(JPEG + b'x' * m.ICON_LIMIT)]
        for i, make_reply in enumerate(cases, 1):
            with self.subTest(i=i):
                self.on_cdn = make_reply
                self.assertEqual(await m.item_icons(self.service, board(i)), {})
        self.on_media = lambda req, item: Reply(b' ' * (m.JSON_LIMIT+1), 'application/json')
        self.assertEqual(await m.item_icons(self.service, board(99)), {})

    def test_allowlist(self):
        self.assertEqual(m._cdn_url(URL), URL)
        self.assertEqual(m._cdn_url('https://render-kr.worldofwarcraft.com/icons/56/x.jpg'),
                         'https://render.worldofwarcraft.com/kr/icons/56/x.jpg')
        for raw in [URL.replace('https:', 'http:'), URL.replace('.com/', '.com.example/'),
                    URL.replace('render.', 'user@render.'), URL.replace('.com/', '.com:8443/'),
                    URL.replace('/icons/', '/../icons/'), URL+'?access_token=x', URL+'#f',
                    URL.replace('inv_potion_76', '%2e%2e'), '\n'+URL, 'https://[::1', None]:
            with self.subTest(url=raw):
                self.assertIsNone(m._cdn_url(raw))


if __name__ == '__main__':
    unittest.main()

"""Optional Blizzard artwork for the ten visible Forever auction board rows.

A separate, serial Blizzard client keeps token refreshes independent of the
auction collector. CDN requests never use its authenticated request method.
"""
import asyncio
import json
import re
import time
import urllib.error
import urllib.request
from collections import OrderedDict
from urllib.parse import urlsplit

from forever_market import Blizzard

# Current render host; legacy regional hosts now redirect to /{region}/.
# https://render.worldofwarcraft.com/forever-kr/icons/56/inv_potion_76.jpg
HOST = 'render.worldofwarcraft.com'
REGIONS = ('us', 'eu', 'kr', 'tw')
ICON_PATH = re.compile(r'/(?:[a-zA-Z0-9_-]+/)*icons/[0-9]{1,3}/[a-zA-Z0-9_.-]+\.(?:jpe?g|png)', re.I)
JSON_LIMIT, ICON_LIMIT = 32 * 1024, 64 * 1024
CACHE_SIZE, HIT_TTL, MISS_TTL = 96, 24 * 3600, 300  # <= 6 MiB of image payload
BUDGET, IO_TIMEOUT = 15.0, 3.0


def _cdn_url(raw):
    if (not isinstance(raw, str) or not raw.isascii() or len(raw) > 1024 or
            not raw.isprintable() or not raw.lower().startswith('https://')):
        return None
    try:
        u = urlsplit(raw)
    except ValueError:
        return None
    if u.query or u.fragment or not ICON_PATH.fullmatch(u.path):
        return None
    host = u.netloc.lower()
    if host == HOST:
        return 'https://' + HOST + u.path
    for region in REGIONS:
        if host == f'render-{region}.worldofwarcraft.com':
            return f'https://{HOST}/{region}{u.path}'
    return None


def _read(http, req, deadline, types, limit):
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError
    with http.open(req, timeout=min(IO_TIMEOUT, remaining)) as r:
        kind = r.headers.get_content_type()
        if (r.status != 200 or kind not in types or
                r.headers.get('Content-Encoding', 'identity').lower() != 'identity'):
            raise ValueError('unexpected media response')
        length = r.headers.get('Content-Length')
        if length is not None and not 0 < int(length) <= limit:
            raise ValueError('invalid media length')
        data = bytearray()
        while len(data) <= limit and time.monotonic() < deadline:
            part = r.read1(min(16384, limit + 1 - len(data)))
            if not part:
                if length is not None and len(data) != int(length):
                    raise ValueError('truncated media')
                return bytes(data), kind
            data.extend(part)
        raise ValueError('media response exceeded budget')


class _MediaClient(Blizzard):
    """Reuse the OAuth flow and no-redirect opener, not auction-sized I/O limits."""
    def request(self, url, auth, data=None):
        req = urllib.request.Request(url, data=data, headers={
            'Authorization': auth, 'Accept': 'application/json', 'Accept-Encoding': 'identity'})
        raw, _ = _read(self.http, req, self.deadline, {'application/json'}, JSON_LIMIT)
        return json.loads(raw), {}


def _resolve(api, item, urls, deadline):
    api.deadline = deadline
    for namespace in ('static-forever-kr', 'static-classic1x-kr'):
        try:
            doc, _ = api.get(f'/data/wow/media/item/{item}', namespace, 'ko_KR')
        except urllib.error.HTTPError as exc:
            exc.close()
            if exc.code == 404:
                continue
            raise
        if type(doc.get('id')) is not int or doc['id'] != item:
            return None
        for asset in doc.get('assets', []):
            if not isinstance(asset, dict) or asset.get('key') != 'icon':
                continue
            url = _cdn_url(asset.get('value'))
            if url is None:
                continue
            if url not in urls:
                urls[url] = None  # even shared failures are downloaded only once per batch
                req = urllib.request.Request(url, headers={
                    'Accept': 'image/jpeg,image/png', 'Accept-Encoding': 'identity'})
                raw, kind = _read(api.http, req, deadline, {'image/jpeg', 'image/png'}, ICON_LIMIT)
                if ((kind == 'image/jpeg' and raw.startswith(b'\xff\xd8\xff') and raw.endswith(b'\xff\xd9')) or
                        (kind == 'image/png' and raw.startswith(b'\x89PNG\r\n\x1a\n') and raw.endswith(b'IEND\xaeB\x60\x82'))):
                    urls[url] = raw
            return urls[url]
        return None
    return None


class _State:
    def __init__(self, source):
        self.api = _MediaClient(source.client, source.secret)
        self.cache = OrderedDict()
        self.pending = None

    def prune(self):
        now = time.monotonic()
        for item, (until, _) in list(self.cache.items()):
            if until <= now:
                del self.cache[item]

    async def fill(self, items, deadline):
        urls = {}
        for item in items:
            if time.monotonic() >= deadline:
                break
            try:
                data = await asyncio.to_thread(_resolve, self.api, item, urls, deadline)
            except urllib.error.HTTPError as exc:
                exc.close()
                data = None
            except Exception:
                # Art is optional; never expose credentials or fail the board.
                data = None
            self.cache[item] = (time.monotonic() + (HIT_TTL if data else MISS_TTL), data)
            if len(self.cache) > CACHE_SIZE:
                self.cache.popitem(last=False)


async def item_icons(service, board) -> dict[int, bytes]:
    """Return available art, keyed by positive item ID, within a wall-clock budget."""
    ids = {}
    for key in ('raid', 'top'):
        rows = board.get(key) if isinstance(board, dict) else None
        if isinstance(rows, (tuple, list)):
            for row in rows[:5]:
                item = row.get('item_id') if isinstance(row, dict) else None
                if type(item) is int and 0 < item < 2**63:
                    ids[item] = None
    source = getattr(service, 'api', None)
    if not ids or not getattr(source, 'client', None) or not getattr(source, 'secret', None):
        return {}
    state = getattr(service, '_market_item_icon_state', None)
    if state is None:
        service._market_item_icon_state = state = _State(source)
    deadline = time.monotonic() + BUDGET
    state.prune()
    while time.monotonic() < deadline:
        missing = [i for i in ids if i not in state.cache]
        if not missing:
            break
        if state.pending is None or state.pending.done():
            state.pending = asyncio.create_task(state.fill(missing, deadline))
        # A timed-out/cancelled caller doesn't release a client still in use
        # by a worker thread. Overlapping callers join that same worker.
        done, _ = await asyncio.wait([state.pending], timeout=max(0, deadline - time.monotonic()))
        if not done:
            break
    return {i: state.cache[i][1] for i in ids if i in state.cache and state.cache[i][1]}

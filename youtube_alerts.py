"""Public YouTube upload/live notices for an existing discord.ext.commands.Bot."""
import asyncio
import json
import logging
import os
import re
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

LOG = logging.getLogger(__name__)
VID = re.compile(r'[A-Za-z0-9_-]{11}\Z')
CID = re.compile(r'UC[A-Za-z0-9_-]{22}\Z')
API = 'https://www.googleapis.com/youtube/v3/'
RSS = 'https://www.youtube.com/feeds/videos.xml'
YT = '{http://www.youtube.com/xml/schemas/2015}'
POLL, FRESH = 120, 3600
DAY = 86400


def timestamp(text):
    try:
        d = datetime.fromisoformat(text.replace('Z', '+00:00'))
        return d.timestamp() if d.tzinfo else 0
    except (ValueError, TypeError, AttributeError, OverflowError):
        return 0


def classify(row):
    sn = row['snippet']
    ls = row.get('liveStreamingDetails') or {}
    flag = sn.get('liveBroadcastContent')
    if ls.get('actualEndTime'):
        return 'ended', 0
    if flag == 'live' and timestamp(ls.get('actualStartTime')):
        return 'live', timestamp(ls['actualStartTime'])
    if flag in ('live', 'upcoming') or ls:
        return 'watch', 0
    if flag == 'none':
        return 'upload', timestamp(sn.get('publishedAt'))
    return 'unknown', 0


def fresh_state(scope):
    return dict(version=1, scope=scope, done={}, watch={}, pending={})


def load_state(file, scope):
    try:
        s = json.loads(file.read_text(encoding='utf-8'))
        if s['version'] != 1 or s['scope'] != scope:
            return fresh_state(scope)
        for name in ('done', 'watch', 'pending'):
            assert_map = s[name]
            if not isinstance(assert_map, dict):
                raise ValueError('bad map')
            for vid, val in assert_map.items():
                if not VID.fullmatch(vid):
                    raise ValueError('bad ID')
                if name == 'pending':
                    if not isinstance(val, list) or len(val) != 2 or val[0] not in ('live', 'upload'):
                        raise ValueError('bad event')
                    val = val[1]
                if not isinstance(val, (int, float)) or not 0 < val < 10**11:
                    raise ValueError('bad time')
        return s
    except FileNotFoundError:
        pass
    except (OSError, ValueError, TypeError, KeyError, UnicodeError):
        LOG.warning('YouTube state unavailable; using quiet startup baseline')
    return fresh_state(scope)


def save_state(file, state):
    tmp = None
    try:
        file.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=file.parent,
                                         prefix='.yt-state-', delete=False) as out:
            tmp = out.name
            json.dump(state, out, separators=(',', ':'))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, file)
    finally:
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def reconcile(state, rows, channel, baseline, now):
    done, watch, pending = (state[k] for k in ('done', 'watch', 'pending'))
    for table, ttl in ((watch, 30 * DAY), (done, 7 * DAY)):
        for vid, seen in list(table.items()):
            if now - seen > ttl:
                del table[vid]
    for vid, (_, started) in list(pending.items()):
        if now - started > FRESH:
            del pending[vid]
    send = []
    for v in rows:
        vid, sn = v.get('id', ''), v.get('snippet') or {}
        if not VID.fullmatch(vid) or sn.get('channelId') != channel or vid in done:
            continue
        if v.get('status', {}).get('privacyStatus') != 'public':
            continue
        kind, at = classify(v)
        if kind == 'watch':
            watch[vid] = now
        elif kind == 'ended':
            done[vid] = now
            pending.pop(vid, None)
            watch.pop(vid, None)
        elif kind in ('upload', 'live'):
            # Never turn a known broadcast into a normal upload notice.
            if kind == 'upload' and vid in watch:
                continue
            retry = pending.get(vid) == [kind, at]
            if at <= 0 or at > now or now - at > FRESH or (at <= baseline and not retry):
                continue
            pending[vid] = [kind, at]
            send.append((at, vid, kind, sn.get('title', '')))
    return sorted(send)


class Upstream(Exception):
    """Contains status only, never request headers or secret keys."""


async def read_body(session, url, params, key=None):
    headers = {'X-Goog-Api-Key': key} if key else {}
    async with session.get(url, params=params, headers=headers, allow_redirects=False) as r:
        if r.status != 200:
            raise Upstream(str(r.status))
        data = bytearray()
        async for chunk in r.content.iter_chunked(65536):
            data.extend(chunk)
            if len(data) > 2_000_000:
                raise Upstream('response too large')
        return bytes(data)


async def discover(session, channel, key):
    async def rss():
        root = ET.fromstring(await read_body(session, RSS, {'channel_id': channel}))
        return [el.text for el in root.iter(YT + 'videoId')]

    async def playlist():
        data = json.loads(await read_body(session, API + 'playlistItems', {
            'part': 'contentDetails', 'playlistId': 'UU' + channel[2:], 'maxResults': '50',
            'fields': 'items/contentDetails/videoId'}, key))
        return [it['contentDetails']['videoId'] for it in data['items']]

    results = await asyncio.gather(rss(), playlist(), return_exceptions=True)
    ids, ok = set(), False
    for label, res in zip(('RSS', 'playlist'), results):
        if isinstance(res, BaseException):
            if not isinstance(res, Exception):
                raise res
            LOG.warning('YouTube %s unavailable (%s)', label, reason(res))
        else:
            ok = True
            ids.update(v for v in res if isinstance(v, str) and VID.fullmatch(v))
    return ids, ok


async def details(session, ids, key):
    result = []
    ids = sorted(ids)
    for i in range(0, len(ids), 50):
        data = json.loads(await read_body(session, API + 'videos', {
            'id': ','.join(ids[i:i+50]), 'part': 'snippet,status,liveStreamingDetails',
            'fields': 'items(id,snippet(channelId,publishedAt,title,liveBroadcastContent),status/privacyStatus,liveStreamingDetails(actualStartTime,actualEndTime,scheduledStartTime))'}, key))
        result.extend(data['items'])
    return result


def reason(exc):
    return str(exc) if isinstance(exc, Upstream) else type(exc).__name__


async def _run(bot, channel, dest, key, file):
    import aiohttp
    import discord

    scope = '%s:%d' % (channel, dest)
    state = await asyncio.to_thread(load_state, file, scope)
    # Always reset cutoff after process restart, even if disk survived. Only already
    # queued sends may cross that cutoff. First successful inventory is silent.
    baseline = None
    mirror = json.dumps(state, sort_keys=True)
    async def persist():
        nonlocal mirror
        encoded = json.dumps(state, sort_keys=True)
        if encoded != mirror:
            try:
                await asyncio.to_thread(save_state, file, state)
                mirror = encoded
            except OSError as e:
                LOG.warning('YouTube state not saved (%s); keeping memory state', reason(e))

    delay = POLL
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
        while not bot.is_closed():
            try:
                await bot.wait_until_ready()
                ids, ok = await discover(session, channel, key)
                ids.update(state['watch'])
                ids.update(state['pending'])
                rows = await details(session, ids - state['done'].keys(), key)
                if not ok and baseline is None:
                    raise Upstream('no discovery source for baseline')
                now = time.time()
                if baseline is None:
                    baseline = now
                    LOG.info('YouTube alert baseline ready')
                queue = reconcile(state, rows, channel, baseline, now)
                await persist()
                for _, vid, kind, title in queue:
                    try:
                        dest_ch = bot.get_channel(dest) or await bot.fetch_channel(dest)
                        label = '라이브 시작' if kind == 'live' else '새 영상'
                        clean = discord.utils.escape_markdown(' '.join(title.split())[:180])
                        await dest_ch.send(f'{label}: {clean}\nhttps://www.youtube.com/watch?v={vid}',
                                           allowed_mentions=discord.AllowedMentions.none())
                    except Exception as e:
                        LOG.warning('Discord notification deferred (%s)', reason(e))
                        break  # revalidate and retry next poll
                    state['done'][vid] = time.time()
                    state['pending'].pop(vid, None)
                    state['watch'].pop(vid, None)
                    await persist()
                if not ok:
                    raise Upstream('both discovery sources unavailable')
                delay = POLL
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.warning('YouTube poll deferred (%s)', reason(e))
                delay = min(1800, delay * 2)
            await asyncio.sleep(delay)


def install(bot):
    """Register once, without replacing the bot's own on_ready or starting a client."""
    attr = '_youtube_alerts_listener_v1'
    if getattr(bot, attr, None) is not None:
        return
    task = None

    async def ready():
        nonlocal task
        if task is not None and not task.done():
            return
        channel = os.environ.get('YOUTUBE_CHANNEL_ID', '').strip()
        dst = os.environ.get('DISCORD_NOTIFY_CHANNEL_ID', '').strip()
        key = os.environ.get('YOUTUBE_API_KEY', '').strip()
        if not (key and CID.fullmatch(channel) and re.fullmatch(r'[1-9][0-9]{0,19}', dst)):
            LOG.warning('YouTube alerts disabled: check the three environment variables')
            return
        file = Path(os.environ.get('YOUTUBE_STATE_FILE') or '.youtube-alerts-state.json')
        async def isolated():
            try:
                await _run(bot, channel, int(dst), key, file)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                LOG.warning('YouTube alerts stopped; base bot unaffected (%s)', reason(e))
        # No await between checking and assigning the task: repeated ready is safe.
        task = asyncio.create_task(isolated(), name='youtube-alerts')

    bot.add_listener(ready, 'on_ready')
    setattr(bot, attr, ready)

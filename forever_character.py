"""Read-only KR Forever profiles. Discord signup is self-reported, not Battle.net ownership."""
import asyncio
import os
import time
import unicodedata
from urllib.parse import quote

import aiohttp
import discord


API = 'https://kr.api.blizzard.com/profile/wow/character/'
TOKEN_URL = 'https://oauth.battle.net/token'
PARAMS = {'namespace': 'profile-forever-kr', 'locale': 'ko_KR'}


class LookupProblem(Exception):
    """Only raise with a public, credential-free Korean explanation."""


def slug(name):
    # Realmless: https://blizzard.pinta.land/forever/characters-and-guilds/
    clean = unicodedata.normalize('NFC', name.strip()).lower()
    parts = clean.split()
    if len(parts) == 1:
        parts = clean.split('-')
    if len(clean) > 64 or len(parts) != 2 or not all(p.isalpha() for p in parts):
        raise LookupProblem('주 이름과 보조 이름을 띄어 쓴 전체 이름인지 확인해 주세요.')
    return quote('-'.join(parts), safe='-')


class ForeverAPI:
    def __init__(self):
        self._lock = asyncio.Lock()
        self._access = None
        self._expires = 0

    async def _token(self, http):
        async with self._lock:
            if self._access and time.monotonic() < self._expires:
                return self._access
            key, secret = os.getenv('BLIZZARD_CLIENT_ID'), os.getenv('BLIZZARD_CLIENT_SECRET')
            if not key or not secret:
                raise LookupProblem('블리자드 API가 설정되지 않았습니다.')
            async with http.post(TOKEN_URL, auth=aiohttp.BasicAuth(key, secret),
                                 data={'grant_type': 'client_credentials'}, allow_redirects=False) as response:
                if response.status != 200:
                    raise LookupProblem('블리자드 API 인증에 실패했습니다. 운영진에게 알려 주세요.')
                body = await response.json()
            access = body.get('access_token') if isinstance(body, dict) else None
            if not isinstance(access, str) or not access:
                raise LookupProblem('블리자드 API 인증 응답을 확인하지 못했습니다.')
            ttl = body.get('expires_in', 0)
            self._expires = time.monotonic() + max(0, ttl - 60) if isinstance(ttl, int) else 0
            self._access = access
            return access

    async def lookup(self, name, *, equipment=False):
        url = API + slug(name) + ('/equipment' if equipment else '')
        try:
            return await asyncio.wait_for(self._fetch(url, equipment), timeout=15)
        except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
            raise LookupProblem('블리자드 API에 지금 연결할 수 없습니다. 잠시 뒤 다시 시도해 주세요.') from exc

    async def _fetch(self, url, equipment):
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=6)) as http:
            for attempt in range(2):
                token = await self._token(http)
                async with http.get(url, params=PARAMS, headers={'Authorization': 'Bearer ' + token},
                                    allow_redirects=False) as response:
                    if response.status == 401 and attempt == 0:
                        if self._access == token:
                            self._expires = 0
                        continue
                    if response.status == 404:
                        raise LookupProblem('블리자드에 공개된 장비 정보가 없습니다.' if equipment else
                                            '한국 포에버에서 공개 프로필을 찾지 못했습니다. 전체 이름과 공개 여부를 확인해 주세요.')
                    if response.status == 429:
                        raise LookupProblem('블리자드 API 요청이 많습니다. 잠시 뒤 다시 시도해 주세요.')
                    if response.status != 200:
                        raise LookupProblem('블리자드 API에서 정보를 받지 못했습니다.')
                    data = await response.json()
                    if not isinstance(data, dict) or not (isinstance(data.get('equipped_items'), list)
                                                         if equipment else isinstance(data.get('name'), str)):
                        raise LookupProblem('블리자드 응답에 필요한 정보가 없습니다.')
                    return data


def text(value, missing='미제공', limit=64):
    if isinstance(value, dict):
        value = value.get('name')
    if not isinstance(value, (str, int)) or not str(value).strip():
        return missing
    one_line = ' '.join(str(value).split())[:limit]
    return discord.utils.escape_markdown(discord.utils.escape_mentions(one_line))


def registered(data):
    faction, name = data
    # A separate plain code block lets desktop/mobile clients copy only the name.
    # Never allow a malformed signup value to break out of the code block.
    safe = ' '.join(name.split())[:64].replace('`', 'ˋ')
    return f'등업 기록 진영: {text(faction)}\n등록 캐릭터명 (복사용):\n```\n{safe}\n```'


def summary(data, registered_faction):
    faction = data.get('faction') or {}
    key = faction.get('type') if isinstance(faction, dict) else None
    label = {'HORDE': '호드', 'ALLIANCE': '얼라이언스'}.get(key) or text(faction)
    labels = {'호드': 'HORDE', 'horde': 'HORDE', '얼라이언스': 'ALLIANCE', '얼라': 'ALLIANCE', 'alliance': 'ALLIANCE'}
    stored = labels.get(registered_faction.strip().lower())
    actual = key if key in ('HORDE', 'ALLIANCE') else labels.get(label.lower())
    warning = '\n주의: 등업 기록과 API 진영이 다릅니다. 본인에게 다시 확인해 주세요.' if stored and actual and stored != actual else ''
    parts = ' · '.join(text(data.get(k)) for k in ('race', 'character_class'))
    return (f'블리자드 KR 포에버: 레벨 {text(data.get("level"))} · {parts}\n'
            f'API 진영: {label} · 규칙: {text(data.get("ruleset"))}\n'
            f'길드: {text(data.get("guild"))} · 장착 아이템 레벨: {text(data.get("equipped_item_level"))}' + warning)


def equipment_embed(data):
    embed = discord.Embed(title='내 캐릭터 장비')
    lines = []
    for item in data.get('equipped_items', [])[:30]:
        if not isinstance(item, dict):
            continue
        level = item.get('level')
        level = level.get('value') if isinstance(level, dict) else None
        lines.append(f'{text(item.get("slot"), "부위 미제공", 30)}: {text(item.get("name"), "이름 미제공", 56)} · {text(level, "레벨 미제공")}')
    embed.description = ('\n'.join(lines)[:4096] if lines else '공개된 장착 아이템이 없습니다.')
    return embed


class EquipmentView(discord.ui.View):
    def __init__(self, owner, name, api):
        super().__init__(timeout=180)
        self.owner, self.name, self.api = owner, name, api

    async def interaction_check(self, it):
        if it.user.id == self.owner:
            return True
        await it.response.send_message('이 장비는 명령을 실행한 본인만 확인할 수 있습니다.', ephemeral=True)
        return False

    @discord.ui.button(label='내 장비 보기', style=discord.ButtonStyle.secondary)
    async def show(self, it, button):
        await it.response.defer(ephemeral=True, thinking=True)
        try:
            data = await self.api.lookup(self.name, equipment=True)
            await it.edit_original_response(embed=equipment_embed(data), allowed_mentions=discord.AllowedMentions.none())
        except LookupProblem as exc:
            await it.edit_original_response(content=str(exc), allowed_mentions=discord.AllowedMentions.none())

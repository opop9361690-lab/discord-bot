"""KR WoW: Forever PvP auctions (stdlib, Python 3.9+).
Call refresh on startup/hourly; board reads SQLite only. Indexes can contain the
same houses for PvP and Normal. Defaults follow the community's KR-aware group
mapping (not Blizzard-published): https://auctionhouse.tk.gg/data/#markets .
Override IDs require an independent in-game KR PvP regular-auction ID witness.
Sources: https://us.forums.blizzard.com/en/blizzard/t/60334
https://github.com/jkc-be/goldbook/tree/main/docs/battlenet-api
https://wowfah.com/about#houses (ruleset labels can be provisional).
"""
import asyncio
import base64
import gzip
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from collections import Counter
from contextlib import closing, contextmanager
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

LOG = logging.getLogger(__name__)
F = ('alliance', 'horde')
LABEL = {'alliance':'Alliance Auction House', 'horde':'Horde Auction House'}
PVP = {'alliance':110,'horde':113}
API = 'https://kr.api.blizzard.com'
ROOT = '/data/wow/ruleset/1/auctions/'
DAY, GAP = 86400, 5400
# Classic raid staples: potions, protection potions, flasks, food and weapon oils.
RAID = {13444,13446,13452,13454,13457,13458,13459,13461,13442,13510,
        13511,13512,13513,20007,20008,20520,20452,18254,13931,20748,20749}
NOTE = '판매량은 등록 건수가 아닌 추정 개수입니다. 취소가 섞일 수 있고, 만료 가능 매물·수집 공백·관측 사이에 등록되고 팔린 물량은 제외됩니다.'
SCHEMA = '''
CREATE TABLE IF NOT EXISTS proof(house INTEGER PRIMARY KEY, faction TEXT NOT NULL, witness INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS head(house INTEGER PRIMARY KEY, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS active(house INTEGER, kind INTEGER, aid INTEGER, item INTEGER, qty INTEGER, price INTEGER, guaranteed INTEGER, PRIMARY KEY(house,kind,aid)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS spans(house INTEGER, end INTEGER, start INTEGER, PRIMARY KEY(house,end)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS sales(house INTEGER, end INTEGER, item INTEGER, units INTEGER, PRIMARY KEY(house,end,item)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS prices(house INTEGER, item INTEGER, price INTEGER, PRIMARY KEY(house,item)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS names(item INTEGER PRIMARY KEY, name TEXT, checked INTEGER NOT NULL);
'''


class MarketError(ValueError):
    """Safe (non-secret) board diagnostic."""


class MappingError(MarketError):
    """Identity conflict: suppress even recently cached rows until revalidated."""


def pos(value):
    if type(value) is not int or not 0 < value < 2**63:
        raise MarketError('숫자 설정 또는 원본 형식 오류')
    return value


def label(doc, locale='en_US'):
    text = doc.get('name')
    return text.get(locale) if isinstance(text, dict) else text


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Blizzard:
    def __init__(self, client, secret):
        self.client, self.secret = client, secret
        self.token, self.until = '', 0
        self.http = urllib.request.build_opener(NoRedirect())

    def request(self, url, auth, data=None):
        headers = {'Authorization': auth, 'Accept-Encoding':'gzip', 'Accept':'application/json',
                   'User-Agent':'ForeverMarket/1.0'}
        with self.http.open(urllib.request.Request(url, data=data, headers=headers), timeout=25) as r:
            stream = gzip.GzipFile(fileobj=r) if r.headers.get('Content-Encoding') == 'gzip' else r
            raw = stream.read(64*1024*1024+1)
            if len(raw) > 64*1024*1024:
                raise MarketError('원본 응답 크기 초과')
            return json.loads(raw), r.headers

    def get(self, route, ns='dynamic-forever-kr', locale='en_US'):
        if not self.client or not self.secret:
            raise MarketError('블리자드 API 환경 변수 미설정')
        for attempt in range(2):
            if time.monotonic() >= self.until:
                basic = base64.b64encode((self.client+':'+self.secret).encode()).decode()
                info, _ = self.request('https://oauth.battle.net/token','Basic '+basic,b'grant_type=client_credentials')
                self.token = info['access_token']
                self.until = time.monotonic()+max(1,int(info['expires_in'])-60)
            try:
                return self.request(API+route+'?'+urlencode({'namespace':ns,'locale':locale}), 'Bearer '+self.token)
            except urllib.error.HTTPError as exc:
                if exc.code != 401 or attempt:
                    raise
                self.until = 0

    def validate(self, houses):
        rules, _ = self.get('/data/wow/ruleset/1')
        if rules.get('id') != 1 or label(rules) != 'PvP':
            raise MappingError('한국 전쟁 규칙 확인 실패')
        try:
            index, _ = self.get(ROOT)
        except urllib.error.HTTPError as exc:
            if exc.code != 404:
                raise
            index, _ = self.get(ROOT+'index')
        for faction, house in houses.items():
            if [label(x) for x in index['auctions'] if x.get('id') == house] != [LABEL[faction]]:
                raise MappingError('설정한 경매장 ID와 블리자드 진영이 일치하지 않습니다')

    def snapshot(self, faction, house, now):
        route = ROOT+str(house)
        reg, rh = self.get(route)
        if reg.get('id') != house or label(reg) != LABEL[faction]:
            raise MappingError('경매장 응답 식별 불일치')
        com, ch = self.get(route+'/commodities')
        stamps = [int(parsedate_to_datetime(h['Last-Modified']).timestamp()) for h in (rh,ch)]
        if abs(stamps[0]-stamps[1]) > 120 or min(stamps) < now-GAP or max(stamps) > now+120:
            raise MarketError('오래되거나 시각이 다른 일반/상품 스냅샷')
        # Missing arrays can mean empty house, but never infer mass sales from
        # a lost field. Leave the last complete snapshot intact instead.
        if not all(isinstance(d.get('auctions'),list) for d in (reg,com)):
            raise MarketError('일반/상품 매물 배열 누락')
        return max(stamps), normalize(reg['auctions'],com['auctions'])

    def name(self, item):
        for ns in ('static-forever-kr','static-classic1x-kr'):
            try:
                data, _ = self.get('/data/wow/item/'+str(item),ns,'ko_KR')
                if data.get('id') == item and isinstance(label(data,'ko_KR'),str) and label(data,'ko_KR'):
                    return label(data,'ko_KR')[:160]
            except urllib.error.HTTPError as exc:
                if exc.code != 404:
                    raise
        return None


def normalize(regular, commodities):
    rows = {}
    for kind, listings in enumerate((regular,commodities)):
        for ent in listings:
            key = (kind,pos(ent['id']))
            item, qty = pos(ent['item']['id']), pos(ent['quantity'])
            if kind:
                price = pos(ent['unit_price'])
            else:
                total = ent.get('buyout') or 0
                price = (pos(total)+qty-1)//qty if total else None
            # Round whole-stack prices up to one copper. Never use an opening bid.
            guaranteed = {'SHORT':0,'MEDIUM':1800,'LONG':7200,'VERY_LONG':43200}.get(ent.get('time_left'),0)
            if key in rows:
                raise MarketError('중복 경매 ID')
            rows[key] = (item,qty,price,guaranteed)
    return rows


@contextmanager
def database(dbpath):
    with closing(sqlite3.connect(dbpath,timeout=20)) as db:
        db.execute('PRAGMA journal_mode=WAL')
        db.executescript(SCHEMA)
        with db:
            yield db


def record(db, house, ts, rows):
    old = db.execute('SELECT ts FROM head WHERE house=?',(house,)).fetchone()
    if old and old[0] >= ts:
        return 'unchanged'
    if old and ts-old[0] <= GAP:
        gap = ts-old[0]
        sold = Counter()
        for k,a,i,q,p,g in db.execute('SELECT kind,aid,item,qty,price,guaranteed FROM active WHERE house=?',(house,)):
            current = rows.get((k,a))
            if current is None:
                if p is not None and g > gap+120:
                    sold[i] += q
            elif k and current[0] == i and current[2] == p and q > current[1]:
                sold[i] += q-current[1]  # still active; cannot have expired
        db.executemany('INSERT INTO sales VALUES (?,?,?,?)',((house,ts,i,q) for i,q in sold.items()))
        db.execute('INSERT INTO spans VALUES (?,?,?)',(house,ts,old[0]))
    db.execute('DELETE FROM active WHERE house=?',(house,))
    db.executemany('INSERT INTO active VALUES (?,?,?,?,?,?,?)',((house,k,a,*v) for (k,a),v in rows.items()))
    db.execute('DELETE FROM prices WHERE house=?',(house,))
    db.execute('INSERT INTO prices SELECT house,item,MIN(price) FROM active WHERE house=? AND price IS NOT NULL GROUP BY item',(house,))
    db.execute('INSERT OR REPLACE INTO head VALUES (?,?)',(house,ts))
    for table in ('spans','sales'):
        db.execute('DELETE FROM '+table+' WHERE house=? AND end<=?',(house,ts-DAY-GAP))
    return 'updated'


def diagnostic(exc):
    if isinstance(exc, MarketError):
        return str(exc)
    if isinstance(exc, urllib.error.HTTPError):
        return '블리자드 HTTP '+str(exc.code)
    return '수집/저장 오류 ('+type(exc).__name__+')'


class MarketService:
    def __init__(self, dbpath, houses, witnesses, api, raid_ids=None, clock=time.time):
        self.dbpath, self.houses, self.witnesses = dbpath, houses, witnesses
        self.api, self.clock = api, clock
        self.raid = frozenset(RAID if raid_ids is None else raid_ids)
        self.config_error = ''
        self.errors = {}
        self.blocked = set()
        self.lock = asyncio.Lock()

    @classmethod
    def from_env(cls):
        api = Blizzard(os.getenv('BLIZZARD_CLIENT_ID'),os.getenv('BLIZZARD_CLIENT_SECRET'))
        service = cls(os.getenv('WOW_MARKET_DB') or 'forever-market.sqlite3',{},{},api)
        try:
            for f in F:
                prefix = 'WOW_PVP_'+f.upper()
                h = os.getenv(prefix+'_HOUSE_ID',str(PVP[f]))
                service.houses[f] = pos(int(h))
                witness = os.getenv(prefix+'_AUCTION_ID')
                if witness:
                    service.witnesses[f] = pos(int(witness))
            if len(set(service.houses.values())) != 2:
                raise MarketError('진영별 경매장 ID 중복')
            if 'WOW_RAID_ITEM_IDS' in os.environ:
                ids = os.environ['WOW_RAID_ITEM_IDS'].split(',')
                if len(ids) > 500:
                    raise MarketError('레이드 물품은 500개 이하로 설정하세요')
                service.raid = frozenset(pos(int(v.strip())) for v in ids)
        except (ValueError,OverflowError) as exc:
            service.config_error = str(exc) if isinstance(exc,MarketError) else '경매장/레이드 숫자 설정 오류'
        return service

    async def refresh(self):
        async with self.lock:
            return await asyncio.to_thread(self._refresh)

    def _refresh(self):
        try:
            if self.config_error:
                raise MarketError(self.config_error)
            self.api.validate(self.houses)
        except Exception as exc:
            if isinstance(exc,MappingError):
                self.blocked.update(F)
            self.errors = {f:diagnostic(exc) for f in F}
            return dict(self.errors)
        result = {}
        for f in F:
            try:
                h = self.houses[f]
                ts, rows = self.api.snapshot(f,h,int(self.clock()))
                with database(self.dbpath) as db:
                    db.execute('BEGIN IMMEDIATE')
                    proof = db.execute('SELECT witness FROM proof WHERE house=? AND faction=?',(h,f)).fetchone()
                    witness = self.witnesses.get(f)
                    if not proof or (witness and witness != proof[0]):
                        # Zero = community-mapped default plus live faction label; a
                        # nonzero witness = independently observed in-game auctionID.
                        if (h != PVP[f] or witness) and (0,witness) not in rows:
                            raise MappingError('한국 전쟁 대도시의 일반 매물 auctionID로 경매장 최초 확인 필요')
                        db.execute('INSERT OR REPLACE INTO proof VALUES (?,?,?)',(h,f,witness or 0))
                    result[f] = record(db,h,ts,rows)
                del rows
                self.errors.pop(f,None)
                self.blocked.discard(f)
            except Exception as exc:
                if isinstance(exc,MappingError):
                    self.blocked.add(f)
                self.errors[f] = result[f] = diagnostic(exc)
                LOG.warning('Forever market %s: %s',f,result[f])
        # Only resolve items actually selected; cache 404s briefly and keep previous
        # data when name lookup fails. Public board reads never perform a request.
        try:
            board = self._view(F)
            wanted = {x['item_id'] for x in board['top']+board['raid']}
            now = int(self.clock())
            with database(self.dbpath) as db:
                known = {i for i, in db.execute('SELECT item FROM names WHERE name IS NOT NULL OR checked>?',(now-DAY,))}
            for item in sorted(wanted-known):
                name = self.api.name(item)
                with database(self.dbpath) as db:
                    db.execute('INSERT OR REPLACE INTO names VALUES (?,?,?)',(item,name,now))
        except Exception as exc:
            LOG.warning('Forever market names: %s',diagnostic(exc))
        return result

    async def board(self):
        return await asyncio.to_thread(self._view,F)

    async def report(self,faction):
        if faction not in F:
            raise ValueError('unknown faction')
        out = await asyncio.to_thread(self._view,(faction,))
        for key in ('top','raid'):
            out[key] = [{**r[faction],'name':r['name'],'item_id':r['item_id']} for r in out[key]]
        out['faction'] = faction
        return out

    def _view(self,factions):
        now = int(self.clock())
        houses = {f:{'as_of':None,'coverage_hours':0.0,'status':'unavailable'} for f in factions}
        out = {'houses':houses,'as_of':None,'coverage_hours':0.0,'status':'unavailable','top':[],'raid':[],'note':NOTE}
        if self.config_error:
            out['note'] = self.config_error+'. '+NOTE
            return out
        try:
            with database(self.dbpath) as db:
                db.execute('BEGIN')
                for f in factions:
                    h = self.houses[f]
                    if f in self.blocked:
                        continue
                    last = db.execute('SELECT ts FROM head JOIN proof USING(house) WHERE house=? AND faction=? AND (? IS NULL OR witness=?)',(h,f,self.witnesses.get(f),self.witnesses.get(f))).fetchone()
                    if last:
                        houses[f]['as_of'] = last[0]
                times = [s['as_of'] for s in houses.values() if s['as_of'] is not None]
                # One window for both houses. When everything is old, age events
                # out against wall time instead of presenting an ancient 24h view.
                anchor = max(times) if times and now-max(times)<=GAP else now
                start = anchor-DAY
                for f in factions:
                    s = houses[f]
                    if s['as_of'] is None:
                        continue
                    seconds, = db.execute('SELECT COALESCE(SUM(end-max(start,?)),0) FROM spans WHERE house=? AND end>? AND end<=?',(start,self.houses[f],start,anchor)).fetchone()
                    s['coverage_hours'] = min(DAY,seconds)/3600
                    s['status'] = 'stale' if now-s['as_of']>GAP else ('ready' if seconds>=DAY-120 else 'partial')
                statuses = [s['status'] for s in houses.values()]
                out['status'] = next((st for st in ('unavailable','stale','partial') if st in statuses),'ready')
                out['as_of'] = max(times) if times else None
                out['coverage_hours'] = min(s['coverage_hours'] for s in houses.values())
                messages = dict(self.errors)
                reasons = list(dict.fromkeys(messages[f] for f in factions if f in messages))
                if out['status'] in ('unavailable','stale') or out['coverage_hours'] == 0:
                    reasons.append('모든 대상 진영에서 최근 비교 가능한 관측이 있어야 순위를 표시합니다')
                out['note'] = ('. '.join(reasons)+'. ' if reasons else '')+NOTE
                if out['status'] in ('unavailable','stale') or out['coverage_hours'] == 0:
                    return out
                ids = [self.houses[f] for f in factions]
                q = 'SELECT item,SUM(units) AS n FROM sales WHERE end>? AND end<=? AND house IN ('+','.join('?' for _ in ids)+')'
                for key,selection in [('top',None),('raid',sorted(self.raid))]:
                    if selection == []:
                        continue
                    restrict = '' if selection is None else ' AND item IN ('+','.join('?' for _ in selection)+')'
                    for item,total in db.execute(q+restrict+' GROUP BY item ORDER BY n DESC,item LIMIT 5',[start,anchor,*ids,*(selection or [])]):
                        nm = db.execute('SELECT name FROM names WHERE item=?',(item,)).fetchone()
                        row = {'item_id':item,'name':nm[0] if nm and nm[0] else '아이템 #'+str(item),'units':total}
                        for f in factions:
                            h = self.houses[f]
                            units, = db.execute('SELECT COALESCE(SUM(units),0) FROM sales WHERE house=? AND item=? AND end>? AND end<=?',(h,item,start,anchor)).fetchone()
                            price = db.execute('SELECT price FROM prices WHERE house=? AND item=?',(h,item)).fetchone()
                            row[f] = {'units':units,'min_unit_copper':price[0] if price else None}
                        out[key].append(row)
        except Exception as exc:
            out.update(status='unavailable',top=[],raid=[],note=diagnostic(exc)+'. '+NOTE)
        return out

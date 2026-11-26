"""Offline synthetic API contract, estimator, SQLite and combined ranking tests."""
import asyncio
import os
import tempfile
import unittest
from email.utils import formatdate
from unittest.mock import patch

import forever_market as m

T = 1800000000


def lot(a,i,q,p=10,t='LONG',commodity=False):
    row = {'id':a,'item':{'id':i},'quantity':q,'time_left':t}
    if p is not None:
        row['unit_price' if commodity else 'buyout'] = p
    return row


class Fake:
    def __init__(self):
        self.ts = T
        self.rows = {f:{} for f in m.F}
        self.fail = None

    def validate(self,houses):
        pass

    def snapshot(self,f,h,now):
        if f == self.fail:
            raise m.MarketError('수집 실패')
        return self.ts,self.rows[f]

    def name(self,item):
        return '시험 물품 '+str(item)


class Markets(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        fd, self.db = tempfile.mkstemp(suffix='.sqlite3')
        os.close(fd)
        self.addCleanup(os.unlink,self.db)
        self.api = Fake()
        self.s = m.MarketService(self.db,m.PVP.copy(),{},self.api,{9,20},lambda:self.api.ts)

    async def test_quantity_expiry_prices_rank_and_restart(self):
        self.api.rows['alliance'] = m.normalize([lot(1,9,3,10),lot(2,7,15,50,'SHORT'),lot(3,8,5,None),lot(4,17,9,30,'MEDIUM')],
            [lot(8,20,51,3,'SHORT',True),lot(9,9,5,50,'LONG',True)])
        self.api.rows['horde'] = m.normalize([lot(1,9,2)],[])
        await self.s.refresh()
        self.assertEqual((await self.s.board())['top'],[])
        self.api.ts += 3600
        self.api.rows['alliance'] = m.normalize([lot(88,9,3,10),lot(89,9,1,57),lot(90,9,1,None)],
            [lot(8,20,40,3,'SHORT',True)])
        self.api.rows['horde'] = {}
        await self.s.refresh()
        b = await self.s.board()
        self.assertEqual(b['status'],'partial')
        self.assertEqual(b['coverage_hours'],1)
        self.assertEqual([(r['item_id'],r['units']) for r in b['top']],[(20,11),(9,10)])
        self.assertEqual(b['top'],b['raid'])
        self.assertEqual(b['top'][1]['alliance'],{'units':8,'min_unit_copper':4})
        self.assertEqual(b['top'][1]['horde'],{'units':2,'min_unit_copper':None})
        self.assertEqual(b['top'][0]['alliance']['min_unit_copper'],3)
        self.assertEqual(b['top'][0]['name'],'시험 물품 20')
        restarted = m.MarketService(self.db,m.PVP,{},self.api,{20,9},lambda:self.api.ts)
        self.assertEqual(await restarted.board(),b)
        self.assertEqual(await restarted.refresh(),dict.fromkeys(m.F,'unchanged'))
        self.assertEqual((await restarted.report('alliance'))['top'][0]['units'],11)

    async def test_combined_winner_outside_both_top5(self):
        for idx,f in enumerate(m.F):
            items = [lot(n,100*(idx+1)+n,10) for n in range(1,6)] + [lot(9,9,9)]
            self.api.rows[f] = m.normalize(items,[])
        await self.s.refresh()
        self.api.ts += 3600
        self.api.rows = {f:{} for f in m.F}
        await self.s.refresh()
        b = await self.s.board()
        self.assertEqual(len(b['top']),5)
        self.assertEqual((b['top'][0]['item_id'],b['top'][0]['units']),(9,18))
        self.assertEqual([r['item_id'] for r in b['raid']],[9])
        self.assertNotIn(9,[r['item_id'] for r in (await self.s.report('alliance'))['top']])

    async def test_gap_stale_and_duplicate_do_not_create_sales(self):
        self.api.rows = {f:m.normalize([lot(1,9,3)],[]) for f in m.F}
        await self.s.refresh()
        self.api.ts += 7200
        self.assertEqual((await self.s.board())['status'],'stale')
        self.api.rows = {f:{} for f in m.F}
        await self.s.refresh()
        self.assertEqual((await self.s.board())['coverage_hours'],0)
        self.api.ts += 3600
        await self.s.refresh()
        self.assertEqual((await self.s.board())['top'],[])
        self.api.ts -= 60
        self.assertEqual((await self.s.refresh())['alliance'],'unchanged')

    async def test_full_rolling_window(self):
        self.api.rows = {f:m.normalize([lot(2,9,77)],[]) for f in m.F}
        await self.s.refresh()
        self.api.rows = {f:{} for f in m.F}
        for _ in range(24):
            self.api.ts += 3600
            await self.s.refresh()
        b = await self.s.board()
        self.assertEqual((b['status'],b['coverage_hours'],b['top'][0]['units']),('ready',24,154))
        self.api.ts += 3600
        await self.s.refresh()
        self.assertEqual((await self.s.board())['top'],[])

    async def test_wrong_override_witness_rejected(self):
        self.s.houses['horde'] = 114
        self.s.witnesses['horde'] = 9001
        self.api.rows['horde'] = m.normalize([],[lot(9001,4,2,3,'LONG',True)])
        await self.s.refresh()
        self.assertEqual((await self.s.board())['houses']['horde']['status'],'unavailable')
        self.api.rows['horde'] = m.normalize([lot(9001,4,1)],[])
        await self.s.refresh()
        self.assertEqual((await self.s.board())['houses']['horde']['status'],'partial')
        self.s.witnesses.clear()
        self.api.rows['horde'] = {}
        self.api.ts += 3600
        await self.s.refresh()
        self.assertEqual((await self.s.board())['top'][0]['horde']['units'],1)

    async def test_one_failed_house_does_not_publish_unilateral_board(self):
        self.api.fail = 'horde'
        await self.s.refresh()
        b = await self.s.board()
        self.assertEqual(b['status'],'unavailable')
        self.assertFalse(b['top'])
        self.assertIsNotNone(b['houses']['alliance']['as_of'])
        self.assertIsNone(b['houses']['horde']['as_of'])


class Wire(unittest.TestCase):
    def setUp(self):
        self.api = m.Blizzard(None,None)
        self.stamp = {'Last-Modified':formatdate(T,usegmt=True)}
        self.routes = {'/data/wow/ruleset/1':({'id':1,'name':'PvP'},{}),
                       m.ROOT:({'auctions':[{'id':110,'name':m.LABEL['alliance']},{'id':113,'name':m.LABEL['horde']},{'id':116,'name':'Blackwater Auction House'}]},{}),
                       m.ROOT+'110':({'id':110,'name':m.LABEL['alliance'],'auctions':[lot(1,99,1)]},self.stamp),
                       m.ROOT+'110/commodities':({'auctions':[lot(1,100,50,5,'LONG',True)]},self.stamp)}
        self.api.get = lambda route:self.routes[route]

    def test_separate_endpoints_same_numeric_auction_id(self):
        self.api.validate(m.PVP)
        ts, rows = self.api.snapshot('alliance',110,T)
        self.assertEqual(ts,T)
        self.assertEqual(rows[(0,1)][:2],(99,1))
        self.assertEqual(rows[(1,1)][:2],(100,50))
        with self.assertRaises(m.MarketError):
            self.api.validate({'alliance':116})
        with self.assertRaises(m.MarketError):
            self.api.validate({'alliance':113})
        self.routes['/data/wow/ruleset/1'][0]['name']='Normal'
        with self.assertRaises(m.MarketError):
            self.api.validate(m.PVP)

    def test_malformed_stale_and_mixed_feeds(self):
        c = m.ROOT+'110/commodities'
        with self.assertRaises(m.MarketError):
            self.api.snapshot('alliance',110,T+7200)
        self.routes[c] = ({'auctions':[]},{'Last-Modified':formatdate(T-3600,usegmt=True)})
        with self.assertRaises(m.MarketError):
            self.api.snapshot('alliance',110,T)
        self.routes[c] = ({},self.stamp)
        with self.assertRaises(m.MarketError):
            self.api.snapshot('alliance',110,T)
        with self.assertRaises(m.MarketError):
            m.normalize([lot(1,3,1),lot(1,3,1)],[])

    def test_defaults_missing_secrets_and_invalid_override(self):
        with patch.dict(os.environ,{},clear=True):
            s = m.MarketService.from_env()
            self.assertEqual(s.houses,m.PVP)
            self.assertTrue(all('환경 변수' in msg for msg in asyncio.run(s.refresh()).values()))
        with patch.dict(os.environ,{'WOW_PVP_HORDE_HOUSE_ID':'110'},clear=True):
            self.assertEqual(asyncio.run(m.MarketService.from_env().board())['status'],'unavailable')


if __name__ == '__main__':
    unittest.main()

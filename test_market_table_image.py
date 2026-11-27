"""Offline contract and layout tests; use the same local Korean font as the bot."""
import copy
import unittest
from io import BytesIO
from unittest.mock import patch

from PIL import Image

import market_table_image as table


def picture(color, size=(16, 16), fmt='PNG'):
    data = BytesIO()
    Image.new('RGB', size, color).save(data, fmt)
    return data.getvalue()


def item(i):
    return {'item_id': i, 'name': f'상급 보호 물약 {i}',
            'alliance': {'min_unit_copper': 123456, 'units': 0},
            'horde': {'min_unit_copper': 99, 'units': 23}}


def board(raid, top):
    return {'raid': raid, 'top': top, 'as_of': 1700000000,
            'houses': {'alliance': {'status': 'ready'}, 'horde': {'status': 'partial'}}}


class MarketTableImageTests(unittest.TestCase):
    def test_no_ranks_no_image_or_font_io(self):
        for data in ({}, {'raid': [], 'top': [], 'status': 'partial'}):
            self.assertIsNone(table.render_table(data, {}, 'missing.ttf'))

    def test_two_sections_five_slots_single_column_header(self):
        records = board([item(i) for i in range(1, 7)], [item(7)])
        before = copy.deepcopy(records)
        with patch.object(table, '_write', wraps=table._write) as write:
            png = table.render_table(records, {})
        self.assertEqual(records, before)
        self.assertEqual(png[:8], b'\x89PNG\r\n\x1a\n')
        with Image.open(BytesIO(png)) as im:
            im.verify()
        texts = [(c.args[1], c.args[2], c.args[3]) for c in write.call_args_list]
        headers = [c for c in texts if c[2] in ('아이템', '얼라이언스', '호드')]
        self.assertEqual([c[2] for c in headers], ['아이템', '얼라이언스', '호드'])
        self.assertEqual(len({c[1] for c in headers}), 1)
        self.assertEqual([c[0] for c in headers], sorted(c[0] for c in headers))
        labels = [t for _, _, t in texts]
        self.assertLess(labels.index('레이드 준비물'), labels.index('전체 품목'))
        self.assertNotIn('상급 보호 물약 6', labels)
        self.assertEqual(labels.count('순위 자료 없음'), 4)
        self.assertEqual(labels.count('24h 0개'), 6)
        self.assertEqual(labels.count('24h 23개 · 부분'), 6)

    def test_pastes_real_pixels_missing_or_corrupt_icon_is_a_placeholder(self):
        data = board([item(1)], [item(1)])
        hotpink = (253, 2, 176)
        with patch.object(table, '_thumb', wraps=table._thumb) as decode:
            supplied = table.render_table(data, {1: picture(hotpink)})
        self.assertEqual(decode.call_count, 1)  # repeated ID, two distinct ranks
        with Image.open(BytesIO(supplied)) as im:
            self.assertGreaterEqual(sum(px == hotpink for px in im.getdata()), 2*40*40)
        unknown = table.render_table(data, {})
        self.assertNotEqual(supplied, unknown)
        self.assertEqual(table.render_table(data, {1: b'invalid PNG'}), unknown)
        self.assertIsNone(table._thumb(picture('black', size=(1025, 1024))))
        self.assertIsNotNone(table._thumb(picture('navy', fmt='JPEG')))

    def test_money_is_exact_and_draws_only_nonzero_denominations(self):
        for copper, digits in ((1,['0','0','1']), (10000,['1','0','0']),
                               (23004567,['2,300','45','67'])):
            with self.subTest(copper=copper):
                self.assertEqual(table._cell({'min_unit_copper': copper}, 'ready')[0], digits)
        data = board([item(1)], [])
        with patch.object(table, '_coin', wraps=table._coin) as coin, \
                patch.object(table, '_write', wraps=table._write) as write:
            table.render_table(data, {})
        self.assertEqual([c.args[3] for c in coin.call_args_list], [0,1,2,2])
        self.assertFalse(any('골드' in c.args[3] or '실버' in c.args[3] or '코퍼' in c.args[3]
                             for c in write.call_args_list))

    def test_states_do_not_invent_prices_or_sales(self):
        for state in (None, 'unavailable', 'stale'):
            nums, status, qty = table._cell(item(1)['horde'], state)
            self.assertIsNone(nums)
            self.assertTrue(status)
            self.assertFalse(qty)
        for bad in (None, 0, -1, True, '100'):
            self.assertEqual(table._cell({'min_unit_copper': bad}, 'ready')[:2],
                             (None, '매물 미확인'))
        self.assertEqual(table._cell({'units': 0}, 'partial')[2], '24h 관측 부족')
        self.assertEqual(table._cell({'units': 0}, 'ready')[2], '24h 0개')
        for bad in (None, -1, True, '9'):
            self.assertEqual(table._cell({'units': bad}, 'ready')[2], '24h 관측 부족')

    def test_long_korean_and_sqlite_max_are_drawn_in_column_without_data_loss(self):
        row = item(3)
        row['name'] = '최상급치유의물약' * 15
        row['alliance'] = {'min_unit_copper': 2**63-1, 'units': 2**63-1}
        with patch.object(table, '_write', wraps=table._write) as write, \
                patch.object(table, '_coin', wraps=table._coin) as coins:
            png = table.render_table(board([row], []), {})
        with Image.open(BytesIO(png)) as im:
            canvas_width, canvas_height = im.size
        seen_names, qty_text = [], []
        largest_gold = f'{(2**63-1)//10000:,}'
        found_gold = False
        for call in write.call_args_list:
            draw, x, y, text, f = call.args[:5]
            left, top, right, bottom = draw.textbbox((x,y), text, font=f, anchor='lm')
            self.assertGreaterEqual(min(left, top), 0)
            self.assertLessEqual(right, canvas_width)
            self.assertLessEqual(bottom, canvas_height)
            if text and set(text) <= set(row['name']):
                seen_names.append(text)
                self.assertLess(right, table.CUTS[1])
            if x == table.CUTS[1]+16 and text not in ('얼라이언스',):
                self.assertLess(right, table.CUTS[2])
                if text == largest_gold:
                    found_gold = True
                qty_text.append(text)
        self.assertTrue(found_gold)
        self.assertIn(f'{2**63-1:,}개', ''.join(qty_text))
        self.assertEqual(''.join(seen_names), row['name'])
        for call in coins.call_args_list[:3]:
            self.assertLess(call.args[1]+20, table.CUTS[2])


if __name__ == '__main__':
    unittest.main()

"""Offline Pillow view of a Forever market board; does not mutate its inputs."""
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps


WIDTH = 920
CUTS = (0, 348, 634, WIDTH)
PRICE_WIDTH = CUTS[2] - CUTS[1] - 32
PAPER, PANEL, ALT = '#101923', '#17232f', '#1b2936'
INK, MUTED, RULE = '#f4f5f1', '#b9c8d5', '#354453'
ACCENT = '#e8c47e'
SIDES = ('alliance', 'horde')
SECTIONS = (('raid', '레이드 준비물'), ('top', '전체 품목'))
KST = timezone(timedelta(hours=9))


def _fonts(path):
    if path is None:
        base = Path(__file__).resolve().parent
        path = base / 'resources' / 'NotoSansKR.ttf'
        if not path.is_file():
            path = base / 'NotoSansKR.ttf'

    @lru_cache(maxsize=64)
    def font(size, bold=False):
        f = ImageFont.truetype(str(path), size)
        try:
            styles = f.get_variation_names()
            style = b'Bold' if bold else b'Regular'
            if style in styles:
                f.set_variation_by_name(style)
        except OSError:  # A static Korean font is also supported.
            pass
        return f
    return font


def _write(draw, x, y, text, font, color=INK):
    draw.text((x, y), text, font=font, fill=color, anchor='lm')


def _wrap(text, font, width):
    """Prefer spaces; split long Korean/Latin tokens at glyph boundaries."""
    lines = []
    while text:
        lo, hi = 0, len(text)
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if font.getlength(text[:mid]) <= width:
                lo = mid
            else:
                hi = mid - 1
        end = max(1, lo)
        if end == len(text):
            lines.append(text)
            break
        space = text.rfind(' ', 1, end + 1)
        end = space if space > end // 2 else end
        lines.append(text[:end].rstrip())
        text = text[end:].lstrip()
    return lines


def _cell(cell, state):
    if state not in ('ready', 'partial'):
        return None, '오래된 자료' if state == 'stale' else '자료 없음', ''
    price, qty = cell.get('min_unit_copper'), cell.get('units')
    if type(qty) is int and qty >= 0 and (state == 'ready' or qty > 0):
        quantity = f'24h {qty:,}개' + (' · 부분' if state == 'partial' else '')
    else:
        quantity = '24h 관측 부족'
    if type(price) is not int or price <= 0:
        return None, '매물 미확인', quantity
    g, rem = divmod(price, 10000)
    s, c = divmod(rem, 100)
    return [f'{g:,}', str(s), str(c)], '', quantity


def _coin(draw, x, y, kind):
    # Minted rims + three distinct devices (sun, diamond, square hole).
    edge, face, light = (('#97651e', '#e5ac3f', '#ffe4a1'),
                         ('#657b91', '#b6c9d9', '#edf6fc'),
                         ('#994f35', '#d88d68', '#ffd3ad'))[kind]
    draw.ellipse((x, y-10, x+20, y+10), fill=edge)
    draw.ellipse((x+2, y-9, x+18, y+7), fill=face, outline=light)
    if kind == 0:
        draw.polygon([(x+10,y-6),(x+12,y-2),(x+16,y-1),(x+12,y+1),
                      (x+10,y+5),(x+8,y+1),(x+4,y-1),(x+8,y-2)], fill=edge)
    elif kind == 1:
        draw.polygon([(x+10,y-6),(x+14,y-1),(x+10,y+4),(x+6,y-1)], fill=edge)
    else:
        draw.rectangle((x+8,y-3,x+12,y+1), fill=edge)


def _price_layout(numbers, font):
    # The largest value from SQLite can need two lines. Never abbreviate money.
    active = [(t, k) for k, t in enumerate(numbers) if t != '0']
    for size in range(30, 20, -1):
        f = font(size, True)
        widths = [round(f.getlength(t)) + 27 for t, _ in active]
        if sum(widths) + 8 * (len(active)-1) <= PRICE_WIDTH:
            return f, [[(t, k, w) for (t, k), w in zip(active, widths)]]
    while max(widths) > PRICE_WIDTH and size > 12:
        size -= 1
        f = font(size, True)
        widths = [round(f.getlength(t)) + 27 for t, _ in active]
    lines, line, used = [], [], 0
    for (text, kind), w in zip(active, widths):
        if line and used + 8 + w > PRICE_WIDTH:
            lines.append(line)
            line, used = [], 0
        line.append((text, kind, w))
        used += (8 if len(line) > 1 else 0) + w
    return f, lines + [line]


def _thumb(data):
    if not isinstance(data, bytes) or not data or len(data) > 2_000_000:
        return None
    try:
        with Image.open(BytesIO(data), formats=('JPEG', 'PNG', 'WEBP', 'GIF')) as src:
            if src.width * src.height > 1024 * 1024:
                return None
            return ImageOps.fit(src.convert('RGBA'), (48, 48), Image.Resampling.LANCZOS)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        return None


def _item_icon(im, draw, thumb, cy):
    x = 45
    if thumb is not None:
        draw.rounded_rectangle((x-2, cy-26, x+50, cy+25), 3, fill='#8090a1')
        im.paste(thumb, (x,cy-24), thumb)
    else:
        # Neutral inventory slot, deliberately not a simulated item portrait.
        draw.rounded_rectangle((x+6,cy-18,x+42,cy+18), 6, fill='#273744', outline='#798d9b', width=2)
        draw.polygon([(x+24,cy-8),(x+31,cy),(x+24,cy+8),(x+17,cy)], outline=MUTED, width=2)


def render_table(board, icons, font_path=None) -> bytes | None:
    """Render ranked rows from MarketService.board(). No ranks = no attachment."""
    sections = [(title, (board.get(key) or [])[:5]) for key, title in SECTIONS]
    if not any(rows for _, rows in sections):
        return None
    font = _fonts(font_path)
    houses = board.get('houses') or {}
    prepared, thumbs = [], {}
    for title, rows in sections:
        slots = []
        for row in rows:
            item_id = row.get('item_id')
            if item_id not in thumbs:
                thumbs[item_id] = _thumb((icons or {}).get(item_id))
            name = ' '.join(str(row.get('name') or f'아이템 #{item_id}').split())
            # The service caps actual names at 160 characters. Preserve every
            # one by growing the row, including unbroken Korean/Latin names.
            if len(name) > 160:
                name = name[:160] + '…'
            names = _wrap(name, font(28, True), CUTS[1]-115)
            cells = []
            height = max(86, 18 + 35 * len(names))
            for side in SIDES:
                nums, status, qty = _cell(row.get(side) or {}, (houses.get(side) or {}).get('status'))
                pf, prices = _price_layout(nums, font) if nums else (None, [])
                quantities = _wrap(qty, font(24), PRICE_WIDTH)
                h = max(1, len(prices)) * 35 + len(quantities) * 30
                cells.append((pf, prices, status, quantities, h))
                height = max(height, h + 18)
            slots.append((height, (item_id, names, cells)))
        slots.extend([(48, None)] * (5 - len(slots)))
        prepared.append((title, slots))

    top = 143
    height = top + sum(56 + sum(h for h, _ in slots) for _, slots in prepared) + 57
    im = Image.new('RGB', (WIDTH, height), PAPER)
    d = ImageDraw.Draw(im)
    d.rectangle((0, 0, 7, 90), fill=ACCENT)
    _write(d, 22, 25, 'KR  /  PVP', font(19, True), ACCENT)
    _write(d, 20, 62, '경매장 시세판', font(38, True))
    _write(d, 388, 31, '개당 최저가  /  24시간 추정 판매수량', font(26, True))
    ts = board.get('as_of')
    if type(ts) is int:
        try:
            stamp = datetime.fromtimestamp(ts, KST).strftime('%Y.%m.%d  %H:%M KST')
        except (ValueError, OSError, OverflowError):
            stamp = None
        if stamp:
            _write(d, 390, 68, '자료 기준   ' + stamp, font(22), MUTED)
    for col, (label, bg, fg) in enumerate((('아이템','#2a3948',INK), ('얼라이언스','#203c57','#b7daff'),
                                            ('호드','#493038','#ffc7b8'))):
        x = CUTS[col]
        d.rectangle((x,92,CUTS[col+1]-1,top-1), fill=bg)
        _write(d, x+16, 117, label, font(29, True), fg)

    y = top
    for si, (title, slots) in enumerate(prepared, 1):
        d.rectangle((0, y, WIDTH-1, y+55), fill='#24333c')
        _write(d, 14, y+27, f'0{si}', font(20, True), ACCENT)
        _write(d, 53, y+27, title, font(29, True))
        _write(d, WIDTH-91, y+27, 'TOP 5', font(23, True), ACCENT)
        y += 56
        for rank, (rh, content) in enumerate(slots, 1):
            cy = y + rh // 2
            d.rectangle((0,y,WIDTH-1,y+rh-1), fill=PANEL if rank % 2 else ALT)
            for split in CUTS[1:-1]:
                d.line((split,y,split,y+rh-1), fill=RULE)
            d.line((0,y+rh-1,WIDTH-1,y+rh-1), fill=RULE)
            _write(d, 12, cy, str(rank), font(23, True), ACCENT if content else MUTED)
            if content is None:
                _write(d, 55, cy, '순위 자료 없음', font(22), MUTED)
            else:
                ident, names, cells = content
                _item_icon(im, d, thumbs[ident], cy)
                for n, text in enumerate(names):
                    _write(d, 105, cy + 35*n - (len(names)-1)*35//2, text, font(28, True))
                for col, (pf, prices, status, qty, h) in enumerate(cells, 1):
                    ox = CUTS[col] + 16
                    line_y = cy - h//2 + 17
                    if not prices:
                        _write(d, ox, line_y, status, font(27), MUTED)
                        line_y += 35
                    for line in prices:
                        x = ox
                        for value, kind, w in line:
                            _write(d, x, line_y, value, pf)
                            _coin(d, x + w - 22, line_y, kind)
                            x += w + 8
                        line_y += 35
                    for text in qty:
                        _write(d, ox, line_y-2, text, font(24), MUTED)
                        line_y += 30
            y += rh
    _write(d, 16, y+27, '양 진영 합산 수량 순위  ·  추정치는 실제 거래량과 다를 수 있습니다.', font(22), MUTED)
    out = BytesIO()
    im.save(out, 'PNG')
    return out.getvalue()

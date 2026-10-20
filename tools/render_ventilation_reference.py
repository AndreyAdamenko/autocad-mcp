"""Render control PNGs from in-memory COM records; never a modelling input."""
import json
import re
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from autocad_mcp.geometry import world_geometry, transform

packet = json.load(sys.stdin)
records = packet['records']
frames = packet['frames']
font = ImageFont.truetype(r'C:\Windows\Fonts\arial.ttf', 14)
small = ImageFont.truetype(r'C:\Windows\Fonts\arial.ttf', 11)


def plain(text):
    text = text.replace('%%C', 'Ø').replace('\\P', ' ').replace('\\~', ' ')
    text = re.sub(r'\\[A-Za-z][^;]*;', '', text)
    return text.replace('{', '').replace('}', '')


def render(name, crop, origin):
    x0, y0, x1, y1 = crop
    width = 2400
    scale = (width - 100) / (x1 - x0)
    height = round((y1 - y0) * scale) + 110
    image = Image.new('RGB', (width, height), '#ffffff')
    draw = ImageDraw.Draw(image)

    def xy(point):
        return (50 + (point[0] - origin[0] - x0) * scale,
                height - 50 - (point[1] - origin[1] - y0) * scale)

    def inside(point):
        return x0 - 300 < point[0] - origin[0] < x1 + 300 and y0 - 300 < point[1] - origin[1] < y1 + 300

    for r in records:
        if r.get('visible') is False:
            continue
        g = r['geometry']
        layer = r.get('effective_layer', '')
        if g['kind'] in ('text', 'dimension', 'block', 'unsupported'):
            if g.get('centerline'):
                pts = [transform(r['world_matrix'], p) for p in g['centerline']]
            else:
                continue
        else:
            try:
                pts, edges, faces, closed = world_geometry(r, .5)
            except ValueError:
                continue
            if not any(inside(p) for p in pts):
                continue
            color = '#2873bd' if 'Прит' in layer else '#c02c2c' if ('Выт' in layer or 'Вентиляция_Выт' in layer) else '#328446' if 'Оборуд' in layer else '#c1c6ca'
            for i, j in edges:
                if inside(pts[i]) or inside(pts[j]):
                    draw.line([xy(pts[i]), xy(pts[j])], fill=color, width=2 if color != '#c1c6ca' else 1)
            continue
        if len(pts) > 1 and any(inside(p) for p in pts):
            draw.line([xy(p) for p in pts], fill='#9361a0', width=2)

    for r in records:
        if r.get('visible') is False:
            continue
        g = r['geometry']
        if g.get('leader_lines'):
            pts = [transform(r['world_matrix'], p) for p in g['leader_lines'][0]['vertices']]
            if not pts or not inside(pts[0]):
                continue
            draw.line([xy(p) for p in pts], fill='#686868', width=1)
            x, y = xy(pts[-1])
            label = plain(g.get('text', ''))
            draw.text((x, y - 16), label, font=small, fill='#222222')
            x, y = xy(pts[0])
            draw.ellipse((x - 2, y - 2, x + 2, y + 2), fill='#222222')
        elif g['kind'] == 'text' and r['id'].count('/') == 1:
            p = transform(r['world_matrix'], g['point'])
            if inside(p):
                draw.text(xy(p), plain(g['text']), font=font, fill='#333333')
        elif g['kind'] == 'block' and r['id'].count('/') == 1:
            p = transform(r['world_matrix'], g['insertion'])
            if inside(p) and any(token in g['effective_name'].lower() for token in ('duct', 'круг', 'переход', 'kv ')):
                draw.text(xy(p), r['id'].split('/')[-1], font=small, fill='#555555')
    draw.text((25, 15), name + ' | blue: supply, red: extract, purple: MLine path | COM QA', font=font, fill='#111111')
    target = ROOT / 'artifacts/ventilation' / (name + '.png')
    image.save(target)
    print(str(target), flush=True)


for key in ('ground', 'upper', 'roof'):
    render('source-' + key, (-2500, -1800, 32500, 15100), frames[key]['origin_mm'])
render('source-schemes', (365000, -584000, 430000, -545000), [0, 0])

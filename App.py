import streamlit as st
import ezdxf
from ezdxf import units
import xml.etree.ElementTree as ET
import zipfile
import math
import io
import urllib.request
import json
import matplotlib.pyplot as plt
from datetime import datetime
import re
import ssl

# ==========================================
# CONFIGURATION
# ==========================================
st.set_page_config(page_title="ASPLAN PRO v13.0 - QuickOSM to DXF", layout="wide")

OVERPASS_URL = "https://overpass.private.coffee/api/interpreter"

ROAD_WIDTHS = {
    'motorway': 20.0,
    'trunk': 16.0,
    'primary': 14.0,
    'secondary': 12.0,
    'tertiary': 10.0,
    'residential': 8.0,
    'service': 5.0,
    'unclassified': 7.0,
    'track': 4.0,
    'path': 2.0
}

# ==========================================
# 1. HELPER FUNCTIONS
# ==========================================

def latlon_to_meters(lon, lat, ref_lon, ref_lat):
    """Konversi Lat/Lon ke meter lokal (Mercator)"""
    r = 6378137.0
    x = r * math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat))
    y = r * math.radians(lat - ref_lat)
    return x, y

def smart_rename(name):
    """Penamaan cerdas untuk Closure dan Slack"""
    if not name:
        return name
    upper = name.upper()
    if re.search(r'\bCL(?:OS)?', upper) or re.search(r'\bC\d+', upper):
        numbers = re.findall(r'\d+', upper)
        if numbers:
            num = max(int(n) for n in numbers)
            return f"New Closure {num}C"
        return "New Closure"
    if re.search(r'\bSLACK\b|\.SS\b|SS\b|HANGER', upper):
        return "New Slack Support"
    return name

def get_road_width(highway_type):
    """Dapatkan lebar jalan berdasarkan klasifikasi"""
    if isinstance(highway_type, list):
        highway_type = highway_type[0] if highway_type else 'unclassified'
    return ROAD_WIDTHS.get(highway_type, 8.0)

def generate_smart_corridor(coords, width=6.0):
    """Buat koridor dari centerline (fallback)"""
    half_w = width / 2.0
    left_pts, right_pts, center_pts = [], [], []
    for i in range(len(coords) - 1):
        x1, y1 = coords[i]
        x2, y2 = coords[i+1]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        nx = -dy / length
        ny = dx / length
        left_pts.append((x1 + nx * half_w, y1 + ny * half_w))
        right_pts.append((x1 - nx * half_w, y1 - ny * half_w))
        center_pts.append((x1, y1))
        if i == len(coords) - 2:
            left_pts.append((x2 + nx * half_w, y2 + ny * half_w))
            right_pts.append((x2 - nx * half_w, y2 - ny * half_w))
            center_pts.append((x2, y2))
    return left_pts, right_pts, center_pts

def buffer_road_line(coords, width):
    """Buffer garis jalan menjadi poligon manual"""
    if len(coords) < 2:
        return None

    half_w = width / 2.0
    left_pts, right_pts = [], []

    for i in range(len(coords) - 1):
        x1, y1 = coords[i]
        x2, y2 = coords[i+1]
        dx = x2 - x1
        dy = y2 - y1
        length = math.hypot(dx, dy)
        if length == 0:
            continue
        nx = -dy / length
        ny = dx / length

        left_pts.append((x1 + nx * half_w, y1 + ny * half_w))
        right_pts.append((x1 - nx * half_w, y1 - ny * half_w))

        if i == len(coords) - 2:
            left_pts.append((x2 + nx * half_w, y2 + ny * half_w))
            right_pts.append((x2 - nx * half_w, y2 - ny * half_w))

    if left_pts and right_pts:
        return left_pts + right_pts[::-1] + [left_pts[0]]
    return None

# ==========================================
# 2. QUICKOSM ENGINE
# ==========================================

def fetch_roads_quickosm(min_lon, min_lat, max_lon, max_lat, ref_lon, ref_lat):
    buffer = 0.005
    north = max_lat + buffer
    south = min_lat - buffer
    east = max_lon + buffer
    west = min_lon - buffer

    query = f"""
    [out:json][timeout:15];
    (
      way["highway"]({south},{west},{north},{east});
    );
    out geom;
    """

    roads = []
    try:
        req = urllib.request.Request(
            OVERPASS_URL,
            data=query.encode('utf-8'),
            headers={'User-Agent': 'ASPLAN-PRO-QuickOSM/13.0'}
        )
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        with urllib.request.urlopen(req, timeout=15, context=ctx) as response:
            data = json.loads(response.read().decode('utf-8'))

        for element in data.get('elements', []):
            if element.get('type') != 'way':
                continue

            tags = element.get('tags', {})
            highway = tags.get('highway', '')
            if not highway:
                continue

            geom = element.get('geometry', [])
            if len(geom) < 2:
                continue

            m_coords = [latlon_to_meters(pt['lon'], pt['lat'], ref_lon, ref_lat) for pt in geom]
            roads.append({
                'name': tags.get('name', ''),
                'coords': m_coords,
                'highway': highway,
                'width': get_road_width(highway)
            })

        return roads

    except Exception as e:
        st.warning(f"⚠️ Gagal mengambil data dari Overpass: {e}")
        return []

# ==========================================
# 3. PARSER KMZ
# ==========================================

def parse_kmz(kmz_bytes):
    with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as z:
        kml_files = [f for f in z.namelist() if f.endswith('.kml')]
        if not kml_files:
            return None
        kml_content = z.read(kml_files[0])

    root = ET.fromstring(kml_content)
    ns = {'kml': 'http://www.opengis.net/kml/2.2'}

    cables, poles, all_raw_coords = [], [], []

    for placemark in root.findall('.//kml:Placemark', ns):
        line = placemark.find('.//kml:LineString/kml:coordinates', ns)
        if line is not None and line.text:
            pts = []
            for pt in line.text.strip().split():
                parts = pt.split(',')
                if len(parts) >= 2:
                    lon, lat = float(parts[0]), float(parts[1])
                    pts.append((lon, lat))
                    all_raw_coords.append((lon, lat))
            if pts:
                cables.append({'name': placemark.findtext('kml:name', '', ns), 'coords': pts})

        point = placemark.find('.//kml:Point/kml:coordinates', ns)
        if point is not None and point.text:
            parts = point.text.strip().split(',')
            if len(parts) >= 2:
                lon, lat = float(parts[0]), float(parts[1])
                name = placemark.findtext('kml:name', 'Pole', ns)
                desc = placemark.findtext('kml:description', '', ns).lower()
                has_acc = any(k in desc for k in ['acc', 'accessories', 'slack', 'box', 'closure', 'odp'])
                all_raw_coords.append((lon, lat))
                poles.append({'name': name, 'raw_coords': (lon, lat), 'has_accessories': has_acc})

    if not all_raw_coords:
        return None

    lons = [p[0] for p in all_raw_coords]
    lats = [p[1] for p in all_raw_coords]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    ref_lon, ref_lat = sum(lons)/len(lons), sum(lats)/len(lats)

    converted_cables = [{'name': c['name'], 'coords': [latlon_to_meters(lon, lat, ref_lon, ref_lat) for lon, lat in c['coords']]} for c in cables]

    converted_poles = []
    coord_tracker = {}
    inspector_logs = []

    for p in poles:
        mx, my = latlon_to_meters(p['raw_coords'][0], p['raw_coords'][1], ref_lon, ref_lat)
        coord_key = (round(mx, 2), round(my, 2))
        is_overlap = False
        if coord_key in coord_tracker:
            inspector_logs.append({
                'level': 'WARNING',
                'category': 'Overlap',
                'detail': f"Tiang '{p['name']}' bertumpuk dengan '{coord_tracker[coord_key]}'"
            })
            is_overlap = True
        else:
            coord_tracker[coord_key] = p['name']

        converted_poles.append({
            'name': p['name'],
            'coords': (mx, my),
            'has_accessories': p['has_accessories'],
            'is_overlap': is_overlap
        })

    if converted_cables and converted_poles:
        pole_positions = [p['coords'] for p in converted_poles]
        for cable in converted_cables:
            first, last = cable['coords'][0], cable['coords'][-1]
            dist_first = min(math.hypot(first[0]-p[0], first[1]-p[1]) for p in pole_positions)
            dist_last = min(math.hypot(last[0]-p[0], last[1]-p[1]) for p in pole_positions)
            if dist_first > 2.0:
                inspector_logs.append({'level': 'WARNING', 'category': 'Presisi Kabel', 'detail': f"Ujung awal kabel tidak menempel ke tiang ({dist_first:.1f}m)"})
            if dist_last > 2.0:
                inspector_logs.append({'level': 'WARNING', 'category': 'Presisi Kabel', 'detail': f"Ujung akhir kabel tidak menempel ke tiang ({dist_last:.1f}m)"})

    roads = fetch_roads_quickosm(min_lon, min_lat, max_lon, max_lat, ref_lon, ref_lat)
    road_polygons = []
    for road in roads:
        poly = buffer_road_line(road['coords'], road['width'])
        if poly:
            road_polygons.append({'name': road['name'], 'coords': poly, 'is_polygon': True})

    if not road_polygons and converted_cables:
        for cable in converted_cables:
            _, _, center = generate_smart_corridor(cable['coords'], width=6.0)
            if center:
                road_polygons.append({'name': 'JALAN UTAMA', 'coords': center, 'is_polygon': False})

    return {
        'cables': converted_cables,
        'poles': converted_poles,
        'roads': road_polygons,
        'inspector': inspector_logs,
        'ref': (ref_lon, ref_lat),
        'bbox': (min_lon, min_lat, max_lon, max_lat)
    }

# ==========================================
# 4. DXF GENERATOR
# ==========================================

def build_dxf_document(data, proj_info):
    doc = ezdxf.new(dxfversion='AC1027')
    doc.header['$INSUNITS'] = units.MM

    layers = doc.layers
    layers.add("01_BADAN_JALAN", color=8, lineweight=13)
    layers.add("01_NAMA_JALAN", color=2, lineweight=18)
    layers.add("03_KABEL_FO", color=1, lineweight=50)
    layers.add("04_POLE_TIANG", color=3, lineweight=30)
    layers.add("05_SMARTBOX_SLACK", color=2, lineweight=25)
    layers.add("KOP_TITLE_BLOCK", color=7, lineweight=25)
    layers.add("LEGENDA", color=7, lineweight=18)

    msp = doc.modelspace()
    all_x, all_y = [], []

    for road in data.get('roads', []):
        pts = road['coords']
        if len(pts) < 2:
            continue

        if road.get('is_polygon', False):
            msp.add_lwpolyline(pts, close=True, dxfattribs={'layer': '01_BADAN_JALAN', 'color': 8})
        else:
            msp.add_lwpolyline(pts, dxfattribs={'layer': '01_BADAN_JALAN', 'color': 8})

        if road.get('name'):
            mid_pt = pts[len(pts) // 2]
            msp.add_text(road['name'].upper(), dxfattribs={'layer': '01_NAMA_JALAN', 'height': 2.5, 'color': 2}).set_placement((mid_pt[0], mid_pt[1] + 1.5))

    for cable in data['cables']:
        pts = cable['coords']
        if len(pts) < 2:
            continue
        for pt in pts:
            all_x.append(pt[0]); all_y.append(pt[1])
        msp.add_lwpolyline(pts, dxfattribs={'layer': '03_KABEL_FO', 'color': 1, 'lineweight': 50})

    for p in data['poles']:
        pos, name = p['coords'], p['name']
        all_x.append(pos[0]); all_y.append(pos[1])

        msp.add_circle(pos, radius=0.8, dxfattribs={'layer': '04_POLE_TIANG', 'color': 3})
        display_name = smart_rename(name)

        if p['has_accessories']:
            msp.add_lwpolyline([
                (pos[0]-1.2, pos[1]-1.2), (pos[0]+1.2, pos[1]-1.2),
                (pos[0]+1.2, pos[1]+1.2), (pos[0]-1.2, pos[1]+1.2)
            ], close=True, dxfattribs={'layer': '05_SMARTBOX_SLACK', 'color': 2})

            msp.add_line(pos, (pos[0]+1.8, pos[1]-0.5), dxfattribs={'layer': '05_SMARTBOX_SLACK', 'color': 2})
            msp.add_text("New Slack Support", dxfattribs={'layer': '05_SMARTBOX_SLACK', 'height': 0.8, 'color': 2}).set_placement((pos[0] + 1.8, pos[1] - 0.5))

        text_y_offset = 0.8 if not p['is_overlap'] else 1.8
        msp.add_text(display_name, dxfattribs={'layer': '04_POLE_TIANG', 'height': 0.9, 'color': 3}).set_placement((pos[0] + 1.8, pos[1] + text_y_offset))

    layout = doc.layouts.get("Paper_A3") if "Paper_A3" in doc.layouts else doc.layouts.new("Paper_A3")
    layout.dxf.paper_width = 420.0
    layout.dxf.paper_height = 297.0

    min_x, max_x = (min(all_x), max(all_x)) if all_x else (0, 100)
    min_y, max_y = (min(all_y), max(all_y)) if all_y else (0, 100)

    cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    h = max_y - min_y or 100.0

    layout.add_viewport(center=(160, 148), size=(280.0, 200.0), view_center_point=(cx, cy), view_height=h * 1.18)

    tb_x1, tb_y1, tb_x2, tb_y2 = 300, 20, 410, 270
    layout.add_lwpolyline([(tb_x1, tb_y1), (tb_x2, tb_y1), (tb_x2, tb_y2), (tb_x1, tb_y2)], close=True, dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'color': 7, 'lineweight': 25})

    # Helper text untuk Kop dengan dukungan parameter color yang aman
    def add_title_text(text, x, y, height=2.5, color=7):
        layout.add_text(text, dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': height, 'color': color}).set_placement((x, y))

    y_pos = tb_y2 - 8
    add_title_text(f"SPAN: {proj_info.get('span_name', '')}", tb_x1+5, y_pos, 3.5)
    y_pos -= 10
    add_title_text(f"PROJECT: {proj_info.get('project_code', '')}", tb_x1+5, y_pos, 3.0)
    y_pos -= 8
    add_title_text(f"TANGGAL: {datetime.now().strftime('%d-%m-%Y')}", tb_x1+5, y_pos, 2.5)
    y_pos -= 6
    add_title_text(f"REVISI: {proj_info.get('revision', '0')}", tb_x1+5, y_pos, 2.5)
    y_pos -= 6
    add_title_text(f"SKALA: 1:{int(200.0/h*10) if h>0 else 1}", tb_x1+5, y_pos, 2.5)
    y_pos -= 8
    layout.add_line((tb_x1, y_pos), (tb_x2, y_pos), dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'color': 7})
    y_pos -= 5
    add_title_text(f"DRAWN: {proj_info.get('drawn_by', '')}", tb_x1+5, y_pos, 2.5)
    y_pos -= 5
    add_title_text(f"CHECKED: {proj_info.get('checked_by', '')}", tb_x1+5, y_pos, 2.5)
    y_pos -= 5
    add_title_text(f"APPROVED: {proj_info.get('approved_by', '')}", tb_x1+5, y_pos, 2.5)

    add_title_text("PT. RIZKI PRIMA SAKTI", tb_x1+5, tb_y1+5, 2.5, color=4)
    add_title_text("CLIENT: iFORTE", tb_x1+5, tb_y1+12, 2.5, color=4)

    leg_x1, leg_y1, leg_x2, leg_y2 = 20, 20, 120, 80
    layout.add_lwpolyline([(leg_x1, leg_y1), (leg_x2, leg_y1), (leg_x2, leg_y2), (leg_x1, leg_y2)], close=True, dxfattribs={'layer': 'LEGENDA', 'color': 7})
    layout.add_text("LEGENDA", dxfattribs={'layer': 'LEGENDA', 'height': 2.5, 'color': 7}).set_placement((leg_x1+5, leg_y2-5))

    legend_items = [("Kabel FO", 1), ("Tiang", 3), ("Closure/Slack", 2), ("Jalan", 8)]
    y_pos = leg_y2 - 15
    for label, color in legend_items:
        layout.add_text(f"● {label}", dxfattribs={'layer': 'LEGENDA', 'height': 2.0, 'color': color}).set_placement((leg_x1+5, y_pos))
        y_pos -= 6

    out_bytes = io.StringIO()
    doc.write(out_bytes)
    return out_bytes.getvalue()

# ==========================================
# 5. STREAMLIT UI
# ==========================================

st.sidebar.title("📇 Informasi Proyek")
span_name = st.sidebar.text_input("SPAN NAME", "14PBG007_REMBANGPBLG")
project_code = st.sidebar.text_input("PROJECT CODE", "RM-26-000327")
drawn_by = st.sidebar.text_input("DRAWN BY", "RPS")
checked_by = st.sidebar.text_input("CHECKED BY", "IFORTE")
approved_by = st.sidebar.text_input("APPROVED BY", "IFORTE")
revision = st.sidebar.text_input("REVISION", "0")

st.title("⚡ ASPLAN PRO v13.0 - QuickOSM to DXF Converter")
st.caption("Upload KMZ → Otomatis ambil peta jalan dari OpenStreetMap (QuickOSM) → DXF siap presentasi")

uploaded_file = st.file_uploader("📂 Upload File KMZ", type=['kmz'])

if uploaded_file:
    with st.spinner("🔄 Memproses data..."):
        parsed = parse_kmz(uploaded_file.read())

    if not parsed:
        st.error("❌ Gagal memproses file KMZ. Pastikan file valid.")
    else:
        st.success("✅ Data berhasil diproses!")
        col1, col2 = st.columns([1, 1])

        with col1:
            st.markdown("### 📐 Preview")
            fig, ax = plt.subplots(figsize=(6, 5))
            ax.set_facecolor('#0e1117')
            fig.patch.set_facecolor('#0e1117')

            for r in parsed['roads']:
                xs = [p[0] for p in r['coords']]
                ys = [p[1] for p in r['coords']]
                if r.get('is_polygon', False):
                    ax.fill(xs, ys, color='#444444', alpha=0.5)
                ax.plot(xs, ys, color='#ffffff', linewidth=1.2, alpha=0.8)

            for c in parsed['cables']:
                xs = [p[0] for p in c['coords']]
                ys = [p[1] for p in c['coords']]
                ax.plot(xs, ys, color='#ff4b4b', linewidth=2.5)

            if parsed['poles']:
                pxs = [p['coords'][0] for p in parsed['poles']]
                pys = [p['coords'][1] for p in parsed['poles']]
                ax.scatter(pxs, pys, color='#00ff7f', s=20, zorder=5)

            ax.tick_params(colors='white')
            ax.grid(True, color='#333333', linestyle='--')
            st.pyplot(fig)

        with col2:
            st.markdown("### 🔍 Inspector")
            if parsed['inspector']:
                st.warning(f"Ditemukan {len(parsed['inspector'])} potensi masalah:")
                st.table(parsed['inspector'])
            else:
                st.success("✅ Semua geometri presisi! Tidak ada error.")

            st.markdown("---")
            st.markdown("**📊 Statistik:**")
            st.write(f"- Segmen Kabel: {len(parsed['cables'])}")
            st.write(f"- Titik Tiang: {len(parsed['poles'])}")
            st.write(f"- Jalur Jalan: {len(parsed['roads'])}")

        proj_info = {
            'span_name': span_name,
            'project_code': project_code,
            'drawn_by': drawn_by,
            'checked_by': checked_by,
            'approved_by': approved_by,
            'revision': revision
        }

        dxf_string = build_dxf_document(parsed, proj_info)

        st.download_button(
            label=f"💾 Download DXF (A3 Layout) - {uploaded_file.name}.dxf",
            data=dxf_string,
            file_name=f"{uploaded_file.name.replace('.kmz', '')}_{datetime.now().strftime('%Y%m%d')}.dxf",
            mime="application/dxf",
            type="primary",
            use_container_width=True
        )

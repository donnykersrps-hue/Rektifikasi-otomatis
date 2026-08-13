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
import osmnx as ox
import re
from datetime import datetime

# ==========================================
# CONFIGURATION & PAGE SETUP
# ==========================================
st.set_page_config(page_title="ASPLAN PRO v12.5 - KMZ to DXF Converter", layout="wide")

# OSMnx settings – perbaikan endpoint & timeout
ox.settings.use_cache = True
ox.settings.timeout = 120  # 2 menit
ox.settings.overpass_endpoint = "https://overpass.private.coffee/api/interpreter"

# Daftar endpoint cadangan
OVERPASS_ENDPOINTS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

# ==========================================
# 1. HELPER FUNCTIONS
# ==========================================

def latlon_to_meters(lon, lat, ref_lon, ref_lat):
    """Konversi Lat/Lon ke meter lokal (Mercator)"""
    r = 6378137.0
    x = r * math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat))
    y = r * math.radians(lat - ref_lat)
    return x, y

def generate_smart_corridor(coords, width=6.0):
    """Buat koridor jalan (kiri, kanan, tengah) dari polyline"""
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

def fetch_real_road_network_robust(min_lon, min_lat, max_lon, max_lat, ref_lon, ref_lat):
    """
    Menarik data jalan dari OSM dengan retry ke beberapa endpoint.
    Menggunakan osmnx.features_from_bbox untuk efisiensi.
    """
    buffer = 0.005  # ~500m
    north = max_lat + buffer
    south = min_lat - buffer
    east = max_lon + buffer
    west = min_lon - buffer

    tags = {'highway': ['primary', 'secondary', 'tertiary', 'residential', 'unclassified', 'service']}
    roads = []

    for endpoint in OVERPASS_ENDPOINTS:
        try:
            ox.settings.overpass_endpoint = endpoint
            # Gunakan features_from_bbox lebih ringan dari graph
            gdf = ox.features_from_bbox(north=north, south=south, east=east, west=west, tags=tags)
            if gdf.empty:
                continue
            # Konversi ke koordinat meter
            for _, row in gdf.iterrows():
                geom = row.geometry
                if geom.geom_type == 'LineString':
                    coords = list(geom.coords)
                    m_pts = [latlon_to_meters(lon, lat, ref_lon, ref_lat) for lon, lat in coords]
                    if len(m_pts) >= 2:
                        roads.append({
                            'name': row.get('name', ''),
                            'coords': m_pts
                        })
            # Jika berhasil, keluar dari loop
            if roads:
                break
        except Exception:
            continue

    return roads

def parse_kmz(kmz_bytes):
    """Parsing KMZ dan ekstraksi kabel, tiang, serta deteksi overlap & ujung putus"""
    with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as z:
        kml_name = [f for f in z.namelist() if f.endswith('.kml')][0]
        kml_content = z.read(kml_name)

    root = ET.fromstring(kml_content)
    ns = {'kml': 'http://www.opengis.net/kml/2.2'}

    cables = []
    poles = []
    all_raw_coords = []

    # Ekstraksi LineString (Kabel)
    for placemark in root.findall('.//kml:Placemark', ns):
        line = placemark.find('.//kml:LineString/kml:coordinates', ns)
        if line is not None and line.text:
            raw_pts = line.text.strip().split()
            pts = []
            for pt in raw_pts:
                parts = pt.split(',')
                if len(parts) >= 2:
                    lon, lat = float(parts[0]), float(parts[1])
                    pts.append((lon, lat))
                    all_raw_coords.append((lon, lat))
            if pts:
                name = placemark.findtext('kml:name', '', ns)
                cables.append({'name': name, 'coords': pts})

        # Ekstraksi Point (Tiang)
        point = placemark.find('.//kml:Point/kml:coordinates', ns)
        if point is not None and point.text:
            parts = point.text.strip().split(',')
            if len(parts) >= 2:
                lon, lat = float(parts[0]), float(parts[1])
                name = placemark.findtext('kml:name', 'Pole', ns)
                desc = placemark.findtext('kml:description', '', ns).lower()
                has_acc = any(k in desc for k in ['acc', 'accessories', 'slack', 'box', 'closure'])
                poles.append({
                    'name': name,
                    'raw_coords': (lon, lat),
                    'has_accessories': has_acc
                })

    if not all_raw_coords:
        return {'cables': [], 'poles': [], 'roads': [], 'inspector': [], 'bbox': (0,0,0,0), 'ref': (0,0)}

    lons = [p[0] for p in all_raw_coords]
    lats = [p[1] for p in all_raw_coords]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    ref_lon = sum(lons) / len(lons)
    ref_lat = sum(lats) / len(lats)

    # Konversi kabel ke meter
    converted_cables = []
    for c in cables:
        m_pts = [latlon_to_meters(lon, lat, ref_lon, ref_lat) for lon, lat in c['coords']]
        converted_cables.append({'name': c['name'], 'coords': m_pts})

    # Konversi tiang & deteksi overlap
    converted_poles = []
    inspector_logs = []
    coord_tracker = {}

    for p in poles:
        mx, my = latlon_to_meters(p['raw_coords'][0], p['raw_coords'][1], ref_lon, ref_lat)
        coord_key = (round(mx, 2), round(my, 2))
        if coord_key in coord_tracker:
            inspector_logs.append({
                'level': 'WARNING',
                'category': 'Overlap Geometry',
                'detail': f"Tiang '{p['name']}' bertumpuk dengan '{coord_tracker[coord_key]}'"
            })
            is_overlap = True
        else:
            coord_tracker[coord_key] = p['name']
            is_overlap = False

        converted_poles.append({
            'name': p['name'],
            'coords': (mx, my),
            'has_accessories': p['has_accessories'],
            'is_overlap': is_overlap
        })

    # Deteksi ujung kabel putus (cable end > 2m dari tiang terdekat)
    if converted_cables and converted_poles:
        pole_positions = [p['coords'] for p in converted_poles]
        for cable in converted_cables:
            first = cable['coords'][0]
            last = cable['coords'][-1]
            # Jarak ke tiang terdekat (dalam meter)
            dist_first = min(math.hypot(first[0]-p[0], first[1]-p[1]) for p in pole_positions)
            dist_last = min(math.hypot(last[0]-p[0], last[1]-p[1]) for p in pole_positions)
            if dist_first > 2.0:
                inspector_logs.append({
                    'level': 'WARNING',
                    'category': 'Presisi Kabel',
                    'detail': f"Ujung awal kabel '{cable['name']}' tidak menempel ke tiang (jarak {dist_first:.1f}m)."
                })
            if dist_last > 2.0:
                inspector_logs.append({
                    'level': 'WARNING',
                    'category': 'Presisi Kabel',
                    'detail': f"Ujung akhir kabel '{cable['name']}' tidak menempel ke tiang (jarak {dist_last:.1f}m)."
                })

    # EKSEKUSI HYBRID ROAD ENGINE (dengan retry)
    real_roads = fetch_real_road_network_robust(min_lon, min_lat, max_lon, max_lat, ref_lon, ref_lat)
    final_roads = []

    if real_roads:
        final_roads = real_roads
    else:
        # Fallback: Smart Corridor untuk setiap kabel
        for cable in converted_cables:
            left_b, right_b, _ = generate_smart_corridor(cable['coords'], width=7.0)
            if left_b:
                final_roads.append({'name': 'JALAN UTAMA', 'coords': left_b})
            if right_b:
                final_roads.append({'name': '', 'coords': right_b})

    return {
        'cables': converted_cables,
        'poles': converted_poles,
        'roads': final_roads,
        'inspector': inspector_logs,
        'bbox': (min_lon, min_lat, max_lon, max_lat),
        'ref': (ref_lon, ref_lat)
    }

def smart_rename(name):
    """Penamaan cerdas untuk Closure dan Slack sesuai aturan bisnis"""
    if not name:
        return name
    upper = name.upper()
    # Closure detection (CL, CLOS, CLS, atau C diikuti angka)
    if re.search(r'\bCL(?:OS)?', upper) or re.search(r'\bC\d+', upper):
        numbers = re.findall(r'\d+', upper)
        if numbers:
            num = max(int(n) for n in numbers)
            return f"New Closure {num}C"
        else:
            return "New Closure"
    # Slack / SS / Hanger
    if re.search(r'\bSLACK\b|\.SS\b|SS\b|HANGER', upper):
        return "New Slack Support"
    return name

# ==========================================
# 2. DXF GENERATOR ENGINE (DIPERBAIKI)
# ==========================================

def build_dxf_document(parsed_data, proj_info):
    doc = ezdxf.new(dxfversion='AC1027')
    doc.header['$INSUNITS'] = units.MM

    # Layers
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

    # A. Render Jalan & Nama Jalan
    for road in parsed_data.get('roads', []):
        pts = road['coords']
        if len(pts) >= 2:
            msp.add_lwpolyline(pts, dxfattribs={'layer': '01_BADAN_JALAN'})
            if road.get('name'):
                mid_idx = len(pts) // 2
                mid_pt = pts[mid_idx]
                msp.add_text(road['name'].upper(), dxfattribs={
                    'layer': '01_NAMA_JALAN',
                    'height': 2.5
                }).set_placement((mid_pt[0], mid_pt[1] + 1.5))

    # B. Render Kabel
    for cable in parsed_data['cables']:
        pts = cable['coords']
        if len(pts) < 2:
            continue
        for pt in pts:
            all_x.append(pt[0]); all_y.append(pt[1])
        msp.add_lwpolyline(pts, dxfattribs={'layer': '03_KABEL_FO'})

    # C. Render Tiang & Aksesoris
    for p in parsed_data['poles']:
        pos = p['coords']
        name = p['name']
        all_x.append(pos[0]); all_y.append(pos[1])

        # Gambar tiang
        msp.add_circle(pos, radius=0.8, dxfattribs={'layer': '04_POLE_TIANG'})

        # Nama yang sudah diproses
        display_name = smart_rename(name)

        # Aturan aksesoris -> Smartbox + "New Slack Support"
        if p['has_accessories']:
            msp.add_rectangle4p([
                (pos[0]-1.2, pos[1]-1.2),
                (pos[0]+1.2, pos[1]-1.2),
                (pos[0]+1.2, pos[1]+1.2),
                (pos[0]-1.2, pos[1]+1.2)
            ], dxfattribs={'layer': '05_SMARTBOX_SLACK'})
            msp.add_text("New Slack Support", dxfattribs={
                'layer': '05_SMARTBOX_SLACK', 'height': 0.8
            }).set_placement((pos[0] + 1.8, pos[1] - 0.5))

        # Tampilkan nama di bawah/kanan tiang
        text_y_offset = 0.8 if not p['is_overlap'] else 1.8
        msp.add_text(display_name, dxfattribs={
            'layer': '04_POLE_TIANG', 'height': 0.9
        }).set_placement((pos[0] + 1.8, pos[1] + text_y_offset))

    # D. Layout A3 dengan Title Block & Legenda
    layout = doc.layouts.new("Paper_A3_Presentation") if "Paper_A3_Presentation" not in doc.layouts else doc.layouts.get("Paper_A3_Presentation")
    layout.dxf.paper_width = 420.0
    layout.dxf.paper_height = 297.0

    # Hitung bounding box model
    if all_x and all_y:
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
    else:
        min_x = min_y = 0
        max_x = max_y = 100
    cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    h = max_y - min_y or 100.0

    # Viewport utama
    layout.add_viewport(
        center=(160, 148),
        size=(280.0, 200.0),
        view_center_point=(cx, cy),
        view_height=h * 1.18
    )

    # ===== Title Block =====
    tb_x1, tb_y1 = 300, 20
    tb_x2, tb_y2 = 410, 270
    layout.add_lwpolyline([(tb_x1, tb_y1), (tb_x2, tb_y1), (tb_x2, tb_y2), (tb_x1, tb_y2)],
                          close=True, dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'color': 7, 'lineweight': 25})
    # Isi data
    layout.add_text(proj_info.get('span_name', ''), dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 3.5}).set_placement((tb_x1+5, tb_y2-10))
    layout.add_text(proj_info.get('project_code', ''), dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 3.0}).set_placement((tb_x1+5, tb_y2-25))
    layout.add_text(f"Tanggal: {datetime.now().strftime('%d-%m-%Y')}", dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 2.5}).set_placement((tb_x1+5, tb_y2-40))
    layout.add_text(f"Revisi: {proj_info.get('revision', '0')}", dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 2.5}).set_placement((tb_x1+5, tb_y2-55))
    layout.add_text(f"Skala: 1:{int(200.0/h*10) if h>0 else 1}", dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 2.5}).set_placement((tb_x1+5, tb_y2-70))
    # Personil
    layout.add_text(f"Drawn By: {proj_info.get('drawn_by', '')}", dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 2.5}).set_placement((tb_x1+5, tb_y2-90))
    layout.add_text(f"Checked By: {proj_info.get('checked_by', '')}", dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 2.5}).set_placement((tb_x1+5, tb_y2-105))
    layout.add_text(f"Approved By: {proj_info.get('approved_by', '')}", dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 2.5}).set_placement((tb_x1+5, tb_y2-120))

    # ===== Legenda =====
    leg_x1, leg_y1 = 20, 20
    leg_x2, leg_y2 = 120, 80
    layout.add_lwpolyline([(leg_x1, leg_y1), (leg_x2, leg_y1), (leg_x2, leg_y2), (leg_x1, leg_y2)],
                          close=True, dxfattribs={'layer': 'LEGENDA', 'color': 7})
    layout.add_text("LEGENDA", dxfattribs={'layer': 'LEGENDA', 'height': 2.5}).set_placement((leg_x1+5, leg_y2-5))
    items = [
        ("Kabel FO", "03_KABEL_FO", 1),
        ("Tiang", "04_POLE_TIANG", 3),
        ("Closure/Slack", "05_SMARTBOX_SLACK", 2),
        ("Jalan", "01_BADAN_JALAN", 8)
    ]
    y_pos = leg_y2 - 15
    for label, layer, color in items:
        layout.add_text(f"• {label}", dxfattribs={'layer': 'LEGENDA', 'height': 2.0, 'color': color}).set_placement((leg_x1+5, y_pos))
        y_pos -= 6

    out_bytes = io.StringIO()
    doc.write(out_bytes)
    return out_bytes.getvalue()

# ==========================================
# 3. STREAMLIT UI
# ==========================================

st.sidebar.title("📇 Informasi Proyek")
span_name = st.sidebar.text_input("SPAN NAME", "14PBG007_REMBANGPBLG - 14PBG03...")
project_code = st.sidebar.text_input("PROJECT CODE / REVISION", "RM-26-000327")
drawn_by = st.sidebar.text_input("DRAWN BY", "RPS")
checked_by = st.sidebar.text_input("CHECKED BY", "IFORTE")
approved_by = st.sidebar.text_input("APPROVED BY", "IFORTE")
revision = st.sidebar.text_input("REVISION", "0")

st.title("⚡ ASPLAN PRO v12.5 - KMZ to DXF Converter")
uploaded_file = st.file_uploader("Upload File KMZ", type=['kmz'])

if uploaded_file:
    parsed = parse_kmz(uploaded_file.read())
    st.subheader(f"📦 File: {uploaded_file.name}")
    st.write(f"Kabel: {len(parsed['cables'])} segmen, Tiang: {len(parsed['poles'])} titik, Jalan: {len(parsed['roads'])} jalur.")

    col1, col2 = st.columns([1, 1])

    with col1:
        st.markdown("### 📐 Viewport Preview")
        fig, ax = plt.subplots(figsize=(6, 5))
        ax.set_facecolor('#0e1117')
        fig.patch.set_facecolor('#0e1117')

        # Plot jalan
        for r in parsed['roads']:
            xs = [p[0] for p in r['coords']]
            ys = [p[1] for p in r['coords']]
            ax.plot(xs, ys, color='#888888', linestyle='--', linewidth=1, alpha=0.7)
        # Kabel
        for c in parsed['cables']:
            xs = [p[0] for p in c['coords']]
            ys = [p[1] for p in c['coords']]
            ax.plot(xs, ys, color='#ff4b4b', linewidth=2.5)
        # Tiang
        pxs = [p['coords'][0] for p in parsed['poles']]
        pys = [p['coords'][1] for p in parsed['poles']]
        ax.scatter(pxs, pys, color='#00ff7f', s=15, zorder=5)

        ax.tick_params(colors='white')
        ax.grid(True, color='#333333', linestyle='--')
        st.pyplot(fig)

    with col2:
        st.markdown("### 🔍 Precision & Quality Inspector")
        if parsed['inspector']:
            st.warning(f"Ditemukan {len(parsed['inspector'])} potensi masalah:")
            st.table(parsed['inspector'])
        else:
            st.success("✅ Semua geometri presisi! Tidak ada error.")

    proj_data = {
        'span_name': span_name,
        'project_code': project_code,
        'drawn_by': drawn_by,
        'checked_by': checked_by,
        'approved_by': approved_by,
        'revision': revision
    }

    dxf_string = build_dxf_document(parsed, proj_data)

    st.download_button(
        label=f"💾 Download DXF (dengan Layout) - {uploaded_file.name}.dxf",
        data=dxf_string,
        file_name=f"{uploaded_file.name}.dxf",
        mime="application/dxf"
    )

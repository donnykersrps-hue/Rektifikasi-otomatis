import streamlit as st
import ezdxf
from ezdxf import units
import xml.etree.ElementTree as ET
import zipfile
import math
import io
import requests
import matplotlib.pyplot as plt
from datetime import datetime
import re
import os
import pandas as pd

try:
    from shapely.geometry import LineString, Polygon, MultiPolygon
    from shapely.ops import unary_union
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False

st.set_page_config(page_title="ASPLAN PRO v15.0 - Final Precision Engine", layout="wide")

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter"
]

ROAD_WIDTHS = {
    'motorway': 16.0, 'trunk': 14.0, 'primary': 12.0,
    'secondary': 10.0, 'tertiary': 8.0, 'residential': 6.0,
    'service': 4.0, 'unclassified': 6.0
}

# ==========================================
# 1. KONVERSI KOORDINAT PRESISI
# ==========================================
def latlon_to_meters(lon, lat, ref_lon, ref_lat):
    r = 6378137.0
    x = r * math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat))
    y = r * math.radians(lat - ref_lat)
    return x, y

def smart_rename(name):
    if not name or pd.isna(name): return ""
    upper = str(name).upper().strip()
    if re.search(r'\bSLACK\b|\.SS\b|SS\b|HANGER', upper): return "New Slack Support"
    if re.search(r'\bCL(?:OS)?', upper) or re.search(r'\bC\d+', upper):
        numbers = re.findall(r'\d+', upper)
        if numbers:
            num = max(int(n) for n in numbers)
            return f"New Closure {num}C"
        return "New Closure"
    return name

# ==========================================
# 2. FETCH DATA JALAN ONLINE (DIRECT OVERPASS)
# ==========================================
def fetch_osm_roads(min_lon, min_lat, max_lon, max_lat):
    buf = 0.008
    south, north = min_lat - buf, max_lat + buf
    west, east = min_lon - buf, max_lon + buf
    
    query = f"""
    [out:json][timeout:25];
    (
      way["highway"]({south},{west},{north},{east});
    );
    out geom;
    """
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}
    
    for ep in OVERPASS_ENDPOINTS:
        try:
            res = requests.post(ep, data={'data': query}, headers=headers, timeout=20)
            if res.status_code == 200:
                data = res.json()
                ways = []
                for elem in data.get('elements', []):
                    if elem.get('type') == 'way' and 'geometry' in elem:
                        geom = elem['geometry']
                        if len(geom) >= 2:
                            coords = [(pt['lon'], pt['lat']) for pt in geom]
                            h_type = elem.get('tags', {}).get('highway', 'residential')
                            r_name = elem.get('tags', {}).get('name', '')
                            ways.append({'coords': coords, 'highway': h_type, 'name': r_name})
                if ways: return ways
        except Exception:
            continue
    return []

# ==========================================
# 3. PARSER KMZ (ACUAN REF UNIFIED)
# ==========================================
def parse_kmz(kmz_bytes):
    with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as z:
        kml_files = [f for f in z.namelist() if f.endswith('.kml')]
        if not kml_files: return None
        root = ET.fromstring(z.read(kml_files[0]))

    ns = {'kml': 'http://www.opengis.net/kml/2.2'}
    cables, poles, all_lons, all_lats = [], [], [], []

    for pm in root.findall('.//kml:Placemark', ns):
        # LineString
        line = pm.find('.//kml:LineString/kml:coordinates', ns)
        if line is not None and line.text:
            pts = []
            for pt in line.text.strip().split():
                p = pt.split(',')
                if len(p) >= 2:
                    lon, lat = float(p[0]), float(p[1])
                    pts.append((lon, lat))
                    all_lons.append(lon); all_lats.append(lat)
            if pts: cables.append({'name': pm.findtext('kml:name', '', ns), 'coords': pts})

        # Point
        point = pm.find('.//kml:Point/kml:coordinates', ns)
        if point is not None and point.text:
            p = point.text.strip().split(',')
            if len(p) >= 2:
                lon, lat = float(p[0]), float(p[1])
                name = pm.findtext('kml:name', 'Pole', ns)
                all_lons.append(lon); all_lats.append(lat)
                poles.append({'name': name, 'lon': lon, 'lat': lat})

    if not all_lons: return None

    # TITIACUAN GLOBAL TUNGGAL (Mencegah Tiang/Kabel Geser)
    ref_lon = sum(all_lons) / len(all_lons)
    ref_lat = sum(all_lats) / len(all_lats)

    # Konversi Kabel & Tiang ke Meter
    cables_m = [{'name': c['name'], 'coords': [latlon_to_meters(lon, lat, ref_lon, ref_lat) for lon, lat in c['coords']]} for c in cables]
    poles_m = [{'name': p['name'], 'coords': latlon_to_meters(p['lon'], p['lat'], ref_lon, ref_lat), 'raw': (p['lon'], p['lat'])} for p in poles]

    # Process Roads (Meter -> Buffer -> Dissolve)
    raw_roads = fetch_osm_roads(min(all_lons), min(all_lats), max(all_lons), max(all_lats))
    road_polygons = []
    road_labels = []

    if SHAPELY_AVAILABLE and raw_roads:
        buffered_list = []
        for r in raw_roads:
            m_coords = [latlon_to_meters(lon, lat, ref_lon, ref_lat) for lon, lat in r['coords']]
            if len(m_coords) >= 2:
                ls = LineString(m_coords)
                w = ROAD_WIDTHS.get(r['highway'], 6.0)
                # BUFFER DALAM METER
                poly = ls.buffer(w / 2.0, cap_style=2, join_style=2)
                if not poly.is_empty and poly.is_valid:
                    buffered_list.append(poly)
                
                # Simpan Label Nama Jalan
                if r['name'] and ls.length > 30:
                    road_labels.append({'name': r['name'], 'line': ls})

        if buffered_list:
            # DISSOLVE POLYGON UTUH
            merged = unary_union(buffered_list)
            if merged.geom_type == 'Polygon':
                road_polygons = [merged]
            elif merged.geom_type == 'MultiPolygon':
                road_polygons = list(merged.geoms)

    return {
        'cables': cables_m, 'poles': poles_m,
        'road_polygons': road_polygons, 'road_labels': road_labels
    }

# ==========================================
# 4. GENERATOR DXF (METER & BOUNDARY ONLY)
# ==========================================
def build_dxf(data):
    doc = ezdxf.new(dxfversion='AC1027')
    doc.header['$INSUNITS'] = units.M

    doc.layers.add("01_BADAN_JALAN", color=8, lineweight=13)
    doc.layers.add("01_NAMA_JALAN", color=7, lineweight=18)
    doc.layers.add("03_KABEL_FO", color=5, lineweight=40)
    doc.layers.add("04_POLE_TIANG", color=3, lineweight=30)
    doc.layers.add("05_SMARTBOX", color=1, lineweight=25)

    msp = doc.modelspace()

    # 1. BADAN JALAN (Garis Luar / Exterior Boundary Polygon Saja)
    for poly in data['road_polygons']:
        if poly.exterior:
            coords = list(poly.exterior.coords)
            msp.add_lwpolyline(coords, close=True, dxfattribs={'layer': '01_BADAN_JALAN', 'color': 8})

    # 2. NAMA JALAN
    for lbl in data['road_labels']:
        line = lbl['line']
        mid_pt = line.interpolate(0.5, normalized=True)
        p1, p2 = line.coords[0], line.coords[-1]
        angle = math.degrees(math.atan2(p2[1] - p1[1], p2[0] - p1[0]))
        if angle > 90: angle -= 180
        elif angle < -90: angle += 180
        
        msp.add_text(
            lbl['name'],
            dxfattribs={'layer': '01_NAMA_JALAN', 'height': 2.0, 'color': 7, 'rotation': angle}
        ).set_placement((mid_pt.x, mid_pt.y))

    # 3. KABEL
    for c in data['cables']:
        if len(c['coords']) >= 2:
            msp.add_lwpolyline(c['coords'], dxfattribs={'layer': '03_KABEL_FO', 'color': 5, 'lineweight': 40})

    # 4. TIANG & SMARTBOX
    pt_a = data['poles'][0]['name'] if data['poles'] else ""
    pt_b = data['poles'][-1]['name'] if data['poles'] else ""

    for p in data['poles']:
        pos = p['coords']
        name = p['name']
        acc_name = smart_rename(name)
        has_acc = (acc_name != name) and (acc_name != "")

        msp.add_circle(pos, radius=1.2, dxfattribs={'layer': '04_POLE_TIANG', 'color': 3})

        if (name in [pt_a, pt_b]) or has_acc:
            disp = [acc_name] if has_acc else []
            box_lines = [f"POLE: {name}"] + [f"+ {n}" for n in disp] + [f"Lat: {p['raw'][1]:.6f}", f"Lon: {p['raw'][0]:.6f}"]
            tx, ty = pos[0] + 8.0, pos[1] + 8.0
            
            msp.add_line(pos, (tx, ty), dxfattribs={'layer': '05_SMARTBOX', 'color': 1})
            mtext = msp.add_mtext("\n".join(box_lines), dxfattribs={'layer': '05_SMARTBOX', 'char_height': 1.5, 'color': 1})
            mtext.set_location((tx, ty), attachment_point=7)
            
            bw = max(len(l) for l in box_lines) * 1.5 * 0.65
            bh = len(box_lines) * 1.5 * 1.6
            msp.add_lwpolyline([(tx-1, ty+1), (tx+bw+2, ty+1), (tx+bw+2, ty-bh-1), (tx-1, ty-bh-1)], close=True, dxfattribs={'layer': '05_SMARTBOX', 'color': 1})
        else:
            msp.add_text(name, dxfattribs={'layer': '04_POLE_TIANG', 'height': 1.8, 'color': 7}).set_placement((pos[0] + 2.0, pos[1] + 2.0))

    out_bytes = io.StringIO()
    doc.write(out_bytes)
    return out_bytes.getvalue()

# ==========================================
# 5. STREAMLIT INTERFACE
# ==========================================
st.title("⚡ ASPLAN PRO v15.0 - Final Precision Engine")

uploaded_file = st.file_uploader("📂 Upload File KMZ", type=['kmz'])

if uploaded_file:
    with st.spinner("🔄 Memproses Peta Jalan & DXF..."):
        parsed = parse_kmz(uploaded_file.read())

    if not parsed:
        st.error("❌ File KMZ tidak valid atau kosong.")
    else:
        st.success(f"✅ Berhasil! Ditemukan {len(parsed['road_polygons'])} koridor jalan ter-dissolve.")

        col1, col2 = st.columns([1.2, 1])

        with col1:
            st.markdown("### 📐 Viewport Preview Box")
            fig, ax = plt.subplots(figsize=(7, 6), facecolor='#0e1117')
            ax.set_facecolor('#0e1117')

            # Render Poligon Jalan Utuh
            for poly in parsed['road_polygons']:
                xs, ys = poly.exterior.xy
                ax.fill(xs, ys, color='#444444', alpha=0.8, zorder=1)
                ax.plot(xs, ys, color='#ffffff', linewidth=1.2, zorder=2)

            # Render Kabel
            for c in parsed['cables']:
                xs, ys = [p[0] for p in c['coords']], [p[1] for p in c['coords']]
                ax.plot(xs, ys, color='#00a8ff', linewidth=2.0, zorder=3)

            # Render Tiang
            if parsed['poles']:
                pxs, pys = [p['coords'][0] for p in parsed['poles']], [p['coords'][1] for p in parsed['poles']]
                ax.scatter(pxs, pys, color='#00ff7f', s=20, zorder=4)

            ax.set_aspect('equal', adjustable='datalim')
            ax.tick_params(colors='white', labelsize=8)
            ax.grid(True, color='#222222', linestyle='--')
            st.pyplot(fig)

        with col2:
            st.markdown("### 📊 Ringkasan Format DXF")
            st.write(f"- Koridor Jalan (Dissolved): **{len(parsed['road_polygons'])}**")
            st.write(f"- Label Nama Jalan: **{len(parsed['road_labels'])}**")
            st.write(f"- Total Tiang: **{len(parsed['poles'])}**")
            st.write(f"- Total Kabel: **{len(parsed['cables'])}**")

            dxf_data = build_dxf(parsed)
            st.download_button(
                label="💾 Download File DXF (As-Built Presisi)",
                data=dxf_data,
                file_name=f"{uploaded_file.name.replace('.kmz', '')}_ASPLAN_V15.dxf",
                mime="application/dxf",
                type="primary",
                use_container_width=True
            )

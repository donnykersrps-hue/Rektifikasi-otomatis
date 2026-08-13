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
import time
import pandas as pd

# ==========================================
# 0. PUSTAKA GEOMETRI SPASIAL (SHAPELY)
# ==========================================
try:
    import geopandas as gpd
    from shapely.geometry import LineString, Polygon, MultiPolygon, GeometryCollection
    from shapely.ops import unary_union
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False
    st.error("⚠️ Pustaka 'shapely' wajib diinstal! Ketik: pip install shapely")

# ==========================================
# 1. KONFIGURASI SISTEM
# ==========================================
st.set_page_config(page_title="ASPLAN PRO v16 - Ultimate Precision", layout="wide")

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter"
]

ROAD_WIDTHS = {
    'motorway': 16.0, 'trunk': 14.0, 'primary': 12.0,
    'secondary': 10.0, 'tertiary': 8.0, 'residential': 6.0,
    'service': 4.0, 'unclassified': 6.0, 'track': 4.0, 'path': 2.0
}

# ==========================================
# 2. FUNGSI MATEMATIKA & LOGIKA
# ==========================================
def latlon_to_meters(lon, lat, ref_lon, ref_lat):
    """Konversi derajat GPS ke Meter (Sangat Penting untuk Buffer!)"""
    r = 6378137.0
    x = r * math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat))
    y = r * math.radians(lat - ref_lat)
    return x, y

def smart_rename(name):
    """Logika Rename Aksesoris & Closure"""
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

def get_road_width(highway_type):
    if isinstance(highway_type, list): highway_type = highway_type[0] if highway_type else 'unclassified'
    return ROAD_WIDTHS.get(highway_type, 6.0)

def extract_polygons(geom):
    """Pengaman Ekstraksi Kulit Luar Poligon"""
    polys = []
    if not geom or geom.is_empty: return polys
    
    if geom.geom_type == 'Polygon':
        polys.append(geom)
    elif geom.geom_type == 'MultiPolygon':
        polys.extend(list(geom.geoms))
    elif geom.geom_type == 'GeometryCollection':
        for g in geom.geoms:
            if g.geom_type == 'Polygon': polys.append(g)
            elif g.geom_type == 'MultiPolygon': polys.extend(list(g.geoms))
    return polys

# ==========================================
# 3. MESIN 3-LAPIS PETA JALAN
# ==========================================
def process_road_engine(cables, min_lon, min_lat, max_lon, max_lat, ref_lon, ref_lat, geojson_path=None):
    road_polygons = []
    road_source = "NONE"

    # Lapis 1: Coba Offline GeoJSON (Jika file ada)
    if geojson_path and os.path.exists(geojson_path) and SHAPELY_AVAILABLE:
        try:
            gdf = gpd.read_file(geojson_path)
            raw_roads = []
            for _, row in gdf.iterrows():
                geom = row.geometry
                if geom and geom.geom_type in ['LineString', 'MultiLineString']:
                    lines = list(geom.geoms) if geom.geom_type == 'MultiLineString' else [geom]
                    for line in lines:
                        if len(line.coords) >= 2:
                            raw_roads.append({
                                'coords': [latlon_to_meters(x, y, ref_lon, ref_lat) for x, y in line.coords],
                                'width': get_road_width(row.get('highway', 'unclassified'))
                            })
            road_polygons = execute_buffer_and_dissolve(raw_roads)
            if road_polygons: road_source = "GEOJSON_OFFLINE"
        except Exception: pass

    # Lapis 2: Coba Online Overpass (Anti-Blokir)
    if not road_polygons:
        buf = 0.008
        query = f"""
        [out:json][timeout:25];
        ( way["highway"]({min_lat-buf},{min_lon-buf},{max_lat+buf},{max_lon+buf}); );
        out geom;
        """
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)', 'Accept': 'application/json'}
        
        for ep in OVERPASS_ENDPOINTS:
            try:
                res = requests.post(ep, data={'data': query}, headers=headers, timeout=20)
                if res.status_code == 200:
                    raw_roads = []
                    for elem in res.json().get('elements', []):
                        if elem.get('type') == 'way' and 'geometry' in elem:
                            coords = [latlon_to_meters(pt['lon'], pt['lat'], ref_lon, ref_lat) for pt in elem['geometry']]
                            raw_roads.append({'coords': coords, 'width': get_road_width(elem.get('tags', {}).get('highway', ''))})
                    road_polygons = execute_buffer_and_dissolve(raw_roads)
                    if road_polygons: 
                        road_source = "OVERPASS_ONLINE"
                        break
            except Exception: time.sleep(1)

    # Lapis 3: Fallback Darurat Koridor Kabel
    if not road_polygons and cables:
        raw_roads = [{'coords': c['coords'], 'width': 8.0} for c in cables if len(c['coords']) >= 2]
        road_polygons = execute_buffer_and_dissolve(raw_roads)
        road_source = "FALLBACK_CORRIDOR"

    return road_polygons, road_source

def execute_buffer_and_dissolve(raw_roads):
    """Pusat Logika: Buffer (Meter) -> Dissolve -> Polygon Saja"""
    if not SHAPELY_AVAILABLE or not raw_roads: return []
    buffered_list = []
    
    for r in raw_roads:
        try:
            line = LineString(r['coords'])
            poly = line.buffer(r['width'] / 2.0, cap_style=2, join_style=2)
            if not poly.is_empty and poly.is_valid:
                buffered_list.append(poly)
        except Exception: continue

    if not buffered_list: return []
    
    try:
        merged = unary_union(buffered_list)
        return extract_polygons(merged)
    except Exception: return []

# ==========================================
# 4. PARSER KMZ TERPUSAT (SATU TITIK ACUAN)
# ==========================================
def parse_kmz(kmz_bytes, geojson_path=None):
    with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as z:
        kml_files = [f for f in z.namelist() if f.endswith('.kml')]
        if not kml_files: return None
        root = ET.fromstring(z.read(kml_files[0]))

    ns = {'kml': 'http://www.opengis.net/kml/2.2'}
    cables, poles, all_lons, all_lats = [], [], [], []

    for pm in root.findall('.//kml:Placemark', ns):
        line = pm.find('.//kml:LineString/kml:coordinates', ns)
        if line is not None and line.text:
            pts = []
            for pt in line.text.strip().split():
                p = pt.split(',')
                if len(p) >= 2:
                    pts.append((float(p[0]), float(p[1])))
                    all_lons.append(float(p[0])); all_lats.append(float(p[1]))
            if pts: cables.append({'name': pm.findtext('kml:name', '', ns), 'coords': pts})

        point = pm.find('.//kml:Point/kml:coordinates', ns)
        if point is not None and point.text:
            p = point.text.strip().split(',')
            if len(p) >= 2:
                all_lons.append(float(p[0])); all_lats.append(float(p[1]))
                poles.append({
                    'name': pm.findtext('kml:name', 'Pole', ns),
                    'lon': float(p[0]), 'lat': float(p[1])
                })

    if not all_lons: return None

    # MENGUNCI TITIK ACUAN AGAR TIANG DAN KABEL TIDAK GESER
    ref_lon, ref_lat = sum(all_lons) / len(all_lons), sum(all_lats) / len(all_lats)

    # Konversi murni ke Meter
    cables_m = [{'name': c['name'], 'coords': [latlon_to_meters(lon, lat, ref_lon, ref_lat) for lon, lat in c['coords']]} for c in cables]
    poles_m = [{'name': p['name'], 'coords': latlon_to_meters(p['lon'], p['lat'], ref_lon, ref_lat), 'raw': (p['lon'], p['lat'])} for p in poles]

    road_polys, road_src = process_road_engine(
        cables_m, min(all_lons), min(all_lats), max(all_lons), max(all_lats), ref_lon, ref_lat, geojson_path
    )

    return {
        'cables': cables_m, 'poles': poles_m,
        'road_polygons': road_polys, 'road_source': road_src
    }

# ==========================================
# 5. GENERATOR DXF (MENCETAK KULIT LUAR SAJA)
# ==========================================
def build_dxf(data):
    doc = ezdxf.new(dxfversion='AC1027')
    doc.header['$INSUNITS'] = units.M

    doc.layers.add("01_BADAN_JALAN", color=8, lineweight=13)
    doc.layers.add("03_KABEL_FO", color=5, lineweight=40)
    doc.layers.add("04_POLE_TIANG", color=3, lineweight=30)
    doc.layers.add("05_SMARTBOX", color=1, lineweight=25)

    msp = doc.modelspace()

    # MENCETAK EKSTERIOR BOUNDARY JALAN (Double Line Bersih)
    for poly in data['road_polygons']:
        if poly.exterior:
            coords = [(float(x), float(y)) for x, y in poly.exterior.coords]
            msp.add_lwpolyline(coords, close=True, dxfattribs={'layer': '01_BADAN_JALAN', 'color': 8})

    for c in data['cables']:
        if len(c['coords']) >= 2:
            msp.add_lwpolyline(c['coords'], dxfattribs={'layer': '03_KABEL_FO', 'color': 5, 'lineweight': 40})

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
# 6. TAMPILAN STREAMLIT (UI)
# ==========================================
st.title("⚡ ASPLAN PRO v16 - Ultimate Precision Engine")
st.caption("Berhasil memadukan Anti-Timeout + 3 Step Dissolve + Lock Reference!")

st.sidebar.markdown("### ⚙️ Pengaturan Lanjutan")
geojson_path = st.sidebar.text_input("Path GeoJSON Offline (Opsional):", value="")

uploaded_file = st.file_uploader("📂 Upload File KMZ Lapangan", type=['kmz'])

if uploaded_file:
    with st.spinner("🔄 Memproses Geometri (Buffer & Dissolve)..."):
        parsed = parse_kmz(uploaded_file.read(), geojson_path)

    if not parsed:
        st.error("❌ File KMZ kosong atau format tidak sesuai.")
    else:
        st.success(f"✅ Pemrosesan Selesai! Peta ditarik via: **{parsed['road_source']}**")

        col1, col2 = st.columns([1.5, 1])

        with col1:
            st.markdown("### 📐 Viewport Preview (Skala Asli)")
            fig, ax = plt.subplots(figsize=(8, 7), facecolor='#0e1117')
            ax.set_facecolor('#0e1117')

            # Render Peta Jalan (Hanya Eksterior) -> Dipaksa tampil di bawah
            for poly in parsed['road_polygons']:
                if poly.exterior:
                    xs, ys = poly.exterior.xy
                    ax.fill(xs, ys, color='#555555', alpha=0.9, zorder=1)
                    ax.plot(xs, ys, color='#ffffff', linewidth=1.5, zorder=2)

            # Render Kabel Biru
            for c in parsed['cables']:
                xs, ys = zip(*c['coords'])
                ax.plot(xs, ys, color='#00a8ff', linewidth=2.5, zorder=3)

            # Render Tiang Hijau
            if parsed['poles']:
                pxs, pys = zip(*[p['coords'] for p in parsed['poles']])
                ax.scatter(pxs, pys, color='#00ff7f', s=30, zorder=4)

            ax.set_aspect('equal', adjustable='datalim')
            ax.tick_params(colors='white')
            ax.grid(True, color='#222222', linestyle=':')
            st.pyplot(fig)

        with col2:
            st.markdown("### 📊 Ringkasan Eksekusi")
            st.info(f"🛣️ Badan Jalan Tersambung: **{len(parsed['road_polygons'])} Blok**")
            st.write(f"- Segmen Kabel FO: **{len(parsed['cables'])}**")
            st.write(f"- Jumlah Tiang (Pole): **{len(parsed['poles'])}**")
            st.write(f"- Mode Keamanan Peta: **{parsed['road_source']}**")

            dxf_data = build_dxf(parsed)
            st.download_button(
                label="💾 DOWNLOAD FILE DXF (SIAP PRINT)",
                data=dxf_data,
                file_name=f"{uploaded_file.name.replace('.kmz', '')}_ASPLAN_V16.dxf",
                mime="application/dxf",
                type="primary",
                use_container_width=True
            )

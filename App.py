import streamlit as st
import ezdxf
from ezdxf import units
import xml.etree.ElementTree as ET
import zipfile
import math
import io
import json
import urllib.request
import matplotlib.pyplot as plt
from datetime import datetime
import re
import os

# ==========================================
# 0. PUSTAKA OPSIONAL (Geopandas & Shapely)
# ==========================================
try:
    import geopandas as gpd
    from shapely.geometry import LineString, Polygon, MultiPolygon
    from shapely.ops import unary_union
    SHAPELY_AVAILABLE = True
except ImportError:
    SHAPELY_AVAILABLE = False
    st.warning("⚠️ Pustaka 'geopandas' dan 'shapely' tidak terinstal. "
               "Fungsi buffer jalan & dissolve dinonaktifkan. "
               "Install dengan: pip install geopandas shapely")

# ==========================================
# 1. KONFIGURASI
# ==========================================
st.set_page_config(page_title="ASPLAN PRO v14.2 - Buffer→Dissolve→Boundary", layout="wide")

OVERPASS_ENDPOINTS = [
    "https://overpass.private.coffee/api/interpreter",
    "https://overpass-api.de/api/interpreter",
]

ROAD_WIDTHS = {
    'motorway': 20.0, 'trunk': 16.0, 'primary': 14.0,
    'secondary': 12.0, 'tertiary': 10.0, 'residential': 8.0,
    'service': 5.0, 'unclassified': 7.0, 'track': 4.0, 'path': 2.0
}

# ==========================================
# 2. FUNGSI BANTU
# ==========================================

def latlon_to_meters(lon, lat, ref_lon, ref_lat):
    r = 6378137.0
    x = r * math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat))
    y = r * math.radians(lat - ref_lat)
    return x, y

def smart_rename(name):
    """Logika Rename Paten Kak Donny"""
    if not name:
        return name
    upper = str(name).upper().strip()
    if re.search(r'\bSLACK\b|\.SS\b|SS\b|HANGER', upper):
        return "New Slack Support"
    if re.search(r'\bCL(?:OS)?', upper) or re.search(r'\bC\d+', upper):
        numbers = re.findall(r'\d+', upper)
        if numbers:
            num = max(int(n) for n in numbers)
            return f"New Closure {num}C"
        return "New Closure"
    return name

def get_road_width(highway_type):
    if isinstance(highway_type, list):
        highway_type = highway_type[0] if highway_type else 'unclassified'
    return ROAD_WIDTHS.get(highway_type, 8.0)

# ==========================================
# 3. CORE ENGINE: BUFFER → DISSOLVE → BOUNDARY
# ==========================================

def create_road_polygons(roads):
    """
    🔥 URUTAN YANG BENAR:
    1. Buffer setiap LineString → Polygon
    2. Dissolve semua Polygon → unary_union
    3. Kembalikan list of Polygon/MultiPolygon hasil dissolve
    """
    if not SHAPELY_AVAILABLE or not roads:
        return []

    polygons = []
    for road in roads:
        coords = road['coords']
        if len(coords) < 2:
            continue
        line = LineString(coords)
        width = road.get('width', 8.0)
        buffer_width = width / 2.0
        try:
            # Buffer dengan cap_style=2 (Flat) dan join_style=2 (Round)
            poly = line.buffer(buffer_width, cap_style=2, join_style=2)
            if not poly.is_empty and poly.is_valid:
                polygons.append(poly)
        except Exception:
            continue

    if not polygons:
        return []

    # Dissolve semua polygon menyatu di persimpangan
    try:
        dissolved = unary_union(polygons)
        if dissolved.is_empty:
            return []
        if dissolved.geom_type == 'Polygon':
            return [{'geometry': dissolved, 'name': ''}]
        elif dissolved.geom_type == 'MultiPolygon':
            return [{'geometry': g, 'name': ''} for g in dissolved.geoms]
        else:
            return []
    except Exception:
        return []

# ==========================================
# 4. ENGINE DATA JALAN (OFFLINE-FIRST)
# ==========================================

def load_roads_from_geojson(geojson_path, ref_lon, ref_lat):
    if not SHAPELY_AVAILABLE:
        return []
    try:
        gdf = gpd.read_file(geojson_path)
        if 'highway' in gdf.columns:
            gdf = gdf[gdf['highway'].notna()]
        roads = []
        for _, row in gdf.iterrows():
            geom = row.geometry
            if geom is None:
                continue
            lines = list(geom.geoms) if geom.geom_type == 'MultiLineString' else ([geom] if geom.geom_type == 'LineString' else [])
            for line in lines:
                if len(line.coords) < 2:
                    continue
                m_coords = [latlon_to_meters(lon, lat, ref_lon, ref_lat) for lon, lat in line.coords]
                highway = row.get('highway', 'unclassified')
                roads.append({
                    'name': row.get('name', ''),
                    'coords': m_coords,
                    'highway': highway,
                    'width': get_road_width(highway)
                })
        return roads
    except Exception as e:
        st.warning(f"⚠️ Gagal memuat GeoJSON: {e}")
        return []

def fetch_roads_online(min_lon, min_lat, max_lon, max_lat, ref_lon, ref_lat):
    buffer = 0.005
    north, south = max_lat + buffer, min_lat - buffer
    east, west = max_lon + buffer, min_lon - buffer
    query = f"""
    [out:json][timeout:15];
    (
      way["highway"]({south},{west},{north},{east});
    );
    out geom;
    """
    for endpoint in OVERPASS_ENDPOINTS:
        try:
            req = urllib.request.Request(
                endpoint,
                data=query.encode('utf-8'),
                headers={'User-Agent': 'ASPLAN-PRO/14.2'}
            )
            with urllib.request.urlopen(req, timeout=15) as response:
                data = json.loads(response.read().decode('utf-8'))
            roads = []
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
            if roads:
                return roads
        except Exception:
            continue
    return []

# ==========================================
# 5. PARSER KMZ
# ==========================================

def parse_kmz(kmz_bytes, geojson_path=None):
    with zipfile.ZipFile(io.BytesIO(kmz_bytes)) as z:
        kml_files = [f for f in z.namelist() if f.endswith('.kml')]
        if not kml_files:
            return None
        kml_content = z.read(kml_files[0])

    root = ET.fromstring(kml_content)
    ns = {'kml': 'http://www.opengis.net/kml/2.2'}

    cables = []
    poles = []
    all_raw_coords = []

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

        point = placemark.find('.//kml:Point/kml:coordinates', ns)
        if point is not None and point.text:
            parts = point.text.strip().split(',')
            if len(parts) >= 2:
                lon, lat = float(parts[0]), float(parts[1])
                name = placemark.findtext('kml:name', 'Pole', ns)
                desc = placemark.findtext('kml:description', '', ns).lower()
                has_acc = any(k in desc for k in ['acc', 'accessories', 'slack', 'box', 'closure', 'odp'])
                all_raw_coords.append((lon, lat))
                poles.append({
                    'name': name,
                    'raw_coords': (lon, lat),
                    'has_accessories': has_acc
                })

    if not all_raw_coords:
        return None

    lons = [p[0] for p in all_raw_coords]
    lats = [p[1] for p in all_raw_coords]
    min_lon, max_lon = min(lons), max(lons)
    min_lat, max_lat = min(lats), max(lats)
    ref_lon = sum(lons) / len(lons)
    ref_lat = sum(lats) / len(lats)

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
                'level': 'WARNING', 'category': 'Overlap',
                'detail': f"Tiang '{p['name']}' bertumpuk dengan '{coord_tracker[coord_key]}'"
            })
            is_overlap = True
        else:
            coord_tracker[coord_key] = p['name']

        converted_poles.append({
            'name': p['name'], 'coords': (mx, my),
            'has_accessories': p['has_accessories'], 'is_overlap': is_overlap,
            'raw_latlon': p['raw_coords']
        })

    # Hybrid Road Engine
    road_polygons = []
    road_source = 'NONE'

    if geojson_path and os.path.exists(geojson_path) and SHAPELY_AVAILABLE:
        roads = load_roads_from_geojson(geojson_path, ref_lon, ref_lat)
        if roads:
            road_polygons = create_road_polygons(roads)
            road_source = 'GEOJSON_OFFLINE'

    if not road_polygons:
        roads = fetch_roads_online(min_lon, min_lat, max_lon, max_lat, ref_lon, ref_lat)
        if roads:
            road_polygons = create_road_polygons(roads)
            road_source = 'OVERPASS_ONLINE'

    if not road_polygons and converted_cables:
        polygons = [LineString(c['coords']).buffer(4.0, cap_style=2, join_style=2) for c in converted_cables if len(c['coords']) >= 2]
        if polygons:
            dissolved = unary_union(polygons)
            if not dissolved.is_empty:
                geoms = dissolved.geoms if dissolved.geom_type == 'MultiPolygon' else [dissolved]
                road_polygons = [{'geometry': g, 'name': ''} for g in geoms if g.geom_type == 'Polygon']
                road_source = 'FALLBACK_CORRIDOR'

    return {
        'cables': converted_cables, 'poles': converted_poles,
        'road_polygons': road_polygons, 'road_source': road_source,
        'inspector': inspector_logs, 'ref': (ref_lon, ref_lat),
        'bbox': (min_lon, min_lat, max_lon, max_lat)
    }

# ==========================================
# 6. DXF GENERATOR
# ==========================================

def build_dxf_document(data, proj_info):
    doc = ezdxf.new(dxfversion='AC1027')
    doc.header['$INSUNITS'] = units.M  # Menggunakan Satuan Meter

    layers = doc.layers
    layers.add("01_BADAN_JALAN", color=8, lineweight=13)
    layers.add("01_NAMA_JALAN", color=7, lineweight=18)
    layers.add("03_KABEL_FO", color=5, lineweight=40)
    layers.add("04_POLE_TIANG", color=3, lineweight=30)
    layers.add("05_SMARTBOX_SLACK", color=1, lineweight=25)

    msp = doc.modelspace()

    # ===== 1. JALAN (EXTERIOR BOUNDARY POLYGON HASIL DISSOLVE) =====
    for road in data.get('road_polygons', []):
        geom = road.get('geometry')
        if geom is None:
            continue
        polys = geom.geoms if geom.geom_type == 'MultiPolygon' else ([geom] if geom.geom_type == 'Polygon' else [])
        for poly in polys:
            if poly.exterior:
                # Ambil koordinat x, y persis dari exterior polygon
                coords_2d = [(float(pt[0]), float(pt[1])) for pt in poly.exterior.coords]
                if len(coords_2d) >= 3:
                    msp.add_lwpolyline(coords_2d, close=True, dxfattribs={'layer': '01_BADAN_JALAN', 'color': 8})

    # ===== 2. KABEL =====
    for cable in data['cables']:
        pts = [(float(p[0]), float(p[1])) for p in cable['coords']]
        if len(pts) >= 2:
            msp.add_lwpolyline(pts, dxfattribs={'layer': '03_KABEL_FO', 'color': 5, 'lineweight': 40})

    # ===== 3. TIANG & SMARTBOX =====
    pt_a = data['poles'][0]['name'] if data['poles'] else ""
    pt_b = data['poles'][-1]['name'] if data['poles'] else ""

    for p in data['poles']:
        pos = (float(p['coords'][0]), float(p['coords'][1]))
        name = p['name']
        acc_name = smart_rename(name)
        has_acc = (acc_name != name) and (acc_name != "")

        # Circle Tiang
        msp.add_circle(pos, radius=1.2, dxfattribs={'layer': '04_POLE_TIANG', 'color': 3})

        # Smartbox Callout
        if (name in [pt_a, pt_b]) or has_acc or p['has_accessories']:
            display_names = [acc_name] if has_acc else []
            box_lines = [f"POLE: {name}"] + [f"+ {n}" for n in display_names] + [f"Lat: {p['raw_latlon'][1]:.6f}", f"Lon: {p['raw_latlon'][0]:.6f}"]
            
            tx, ty = pos[0] + 8.0, pos[1] + 8.0
            msp.add_line(pos, (tx, ty), dxfattribs={'layer': '05_SMARTBOX_SLACK', 'color': 1})
            
            mtext = msp.add_mtext("\n".join(box_lines), dxfattribs={'layer': '05_SMARTBOX_SLACK', 'char_height': 1.5, 'color': 1})
            mtext.set_location((tx, ty), attachment_point=7)
            
            bw = max(len(l) for l in box_lines) * 1.5 * 0.65
            bh = len(box_lines) * 1.5 * 1.6
            pts_box = [(tx-1, ty+1), (tx+bw+2, ty+1), (tx+bw+2, ty-bh-1), (tx-1, ty-bh-1)]
            msp.add_lwpolyline(pts_box, close=True, dxfattribs={'layer': '05_SMARTBOX_SLACK', 'color': 1})
        else:
            msp.add_text(name, dxfattribs={'layer': '04_POLE_TIANG', 'height': 1.8, 'color': 7}).set_placement((pos[0] + 2.0, pos[1] + 2.0))

    out_bytes = io.StringIO()
    doc.write(out_bytes)
    return out_bytes.getvalue()

# ==========================================
# 7. STREAMLIT UI LAYOUT
# ==========================================

st.sidebar.title("📇 Informasi Proyek")
span_name = st.sidebar.text_input("SPAN NAME", "14PBG007_REMBANGPBLG")
project_code = st.sidebar.text_input("PROJECT CODE", "RM-26-000327")
drawn_by = st.sidebar.text_input("DRAWN BY", "RPS")
checked_by = st.sidebar.text_input("CHECKED BY", "IFORTE")
approved_by = st.sidebar.text_input("APPROVED BY", "IFORTE")

st.sidebar.markdown("---")
geojson_path = st.sidebar.text_input("Path File GeoJSON Jalan (Offline)", value="indonesia_roads.geojson")

st.title("⚡ ASPLAN PRO v14.2 - Buffer → Dissolve → Boundary")

uploaded_file = st.file_uploader("📂 Upload File KMZ", type=['kmz'])

if uploaded_file:
    with st.spinner("🔄 Memproses data..."):
        parsed = parse_kmz(uploaded_file.read(), geojson_path if geojson_path.strip() else None)

    if not parsed:
        st.error("❌ Gagal memproses file KMZ.")
    else:
        st.success(f"✅ Data berhasil diproses! Sumber jalan: {parsed['road_source']}")

        col1, col2 = st.columns([1.2, 1])

        with col1:
            st.markdown("### 📐 Viewport Preview Box")
            fig, ax = plt.subplots(figsize=(6, 5), facecolor='#0e1117')
            ax.set_facecolor('#0e1117')

            for r in parsed['road_polygons']:
                geom = r.get('geometry')
                if geom:
                    polys = geom.geoms if geom.geom_type == 'MultiPolygon' else ([geom] if geom.geom_type == 'Polygon' else [])
                    for poly in polys:
                        xs, ys = poly.exterior.xy
                        ax.fill(xs, ys, color='#444444', alpha=0.5)
                        ax.plot(xs, ys, color='#ffffff', linewidth=1.0, alpha=0.8)

            for c in parsed['cables']:
                xs = [p[0] for p in c['coords']]
                ys = [p[1] for p in c['coords']]
                ax.plot(xs, ys, color='#00a8ff', linewidth=2.0)

            if parsed['poles']:
                pxs = [p['coords'][0] for p in parsed['poles']]
                pys = [p['coords'][1] for p in parsed['poles']]
                ax.scatter(pxs, pys, color='#ff4757', s=20, zorder=5)

            ax.set_aspect('equal', adjustable='datalim')
            ax.tick_params(colors='white', labelsize=8)
            ax.grid(True, color='#222222', linestyle='--')
            st.pyplot(fig)

        with col2:
            st.markdown("### 🔍 Precision Inspector")
            if parsed['inspector']:
                st.warning(f"Ditemukan {len(parsed['inspector'])} catatan presisi:")
                st.table(parsed['inspector'])
            else:
                st.success("✅ Semua geometri presisi!")

            st.markdown("---")
            st.write(f"- Segmen Kabel: **{len(parsed['cables'])}**")
            st.write(f"- Titik Tiang: **{len(parsed['poles'])}**")
            st.write(f"- Poligon Jalan: **{len(parsed['road_polygons'])}**")
            st.write(f"- Sumber Jalan: **{parsed['road_source']}**")

        proj_info = {'span_name': span_name, 'project_code': project_code, 'drawn_by': drawn_by, 'checked_by': checked_by, 'approved_by': approved_by}
        dxf_string = build_dxf_document(parsed, proj_info)

        st.download_button(
            label=f"💾 Download File DXF Hasil Revisi ({uploaded_file.name})",
            data=dxf_string,
            file_name=f"{uploaded_file.name.replace('.kmz', '')}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.dxf",
            mime="application/dxf",
            type="primary",
            use_container_width=True
        )

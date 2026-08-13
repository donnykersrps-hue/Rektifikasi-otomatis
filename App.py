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
    if isinstance(highway_type, list):
        highway_type = highway_type[0] if highway_type else 'unclassified'
    return ROAD_WIDTHS.get(highway_type, 8.0)

# ==========================================
# 3. BUFFER → DISSOLVE → BOUNDARY
# ==========================================

def create_road_polygons(roads):
    """
    Buffer setiap LineString menjadi Polygon, lalu dissolve semua Polygon.
    Output: list of dict dengan key 'geometry' berisi Polygon/MultiPolygon hasil dissolve.
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
            poly = line.buffer(buffer_width, cap_style=2, join_style=2)
            if not poly.is_empty:
                polygons.append(poly)
        except Exception:
            continue

    if not polygons:
        return []

    # Dissolve semua polygon
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
            if geom.geom_type == 'MultiLineString':
                lines = list(geom.geoms)
            elif geom.geom_type == 'LineString':
                lines = [geom]
            else:
                continue
            for line in lines:
                if line.geom_type != 'LineString' or len(line.coords) < 2:
                    continue
                m_coords = []
                for lon, lat in line.coords:
                    mx, my = latlon_to_meters(lon, lat, ref_lon, ref_lat)
                    m_coords.append((mx, my))
                name = row.get('name', '')
                highway = row.get('highway', 'unclassified')
                roads.append({
                    'name': name,
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
                m_coords = []
                for pt in geom:
                    mx, my = latlon_to_meters(pt['lon'], pt['lat'], ref_lon, ref_lat)
                    m_coords.append((mx, my))
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

    # Konversi kabel
    converted_cables = []
    for c in cables:
        m_pts = [latlon_to_meters(lon, lat, ref_lon, ref_lat) for lon, lat in c['coords']]
        converted_cables.append({'name': c['name'], 'coords': m_pts})

    # Konversi tiang & deteksi overlap
    converted_poles = []
    coord_tracker = {}
    inspector_logs = []

    for p in poles:
        mx, my = latlon_to_meters(p['raw_coords'][0], p['raw_coords'][1], ref_lon, ref_lat)
        coord_key = (round(mx, 2), round(my, 2))
        if coord_key in coord_tracker:
            inspector_logs.append({
                'level': 'WARNING',
                'category': 'Overlap',
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

    # Deteksi ujung kabel putus
    if converted_cables and converted_poles:
        pole_positions = [p['coords'] for p in converted_poles]
        for cable in converted_cables:
            first = cable['coords'][0]
            last = cable['coords'][-1]
            dist_first = min(math.hypot(first[0]-p[0], first[1]-p[1]) for p in pole_positions)
            dist_last = min(math.hypot(last[0]-p[0], last[1]-p[1]) for p in pole_positions)
            if dist_first > 2.0:
                inspector_logs.append({
                    'level': 'WARNING',
                    'category': 'Presisi Kabel',
                    'detail': f"Ujung awal kabel tidak menempel ke tiang ({dist_first:.1f}m)"
                })
            if dist_last > 2.0:
                inspector_logs.append({
                    'level': 'WARNING',
                    'category': 'Presisi Kabel',
                    'detail': f"Ujung akhir kabel tidak menempel ke tiang ({dist_last:.1f}m)"
                })

    # ===== HYBRID ROAD ENGINE =====
    road_polygons = []
    road_source = 'NONE'

    # PRIORITAS 1: GeoJSON Offline
    if geojson_path and os.path.exists(geojson_path) and SHAPELY_AVAILABLE:
        roads = load_roads_from_geojson(geojson_path, ref_lon, ref_lat)
        if roads:
            road_polygons = create_road_polygons(roads)
            road_source = 'GEOJSON_OFFLINE'
            st.success("✅ Data jalan dari GeoJSON offline berhasil dimuat!")
        else:
            st.warning("⚠️ GeoJSON ditemukan tetapi tidak ada data jalan yang valid.")

    # PRIORITAS 2: Overpass Online
    if not road_polygons:
        roads = fetch_roads_online(min_lon, min_lat, max_lon, max_lat, ref_lon, ref_lat)
        if roads:
            road_polygons = create_road_polygons(roads)
            road_source = 'OVERSPASS_ONLINE'
            st.info("🌐 Data jalan dari Overpass API (online) berhasil diambil.")
        else:
            st.warning("⚠️ Gagal mengambil data dari Overpass, menggunakan fallback koridor.")

    # PRIORITAS 3: Fallback koridor dari kabel
    if not road_polygons and converted_cables:
        polygons = []
        for cable in converted_cables:
            coords = cable['coords']
            if len(coords) >= 2:
                line = LineString(coords)
                try:
                    poly = line.buffer(4.0, cap_style=2, join_style=2)
                    if not poly.is_empty:
                        polygons.append(poly)
                except Exception:
                    continue
        if polygons:
            try:
                dissolved = unary_union(polygons)
                if not dissolved.is_empty:
                    if dissolved.geom_type == 'Polygon':
                        road_polygons = [{'geometry': dissolved, 'name': ''}]
                    elif dissolved.geom_type == 'MultiPolygon':
                        road_polygons = [{'geometry': g, 'name': ''} for g in dissolved.geoms]
                    road_source = 'FALLBACK_CORRIDOR'
            except Exception:
                pass

    return {
        'cables': converted_cables,
        'poles': converted_poles,
        'road_polygons': road_polygons,
        'road_source': road_source,
        'inspector': inspector_logs,
        'ref': (ref_lon, ref_lat),
        'bbox': (min_lon, min_lat, max_lon, max_lat)
    }

# ==========================================
# 6. DXF GENERATOR
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

    # ===== JALAN (exterior dari poligon hasil dissolve) =====
    for road in data.get('road_polygons', []):
        geom = road.get('geometry')
        if geom is None:
            continue
        try:
            if geom.geom_type == 'Polygon':
                coords = list(geom.exterior.coords)
                if len(coords) >= 4:
                    msp.add_lwpolyline(coords, close=True,
                                      dxfattribs={'layer': '01_BADAN_JALAN', 'color': 8})
            elif geom.geom_type == 'MultiPolygon':
                for poly in geom.geoms:
                    coords = list(poly.exterior.coords)
                    if len(coords) >= 4:
                        msp.add_lwpolyline(coords, close=True,
                                          dxfattribs={'layer': '01_BADAN_JALAN', 'color': 8})
        except Exception:
            continue

    # ===== KABEL =====
    for cable in data['cables']:
        pts = cable['coords']
        if len(pts) < 2:
            continue
        for pt in pts:
            all_x.append(pt[0]); all_y.append(pt[1])
        msp.add_lwpolyline(pts, dxfattribs={'layer': '03_KABEL_FO', 'color': 1, 'lineweight': 50})

    # ===== TIANG & AKSESORIS =====
    for p in data['poles']:
        pos = p['coords']
        name = p['name']
        all_x.append(pos[0]); all_y.append(pos[1])

        msp.add_circle(pos, radius=0.8, dxfattribs={'layer': '04_POLE_TIANG', 'color': 3})
        display_name = smart_rename(name)

        if p['has_accessories']:
            msp.add_lwpolyline([
                (pos[0]-1.2, pos[1]-1.2),
                (pos[0]+1.2, pos[1]-1.2),
                (pos[0]+1.2, pos[1]+1.2),
                (pos[0]-1.2, pos[1]+1.2)
            ], close=True, dxfattribs={'layer': '05_SMARTBOX_SLACK', 'color': 2})
            msp.add_line(pos, (pos[0]+1.8, pos[1]-0.5),
                        dxfattribs={'layer': '05_SMARTBOX_SLACK', 'color': 2})
            msp.add_text("New Slack Support", dxfattribs={
                'layer': '05_SMARTBOX_SLACK',
                'height': 0.8,
                'color': 2
            }).set_placement((pos[0] + 1.8, pos[1] - 0.5))

        text_y_offset = 0.8 if not p['is_overlap'] else 1.8
        msp.add_text(display_name, dxfattribs={
            'layer': '04_POLE_TIANG',
            'height': 0.9,
            'color': 3
        }).set_placement((pos[0] + 1.8, pos[1] + text_y_offset))

    # ===== LAYOUT A3 =====
    if "Paper_A3" in doc.layouts:
        layout = doc.layouts.get("Paper_A3")
    else:
        layout = doc.layouts.new("Paper_A3")

    layout.dxf.paper_width = 420.0
    layout.dxf.paper_height = 297.0

    if all_x and all_y:
        min_x, max_x = min(all_x), max(all_x)
        min_y, max_y = min(all_y), max(all_y)
    else:
        min_x = min_y = 0
        max_x = max_y = 100

    cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    h = max_y - min_y or 100.0

    layout.add_viewport(
        center=(160, 148),
        size=(280.0, 200.0),
        view_center_point=(cx, cy),
        view_height=h * 1.18
    )

    # ===== TITLE BLOCK =====
    tb_x1, tb_y1 = 300, 20
    tb_x2, tb_y2 = 410, 270
    layout.add_lwpolyline([
        (tb_x1, tb_y1), (tb_x2, tb_y1),
        (tb_x2, tb_y2), (tb_x1, tb_y2)
    ], close=True, dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'color': 7, 'lineweight': 25})

    def add_title_text(text, x, y, height=2.5):
        layout.add_text(text, dxfattribs={
            'layer': 'KOP_TITLE_BLOCK',
            'height': height,
            'color': 7
        }).set_placement((x, y))

    y_pos = tb_y2 - 8
    add_title_text(f"SPAN: {proj_info.get('span_name', '')}", tb_x1+5, y_pos, 3.5)
    y_pos -= 10
    add_title_text(f"PROJECT: {proj_info.get('project_code', '')}", tb_x1+5, y_pos, 3.0)
    y_pos -= 8
    add_title_text(f"TANGGAL: {datetime.now().strftime('%d-%m-%Y')}", tb_x1+5, y_pos, 2.5)
    y_pos -= 6
    add_title_text(f"REVISI: {proj_info.get('revision', '0')}", tb_x1+5, y_pos, 2.5)
    y_pos -= 6
    skala = int(200.0/h*10) if h > 0 else 1
    add_title_text(f"SKALA: 1:{skala}", tb_x1+5, y_pos, 2.5)
    y_pos -= 8
    layout.add_line((tb_x1, y_pos), (tb_x2, y_pos), dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'color': 7})
    y_pos -= 5
    add_title_text(f"DRAWN: {proj_info.get('drawn_by', '')}", tb_x1+5, y_pos, 2.5)
    y_pos -= 5
    add_title_text(f"CHECKED: {proj_info.get('checked_by', '')}", tb_x1+5, y_pos, 2.5)
    y_pos -= 5
    add_title_text(f"APPROVED: {proj_info.get('approved_by', '')}", tb_x1+5, y_pos, 2.5)

    add_title_text("PT. RIZKI PRIMA SAKTI", tb_x1+5, tb_y1+5, 2.5)
    add_title_text("CLIENT: iFORTE", tb_x1+5, tb_y1+12, 2.5)

    # ===== LEGENDA =====
    leg_x1, leg_y1 = 20, 20
    leg_x2, leg_y2 = 120, 80
    layout.add_lwpolyline([
        (leg_x1, leg_y1), (leg_x2, leg_y1),
        (leg_x2, leg_y2), (leg_x1, leg_y2)
    ], close=True, dxfattribs={'layer': 'LEGENDA', 'color': 7})

    layout.add_text("LEGENDA", dxfattribs={
        'layer': 'LEGENDA', 'height': 2.5, 'color': 7
    }).set_placement((leg_x1+5, leg_y2-5))

    legend_items = [
        ("Kabel FO", 1),
        ("Tiang", 3),
        ("Closure/Slack", 2),
        ("Jalan", 8)
    ]
    y_pos = leg_y2 - 15
    for label, color in legend_items:
        layout.add_text(f"● {label}", dxfattribs={
            'layer': 'LEGENDA', 'height': 2.0, 'color': color
        }).set_placement((leg_x1+5, y_pos))
        y_pos -= 6

    out_bytes = io.StringIO()
    doc.write(out_bytes)
    return out_bytes.getvalue()

# ==========================================
# 7. STREAMLIT UI
# ==========================================

st.sidebar.title("📇 Informasi Proyek")
span_name = st.sidebar.text_input("SPAN NAME", "14PBG007_REMBANGPBLG")
project_code = st.sidebar.text_input("PROJECT CODE", "RM-26-000327")
drawn_by = st.sidebar.text_input("DRAWN BY", "RPS")
checked_by = st.sidebar.text_input("CHECKED BY", "IFORTE")
approved_by = st.sidebar.text_input("APPROVED BY", "IFORTE")
revision = st.sidebar.text_input("REVISION", "0")

st.sidebar.markdown("---")
st.sidebar.markdown("### 🗺️ Data Jalan Offline (Opsional)")
geojson_path = st.sidebar.text_input(
    "Path ke file GeoJSON jalan",
    value="indonesia_roads.geojson",
    help="Download dari Geofabrik: https://download.geofabrik.de/asia/indonesia.html"
)
st.sidebar.caption("Kosongkan jika ingin menggunakan Overpass API online")

st.title("⚡ ASPLAN PRO v14.2 - Buffer → Dissolve → Boundary")
st.caption("Prioritas: GeoJSON Lokal → Overpass API → Fallback (dengan buffer & dissolve)")

uploaded_file = st.file_uploader("📂 Upload File KMZ", type=['kmz'])

if uploaded_file:
    with st.spinner("🔄 Memproses data..."):
        parsed = parse_kmz(uploaded_file.read(), geojson_path if geojson_path.strip() else None)

    if not parsed:
        st.error("❌ Gagal memproses file KMZ.")
    else:
        st.success(f"✅ Data berhasil diproses! Sumber jalan: {parsed['road_source']}")

        col1, col2 = st.columns([1, 1])

        with col1:
            st.markdown("### 📐 Preview")
            fig, ax = plt.subplots(figsize=(6, 5))
            ax.set_facecolor('#0e1117')
            fig.patch.set_facecolor('#0e1117')

            # Jalan (exterior dari poligon hasil dissolve)
            for r in parsed['road_polygons']:
                geom = r.get('geometry')
                if geom is None:
                    continue
                try:
                    if geom.geom_type == 'Polygon':
                        xs, ys = geom.exterior.xy
                        ax.fill(xs, ys, color='#444444', alpha=0.5)
                        ax.plot(xs, ys, color='#ffffff', linewidth=1.2, alpha=0.8)
                    elif geom.geom_type == 'MultiPolygon':
                        for poly in geom.geoms:
                            xs, ys = poly.exterior.xy
                            ax.fill(xs, ys, color='#444444', alpha=0.5)
                            ax.plot(xs, ys, color='#ffffff', linewidth=1.2, alpha=0.8)
                except Exception:
                    pass

            # Kabel
            for c in parsed['cables']:
                xs = [p[0] for p in c['coords']]
                ys = [p[1] for p in c['coords']]
                ax.plot(xs, ys, color='#ff4b4b', linewidth=2.5)

            # Tiang
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
                st.success("✅ Semua geometri presisi!")

            st.markdown("---")
            st.markdown("**📊 Statistik:**")
            st.write(f"- Segmen Kabel: {len(parsed['cables'])}")
            st.write(f"- Titik Tiang: {len(parsed['poles'])}")
            st.write(f"- Poligon Jalan (setelah dissolve): {len(parsed['road_polygons'])}")
            st.write(f"- Sumber Jalan: **{parsed['road_source']}**")

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

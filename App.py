import streamlit as st
import ezdxf
import osmnx as ox
import math
import io
import zipfile
import xml.etree.ElementTree as ET
import matplotlib.pyplot as plt
from datetime import datetime
import re
import pandas as pd
from shapely.geometry import LineString, Polygon, MultiPolygon
from shapely.ops import unary_union

# ==========================================
# 1. KONFIGURASI SISTEM
# ==========================================
st.set_page_config(page_title="ASPLAN PRO v17 - Hybrid Engine", layout="wide")

# Mengadopsi pengaturan timeout tangguh dari skrip referensi
ox.settings.timeout = 180 
ox.settings.use_cache = True
ox.settings.user_agent = "AsplanPro_v17_Streamlit"

def smart_rename(name):
    """Logika Rename Aksesoris & Closure Paten"""
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
# 2. PARSER KMZ LAPANGAN
# ==========================================
def parse_kmz(kmz_bytes):
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
                    lon, lat = float(p[0]), float(p[1])
                    pts.append((lon, lat))
                    all_lons.append(lon); all_lats.append(lat)
            if pts: cables.append({'name': pm.findtext('kml:name', '', ns), 'coords': pts})

        point = pm.find('.//kml:Point/kml:coordinates', ns)
        if point is not None and point.text:
            p = point.text.strip().split(',')
            if len(p) >= 2:
                lon, lat = float(p[0]), float(p[1])
                name = pm.findtext('kml:name', 'Pole', ns)
                desc = pm.findtext('kml:description', '', ns).lower()
                has_acc = any(k in desc for k in ['acc', 'accessories', 'slack', 'box', 'closure', 'odp'])
                all_lons.append(lon); all_lats.append(lat)
                poles.append({'name': name, 'lon': lon, 'lat': lat, 'has_accessories': has_acc})

    if not all_lons: return None
    return cables, poles, all_lons, all_lats

# ==========================================
# 3. ENGINE UTAMA: GRAPH -> BUFFER -> DISSOLVE
# ==========================================
def process_hybrid_engine(kmz_bytes):
    parsed = parse_kmz(kmz_bytes)
    if not parsed: return None
    cables, poles, all_lons, all_lats = parsed

    # Kunci Titik Pusat (Center Reference)
    c_lon, c_lat = sum(all_lons) / len(all_lons), sum(all_lats) / len(all_lats)
    m_lat = 111320
    m_lon = 111320 * math.cos(math.radians(c_lat))
    
    def to_m_pts(coords): 
        return [((p[0] - c_lon) * m_lon, (p[1] - c_lat) * m_lat) for p in coords]

    # Konversi KMZ ke Meter
    cables_m = [{'name': c['name'], 'coords': to_m_pts(c['coords'])} for c in cables]
    poles_m = [{'name': p['name'], 'coords': to_m_pts([(p['lon'], p['lat'])])[0], 'raw': (p['lon'], p['lat']), 'has_accessories': p['has_accessories']} for p in poles]

    # Hitung Jarak Ekstraksi Peta (Mengadopsi efisiensi memori)
    dist_lat = (max(all_lats) - min(all_lats)) * 111139
    dist_lon = (max(all_lons) - min(all_lons)) * 111139
    download_dist = (max(dist_lat, dist_lon) / 2) + 500  # Radius dinamis

    road_polygons = []
    
    try:
        # Mengadopsi penarikan Graph Edges (Sangat Ringan & Cepat)
        graph = ox.graph_from_point((c_lat, c_lon), dist=download_dist, network_type='all', simplify=True)
        _, edges = ox.graph_to_gdfs(graph)
        
        all_road_polys = []
        for _, row in edges.iterrows():
            m_pts = to_m_pts(list(row.geometry.coords))
            if len(m_pts) < 2: continue
            
            # Lebar jalan adaptif
            w = 6.0 if row.get('highway') in ['primary', 'secondary'] else 3.5 
            
            # Mengadopsi Buffer & Fillet Halus 32-segmen
            poly = LineString(m_pts).buffer(w, cap_style=1, join_style=1, quad_segs=32)
            if not poly.is_empty and poly.is_valid:
                all_road_polys.append(poly)
                
        if all_road_polys:
            # Dissolve sempurna
            merged = unary_union(all_road_polys)
            road_polygons = [merged] if merged.geom_type == 'Polygon' else list(merged.geoms)

    except Exception as e:
        st.warning(f"OSMnx Fallback: {e}")
        # Fallback Koridor Darurat
        raw_polys = [LineString(c['coords']).buffer(4.0, cap_style=1, join_style=1, quad_segs=32) for c in cables_m if len(c['coords']) >= 2]
        if raw_polys:
            merged = unary_union(raw_polys)
            road_polygons = [merged] if merged.geom_type == 'Polygon' else list(merged.geoms)

    return {
        'cables': cables_m, 'poles': poles_m,
        'road_polygons': road_polygons,
        'ref': (c_lon, c_lat)
    }

# ==========================================
# 4. GENERATOR DXF
# ==========================================
def build_dxf(data):
    doc = ezdxf.new(setup=True)
    doc.styles.new("MAP_FONT", dxfattribs={"font": "arial.ttf"})
    msp = doc.modelspace()

    # Setup Layer Paten
    doc.layers.new("01_PETA_DASAR", dxfattribs={'color': 8})
    doc.layers.new("03_KABEL_BIRU", dxfattribs={'color': 5})
    doc.layers.new("04_TIANG_MERAH", dxfattribs={'color': 1})
    doc.layers.new("05_SMARTBOX", dxfattribs={'color': 7})

    # Mengadopsi fungsi recursive draw_g untuk menggambar Eksterior & Lubang Interior
    def draw_g(g):
        if isinstance(g, Polygon):
            if g.exterior:
                msp.add_lwpolyline(list(g.exterior.coords), dxfattribs={'layer': '01_PETA_DASAR'}, close=True)
            for i in g.interiors: 
                msp.add_lwpolyline(list(i.coords), dxfattribs={'layer': '01_PETA_DASAR'}, close=True)
        elif isinstance(g, MultiPolygon): 
            for p in g.geoms: draw_g(p)

    for poly in data['road_polygons']:
        draw_g(poly)

    for c in data['cables']:
        if len(c['coords']) >= 2:
            msp.add_lwpolyline(c['coords'], dxfattribs={'layer': '03_KABEL_BIRU', 'lineweight': 40})

    pt_a = data['poles'][0]['name'] if data['poles'] else ""
    pt_b = data['poles'][-1]['name'] if data['poles'] else ""

    for p in data['poles']:
        pos = p['coords']
        name = p['name']
        acc_name = smart_rename(name)
        has_acc = (acc_name != name) and (acc_name != "")

        msp.add_circle(pos, radius=1.2, dxfattribs={'layer': '04_TIANG_MERAH'})

        if (name in [pt_a, pt_b]) or has_acc or p['has_accessories']:
            disp = [acc_name] if has_acc else []
            box_lines = [f"POLE: {name}"] + [f"+ {n}" for n in disp] + [f"Lat: {p['raw'][1]:.6f}", f"Lon: {p['raw'][0]:.6f}"]
            bx, by = pos[0] + 15.0, pos[1] + 15.0
            
            msp.add_line(pos, (bx, by), dxfattribs={'layer': '05_SMARTBOX'})
            
            p1, p2, p3, p4 = (bx, by-15), (bx+120, by-15), (bx+120, by+20), (bx, by+20)
            msp.add_lwpolyline([p1, p2, p3, p4], dxfattribs={'layer': '05_SMARTBOX'}, close=True)
            
            for i, txt in enumerate(box_lines):
                msp.add_text(txt, dxfattribs={'layer': '05_SMARTBOX', 'height': 4.0, 'style': 'MAP_FONT'}).set_placement((bx+2, (by+12)-(i*5)))
        else:
            msp.add_text(name, dxfattribs={'layer': '04_TIANG_MERAH', 'height': 2.5, 'style': 'MAP_FONT'}).set_placement((pos[0] + 2.0, pos[1] + 2.0))

    out_bytes = io.StringIO()
    doc.write(out_bytes)
    return out_bytes.getvalue()

# ==========================================
# 5. STREAMLIT INTERFACE
# ==========================================
st.title("⚡ ASPLAN PRO v17 - The Hybrid Engine")
st.caption("Auto-Routing OSNMnx + Smooth Fillet 32-Segmen + Smart Boundary Extraction")

uploaded_file = st.file_uploader("📂 Upload File KMZ Lapangan", type=['kmz'])

if uploaded_file:
    with st.spinner("🔄 Memproses Geometri (Graph Extraction & Fillet Buffer)..."):
        parsed_data = process_hybrid_engine(uploaded_file.read())

    if not parsed_data:
        st.error("❌ File KMZ kosong atau format tidak sesuai.")
    else:
        st.success("✅ Pemrosesan Selesai! Peta jalan berhasil di-render tanpa timeout.")

        col1, col2 = st.columns([1.5, 1])

        with col1:
            st.markdown("### 📐 Viewport Preview (Skala Asli)")
            fig, ax = plt.subplots(figsize=(8, 7), facecolor='#0e1117')
            ax.set_facecolor('#0e1117')

            # Render Peta Jalan (Eksterior & Interior Mencegah Blok Raksasa)
            for poly in parsed_data['road_polygons']:
                if poly.geom_type in ['Polygon', 'MultiPolygon']:
                    polys = poly.geoms if poly.geom_type == 'MultiPolygon' else [poly]
                    for p in polys:
                        if p.exterior:
                            xs, ys = p.exterior.xy
                            ax.fill(xs, ys, color='#555555', alpha=0.9, zorder=1)
                            ax.plot(xs, ys, color='#ffffff', linewidth=1.5, zorder=2)
                        for i in p.interiors:
                            ixs, iys = i.xy
                            ax.fill(ixs, iys, color='#0e1117', zorder=1) # Mengosongkan lubang
                            ax.plot(ixs, iys, color='#ffffff', linewidth=1.5, zorder=2)

            # Render Kabel Biru
            for c in parsed_data['cables']:
                if len(c['coords']) >= 2:
                    xs, ys = zip(*c['coords'])
                    ax.plot(xs, ys, color='#00a8ff', linewidth=3.0, zorder=3)

            # Render Tiang Merah
            if parsed_data['poles']:
                pxs, pys = zip(*[p['coords'] for p in parsed_data['poles']])
                ax.scatter(pxs, pys, color='#ff4757', s=35, zorder=4)

            ax.set_aspect('equal', adjustable='datalim')
            ax.tick_params(colors='white')
            ax.grid(True, color='#222222', linestyle=':')
            st.pyplot(fig)

        with col2:
            st.markdown("### 📊 Ringkasan Eksekusi")
            st.info(f"🛣️ Badan Jalan Tersambung: **{len(parsed_data['road_polygons'])} Geometri**")
            st.write(f"- Segmen Kabel FO: **{len(parsed_data['cables'])}**")
            st.write(f"- Jumlah Tiang (Pole): **{len(parsed_data['poles'])}**")

            dxf_data = build_dxf(parsed_data)
            st.download_button(
                label="💾 DOWNLOAD FILE DXF (AS-BUILT PRESISI)",
                data=dxf_data,
                file_name=f"{uploaded_file.name.replace('.kmz', '')}_ASPLAN_V17.dxf",
                mime="application/dxf",
                type="primary",
                use_container_width=True
            )

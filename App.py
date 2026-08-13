import streamlit as st
import os
import ezdxf
import osmnx as ox
import math
from zipfile import ZipFile
import xml.etree.ElementTree as ET
from shapely.geometry import LineString, Point, Polygon, MultiPolygon
from shapely.ops import unary_union
import re
from datetime import datetime
import io
import pandas as pd
import matplotlib.pyplot as plt

# ==============================================================================
# CONFIG & PAGE SETUP
# ==============================================================================
st.set_page_config(
    page_title="ASPLAN PRO v10.9 - KMZ to DXF Converter & Precision Inspector",
    page_icon="🗺️",
    layout="wide"
)

ox.settings.use_cache = True
ox.settings.timeout = 1800
ox.settings.user_agent = "AsplanPro_v10.9"

ROAD_WIDTHS = {
    'motorway': 20.0, 'trunk': 16.0, 'primary': 14.0, 
    'secondary': 12.0, 'tertiary': 10.0, 'residential': 8.0, 
    'service': 5.0, 'unclassified': 7.0
}

# ==============================================================================
# HELPER FUNCTIONS & SMART LOGIC
# ==============================================================================
def parse_kml_brute_force(kml_bytes):
    features = {'lines': [], 'points': []}
    kml_str = kml_bytes.decode('utf-8', errors='ignore')
    kml_str = re.sub(r'xmlns="[^"]+"', '', kml_str, count=1)
    try:
        root = ET.fromstring(kml_str)
    except Exception:
        return features
    
    for pm in root.findall('.//Placemark'):
        name = pm.find('name').text if pm.find('name') is not None and pm.find('name').text else "No Name"
        
        # Parse LineString (kabel)
        ls = pm.find('.//LineString/coordinates')
        if ls is not None and ls.text:
            coords = []
            for c in ls.text.strip().split():
                parts = c.split(',')
                if len(parts) >= 2:
                    try:
                        coords.append((float(parts[0]), float(parts[1])))
                    except ValueError:
                        continue
            if len(coords) >= 2:
                features['lines'].append({
                    'geom': LineString(coords), 
                    'name': name,
                    'coords': coords
                })
        
        # Parse Point (tiang)
        pt = pm.find('.//Point/coordinates')
        if pt is not None and pt.text:
            c = pt.text.strip().split(',')
            if len(c) >= 2:
                try:
                    features['points'].append({
                        'geom': Point(float(c[0]), float(c[1])), 
                        'name': name, 
                        'orig': (float(c[0]), float(c[1]))
                    })
                except ValueError:
                    continue
    
    return features

def smart_rename(name):
    """Logika Rename Paten Kak Donny (Clean Handling)"""
    if not name or pd.isna(name):
        return ""
    n = str(name).upper().strip()
    if any(x in n for x in ["SLACK", ".SS", "SS"]): 
        return "New Slack Support"
    if any(x in n for x in ["CLOS", "CL24", "CL48", "CL96", "CLS", "C24", "C48", "C96"]):
        numbers = re.findall(r'\d+', n)
        num = max([int(x) for x in numbers]) if numbers else ""
        return f"New Closure {num}C" if num else "New Closure"
    return name

def calculate_cable_bounding_box(data):
    if not data['lines']:
        return None, None, None, None
    all_coords = [coord for line in data['lines'] for coord in line['coords']]
    lats = [coord[1] for coord in all_coords]
    lons = [coord[0] for coord in all_coords]
    buffer_deg = 0.015
    return max(lats) + buffer_deg, min(lats) - buffer_deg, max(lons) + buffer_deg, min(lons) - buffer_deg

def get_center_point_from_kml(data):
    if not data['lines']:
        lats = [p['orig'][1] for p in data['points']]
        lons = [p['orig'][0] for p in data['points']]
        return sum(lats)/len(lats), sum(lons)/len(lons)
    cable_coords = data['lines'][0]['coords']
    mid_idx = len(cable_coords) // 2
    return cable_coords[mid_idx][1], cable_coords[mid_idx][0]

def create_road_buffer_fixed(line_coords_meters, width):
    if len(line_coords_meters) < 2:
        return None
    line = LineString(line_coords_meters)
    effective_width = max(width, 3.0)
    try:
        polygon = line.buffer(
            effective_width / 2, cap_style=2, join_style=2, resolution=16, quad_segs=8
        )
        if isinstance(polygon, Polygon) and not polygon.is_empty:
            return polygon
        return line.buffer(effective_width / 2)
    except Exception:
        return line.buffer(effective_width / 2)

def add_road_label(msp, line_coords, road_name, layer_name):
    if not road_name or pd.isna(road_name) or str(road_name).lower() == 'nan':
        return
    
    road_str = str(road_name).strip()
    if not road_str or len(line_coords) < 2:
        return
    
    line = LineString(line_coords)
    length = line.length
    if length < 80:
        return
    
    mid_point = line.interpolate(0.5, normalized=True)
    p1 = Point(line_coords[0])
    p2 = Point(line_coords[-1])
    dx = p2.x - p1.x
    dy = p2.y - p1.y
    
    angle_rad = math.atan2(dy, dx)
    angle_deg = math.degrees(angle_rad)
    
    while angle_deg > 90:
        angle_deg -= 180
    while angle_deg < -90:
        angle_deg += 180
    
    font_height = 2.5 if length > 150 else 1.8
    offset_dist = font_height * 1.5
    offset_x = -offset_dist * math.sin(angle_rad)
    offset_y = offset_dist * math.cos(angle_rad)
    
    msp.add_text(
        road_str,
        dxfattribs={
            'layer': layer_name,
            'height': font_height,
            'rotation': angle_deg,
            'style': 'ARIAL_STD',
            'color': 7
        }
    ).set_placement((mid_point.x + offset_x, mid_point.y + offset_y))

def draw_smartbox(msp, bx, by, pole_name, acc_names, coords):
    box_lines = [f"POLE: {pole_name}"] + [f"+ {n}" for n in acc_names] + [f"Lat: {coords[1]:.6f}", f"Lon: {coords[0]:.6f}"]
    tx, ty = bx + 12, by + 12
    
    msp.add_line((bx, by), (tx, ty), dxfattribs={'layer': '05_SMARTBOX', 'color': 1})
    
    ch = 1.8
    mtext = msp.add_mtext("\n".join(box_lines), dxfattribs={
        'layer': '05_SMARTBOX', 
        'style': 'ARIAL_STD', 
        'char_height': ch, 
        'color': 1
    })
    mtext.set_location((tx, ty), attachment_point=7)
    
    bw = max(len(l) for l in box_lines) * ch * 0.65
    bh = len(box_lines) * ch * 1.6
    pts = [(tx-1, ty+1), (tx+bw+2, ty+1), (tx+bw+2, ty-bh-1), (tx-1, ty-bh-1)]
    msp.add_lwpolyline(pts, dxfattribs={'layer': '05_SMARTBOX', 'color': 1}, close=True)

# ==============================================================================
# INSPECTOR ENGINE (DETEKSI KESALAHAN PRESISI)
# ==============================================================================
def inspect_cad_precision(data, road_labels_data, road_polygons):
    issues = []
    
    # 1. Cek Teks NaN
    for idx, lbl in enumerate(road_labels_data):
        name = lbl.get('name', '')
        if pd.isna(name) or str(name).lower() == 'nan' or not str(name).strip():
            issues.append({
                'level': '❌ ERROR',
                'category': 'Atribut Teks',
                'detail': f"Ruas Jalan #{idx+1} memiliki label bernilai NaN / Kosong."
            })

    # 2. Cek Tiang Overlap (<0.1m)
    points = data.get('points', [])
    for i in range(len(points)):
        for j in range(i + 1, len(points)):
            p1 = points[i]['geom']
            p2 = points[j]['geom']
            if p1.distance(p2) < 0.000001:
                issues.append({
                    'level': '⚠️ WARNING',
                    'category': 'Overlap Geometry',
                    'detail': f"Tiang '{points[i]['name']}' bertumpuk persis dengan '{points[j]['name']}'."
                })

    # 3. Cek Ujung Kabel Putus (Dangling Kabel Ends)
    lines = data.get('lines', [])
    if lines and points:
        all_pole_geoms = [p['geom'] for p in points]
        for line in lines:
            ls = line['geom']
            start_pt = Point(ls.coords[0])
            end_pt = Point(ls.coords[-1])
            
            min_dist_start = min([start_pt.distance(p) for p in all_pole_geoms]) * 111000
            min_dist_end = min([end_pt.distance(p) for p in all_pole_geoms]) * 111000
            
            if min_dist_start > 2.0:
                issues.append({
                    'level': '⚠️ WARNING',
                    'category': 'Presisi Kabel',
                    'detail': f"Ujung awal Kabel '{line['name']}' tidak menempel ke tiang (>2m)."
                })
            if min_dist_end > 2.0:
                issues.append({
                    'level': '⚠️ WARNING',
                    'category': 'Presisi Kabel',
                    'detail': f"Ujung akhir Kabel '{line['name']}' tidak menempel ke tiang (>2m)."
                })

    return issues

# ==============================================================================
# VIEWPORT PREVIEW CANVAS (MATPLOTLIB)
# ==============================================================================
def render_compact_viewport(data, road_polygons, to_m_func):
    fig, ax = plt.subplots(figsize=(5, 5), facecolor='#0e1117')
    ax.set_facecolor('#0e1117')

    # Draw Road Polygons
    if road_polygons:
        for poly in road_polygons:
            if isinstance(poly, Polygon):
                x, y = poly.exterior.xy
                ax.plot(x, y, color='#555555', linewidth=0.8, alpha=0.8)
            elif isinstance(poly, MultiPolygon):
                for p in poly.geoms:
                    x, y = p.exterior.xy
                    ax.plot(x, y, color='#555555', linewidth=0.8, alpha=0.8)

    # Draw Cables
    for line in data['lines']:
        m_coords = [to_m_func(c[0], c[1]) for c in line['coords']]
        xs, ys = zip(*m_coords)
        ax.plot(xs, ys, color='#00a8ff', linewidth=1.5, zorder=3)

    # Draw Poles
    for pt in data['points']:
        mx, my = to_m_func(pt['orig'][0], pt['orig'][1])
        ax.scatter(mx, my, color='#ff4757', s=18, zorder=5)

    ax.set_aspect('equal', adjustable='datalim')
    ax.tick_params(colors='#888888', labelsize=7)
    ax.grid(True, color='#222222', linestyle=':', linewidth=0.5)
    plt.title("Live Viewport Preview (CAD Canvas)", color='white', fontsize=10, pad=10)
    plt.tight_layout()
    return fig

# ==============================================================================
# MAIN PROCESSING & STREAMLIT UI
# ==============================================================================
st.title("⚡ ASPLAN PRO v10.9")
st.subheader("Interactive Preview & CAD Precision Inspector")

uploaded_files = st.file_uploader("Pilih File KMZ", type=['kmz'], accept_multiple_files=True)

if uploaded_files:
    for idx, file in enumerate(uploaded_files):
        st.divider()
        st.markdown(f"### 📦 File: `{file.name}`")
        
        try:
            kmz_bytes = file.read()
            with ZipFile(io.BytesIO(kmz_bytes), 'r') as zf:
                kml_files = [n for n in zf.namelist() if n.endswith('.kml')]
                if not kml_files:
                    st.error("Tidak ada file KML di dalam KMZ.")
                    continue
                with zf.open(kml_files[0]) as f:
                    data = parse_kml_brute_force(f.read())

            center_lat, center_lon = get_center_point_from_kml(data)
            cable_length_deg = sum([
                math.sqrt((l['coords'][-1][0]-l['coords'][0][0])**2 + (l['coords'][-1][1]-l['coords'][0][1])**2)
                for l in data['lines'] if len(l['coords']) >= 2
            ])
            cable_length_meters = cable_length_deg * 111000
            download_radius = max(cable_length_meters / 2, 1000) + 1000

            m_lat = 111320
            m_lon = 111320 * math.cos(math.radians(center_lat))
            def to_m(lon, lat): return ((lon - center_lon) * m_lon, (lat - center_lat) * m_lat)

            # Download OSM Roads
            road_polygons = []
            road_labels_data = []
            try:
                graph = ox.graph_from_point((center_lat, center_lon), dist=download_radius, network_type='all', simplify=True)
                _, edges = ox.graph_to_gdfs(graph)
                if not edges.empty:
                    cable_pts_m = [to_m(c[0], c[1]) for line in data['lines'] for c in line['coords']]
                    for _, row in edges.iterrows():
                        highway = row.get('highway', '')
                        if isinstance(highway, list): highway = highway[0] if highway else ''
                        if not highway: continue

                        lines_geom = list(row.geometry.geoms) if row.geometry.geom_type == 'MultiLineString' else [row.geometry]
                        for line in lines_geom:
                            if line.geom_type != 'LineString': continue
                            m_coords = [to_m(c[0], c[1]) for c in line.coords]
                            if len(m_coords) < 2: continue
                            
                            line_shapely = LineString(m_coords)
                            if min(line_shapely.distance(Point(cp)) for cp in cable_pts_m) > 200: continue
                            
                            poly = create_road_buffer_fixed(m_coords, ROAD_WIDTHS.get(highway, 8.0))
                            if poly:
                                road_polygons.append(poly)
                                r_name = row.get('name', '') or row.get('ref', '')
                                if r_name and not pd.isna(r_name):
                                    road_labels_data.append({'coords': m_coords, 'name': str(r_name), 'length': line_shapely.length})
            except Exception as e:
                st.caption(f"Note OSM: {e}")

            # Layout 2 Kolom: Viewport Mini Box & Inspector Panel
            col_view, col_inspect = st.columns([1, 1.2])

            with col_view:
                st.markdown("#### 📐 Viewport Preview Box")
                fig = render_compact_viewport(data, road_polygons, to_m)
                st.pyplot(fig, use_container_width=False)

            with col_inspect:
                st.markdown("#### 🔍 Precision & Quality Inspector")
                issues = inspect_cad_precision(data, road_labels_data, road_polygons)
                
                if not issues:
                    st.success("✅ **Gambar Sempurna!** Tidak ditemukan kesalahan presisi pada geometri atau teks.")
                else:
                    st.warning(f"⚠️ Ditemukan **{len(issues)} potensi kesalahan presisi**:")
                    df_issues = pd.DataFrame(issues)
                    st.dataframe(df_issues, use_container_width=True, hide_index=True)

            # Ekspor DXF
            doc = ezdxf.new(setup=True)
            doc.styles.new("ARIAL_STD", dxfattribs={"font": "arial.ttf"})
            msp = doc.modelspace()
            for l_name, col in {'01_BADAN_JALAN': 8, '02_NAMA_JALAN': 7, '03_KABEL': 5, '04_POLE': 7, '05_SMARTBOX': 1}.items():
                doc.layers.new(l_name, dxfattribs={'color': col})

            if road_polygons:
                merged = unary_union(road_polygons)
                polys = merged.geoms if isinstance(merged, MultiPolygon) else [merged]
                for p in polys:
                    if isinstance(p, Polygon) and p.exterior:
                        msp.add_lwpolyline(list(p.exterior.coords), dxfattribs={'layer': '01_BADAN_JALAN', 'color': 8}, close=True)

            for lbl in road_labels_data:
                add_road_label(msp, lbl['coords'], lbl['name'], '02_NAMA_JALAN')

            for line in data['lines']:
                msp.add_lwpolyline([to_m(c[0], c[1]) for c in line['coords']], dxfattribs={'layer': '03_KABEL', 'color': 5, 'lineweight': 40})

            pt_a = data['points'][0]['name'] if data['points'] else ""
            pt_b = data['points'][-1]['name'] if data['points'] else ""

            for pt in data['points']:
                mx, my = to_m(pt['orig'][0], pt['orig'][1])
                p_name = pt['name']
                acc_name = smart_rename(p_name)
                has_acc = (acc_name != p_name) and (acc_name != "")

                if not any(x in p_name.upper() for x in ["ODP", "OTB"]):
                    msp.add_circle((mx, my), 1.5, dxfattribs={'layer': '04_POLE', 'color': 7})

                if (p_name in [pt_a, pt_b]) or has_acc or any(x in p_name.upper() for x in ["ODP", "OTB"]):
                    disp = [acc_name] if has_acc else []
                    draw_smartbox(msp, mx, my, p_name, disp, pt['orig'])
                else:
                    msp.add_text(p_name, dxfattribs={'layer': '04_POLE', 'height': 2.0, 'style': 'ARIAL_STD', 'color': 7}).set_placement((mx + 3, my + 3))

            out_stream = io.StringIO()
            doc.write(out_stream)
            
            output_filename = file.name.replace('.kmz', f'_FIXED_{datetime.now().strftime("%Y%m%d_%H%M%S")}.dxf')
            st.download_button(
                label=f"💾 Download File DXF Hasil Revisi ({file.name})",
                data=out_stream.getvalue(),
                file_name=output_filename,
                mime="application/dxf",
                type="primary"
            )

        except Exception as e:
            st.error(f"Gagal memproses file: {e}")

import streamlit as st
import ezdxf
from ezdxf import units
import xml.etree.ElementTree as ET
import zipfile
import math
import io
import matplotlib.pyplot as plt

st.set_page_config(page_title="ASPLAN PRO v12.1", layout="wide")

# ==========================================
# 1. HELPER & UTILITY FUNCTIONS
# ==========================================

def latlon_to_meters(lon, lat, ref_lon, ref_lat):
    """Konversi koordinat Lat/Lon (derajat) ke Meter lokal (Mercator/Flat Projection)"""
    r = 6378137.0 # Jari-jari bumi
    x = r * math.radians(lon - ref_lon) * math.cos(math.radians(ref_lat))
    y = r * math.radians(lat - ref_lat)
    return x, y

def generate_corridor_polylines(coords, width=6.0):
    """
    Menghitung vektor offset kiri & kanan dari alur kabel 
    untuk menggambarkan badan jalan secara otomatis.
    """
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

    return left_pts, right_pts

def parse_kmz(kmz_bytes):
    """Parsing KMZ/KML dan ekstraksi data kabel & tiang + deteksi overlap"""
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
                cables.append({'name': placemark.findtext('kml:name', '', ns), 'coords': pts})

        # Ekstraksi Point (Tiang)
        point = placemark.find('.//kml:Point/kml:coordinates', ns)
        if point is not None and point.text:
            parts = point.text.strip().split(',')
            if len(parts) >= 2:
                lon, lat = float(parts[0]), float(parts[1])
                name = placemark.findtext('kml:name', 'Pole', ns)
                all_raw_coords.append((lon, lat))
                
                # Cek apakah tiang memiliki aksesoris (berdasarkan deskripsi/style/nama)
                desc = placemark.findtext('kml:description', '', ns).lower()
                has_acc = 'acc' in desc or 'accessories' in desc or 'slack' in desc or 'box' in desc
                
                poles.append({
                    'name': name,
                    'raw_coords': (lon, lat),
                    'has_accessories': has_acc
                })

    if not all_raw_coords:
        return {'cables': [], 'poles': [], 'inspector': []}

    # Hitung Center Reference untuk proyeksi meter lokal
    ref_lon = sum(pt[0] for pt in all_raw_coords) / len(all_raw_coords)
    ref_lat = sum(pt[1] for pt in all_raw_coords) / len(all_raw_coords)

    # Konversi koordinat Kabel ke Meter
    converted_cables = []
    for c in cables:
        m_pts = [latlon_to_meters(lon, lat, ref_lon, ref_lat) for lon, lat in c['coords']]
        converted_cables.append({'name': c['name'], 'coords': m_pts})

    # Konversi koordinat Tiang & Deteksi Overlap
    converted_poles = []
    inspector_logs = []
    coord_tracker = {}

    for p in poles:
        mx, my = latlon_to_meters(p['raw_coords'][0], p['raw_coords'][1], ref_lon, ref_lat)
        coord_key = (round(mx, 2), round(my, 2))

        # Peringatan Precision & Quality Inspector jika bertumpuk
        if coord_key in coord_tracker:
            inspector_logs.append({
                'level': 'WARNING',
                'category': 'Overlap Geometry',
                'detail': f"Tiang '{p['name']}' bertumpuk persis dengan '{coord_tracker[coord_key]}'"
            })
        else:
            coord_tracker[coord_key] = p['name']

        converted_poles.append({
            'name': p['name'],
            'coords': (mx, my),
            'has_accessories': p['has_accessories'],
            'is_overlap': coord_key in coord_tracker
        })

    return {
        'cables': converted_cables,
        'poles': converted_poles,
        'inspector': inspector_logs
    }

# ==========================================
# 2. DXF GENERATOR ENGINE (PERFEKSIONIS)
# ==========================================

def build_dxf_document(parsed_data, proj_info, road_width=6.0):
    doc = ezdxf.new(dxfversion='AC1027')
    doc.header['$INSUNITS'] = units.MM

    # Setup Layering & Lineweight
    layers = doc.layers
    layers.add("01_BADAN_JALAN", color=8, lineweight=13)
    layers.add("03_KABEL_FO", color=1, lineweight=50)
    layers.add("04_POLE_TIANG", color=3, lineweight=30)
    layers.add("05_SMARTBOX_SLACK", color=2, lineweight=25)
    layers.add("KOP_TITLE_BLOCK", color=7, lineweight=25)

    msp = doc.modelspace()
    all_x, all_y = [], []

    # A. Render Kabel & Koridor Jalan
    for cable in parsed_data['cables']:
        pts = cable['coords']
        if len(pts) < 2:
            continue
        for pt in pts:
            all_x.append(pt[0])
            all_y.append(pt[1])

        msp.add_lwpolyline(pts, dxfattribs={'layer': '03_KABEL_FO'})

        # Peta Jalan Otomatis
        left_b, right_b = generate_corridor_polylines(pts, width=road_width)
        if left_b:
            msp.add_lwpolyline(left_b, dxfattribs={'layer': '01_BADAN_JALAN'})
        if right_b:
            msp.add_lwpolyline(right_b, dxfattribs={'layer': '01_BADAN_JALAN'})

    # B. Render Tiang & Terapkan Aturan Bisnis Kak Donny
    for p in parsed_data['poles']:
        pos = p['coords']
        name = p['name']
        all_x.append(pos[0])
        all_y.append(pos[1])

        # Gambar Tiang (Bila overlap, tetap digambar)
        msp.add_circle(pos, radius=0.8, dxfattribs={'layer': '04_POLE_TIANG'})

        # Aturan Aksesoris -> Smartbox & "New Slack Support"
        text_y_offset = 0.8
        if p['has_accessories']:
            msp.add_rectangle4p([
                (pos[0]-1.2, pos[1]-1.2), (pos[0]+1.2, pos[1]-1.2),
                (pos[0]+1.2, pos[1]+1.2), (pos[0]-1.2, pos[1]+1.2)
            ], dxfattribs={'layer': '05_SMARTBOX_SLACK'})

            msp.add_text("New Slack Support", dxfattribs={
                'layer': '05_SMARTBOX_SLACK', 'height': 0.8
            }).set_placement((pos[0] + 1.8, pos[1] - 0.5))

        # Aturan Penamaan Closure (cls48 -> New Closure 48C / cl24 -> New Closure 24)
        lower_name = name.lower()
        if 'cls' in lower_name or 'cl' in lower_name:
            clean_name = name.replace('cls', 'New Closure ').replace('cl', 'New Closure ')
            if '48' in clean_name and not clean_name.endswith('C'):
                clean_name += 'C'
            display_name = clean_name
        else:
            display_name = name

        # Jika overlap, beri sedikit offset teks vertikal agar tidak menumpuk total
        if p.get('is_overlap'):
            text_y_offset += 1.2

        msp.add_text(display_name, dxfattribs={
            'layer': '04_POLE_TIANG', 'height': 0.9
        }).set_placement((pos[0] + 1.8, pos[1] + text_y_offset))

    # C. Dynamic Viewport A3 Layout Space
    if "Paper_A3_Presentation" in doc.layouts:
        layout = doc.layouts.get("Paper_A3_Presentation")
    else:
        layout = doc.layouts.new("Paper_A3_Presentation")

    layout.page_setup(size=(420, 297), unit="mm")

    min_x, max_x = (min(all_x), max(all_x)) if all_x else (0, 100)
    min_y, max_y = (min(all_y), max(all_y)) if all_y else (0, 100)
    cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    h = max_y - min_y or 100.0

    layout.add_viewport(
        center=(160, 148),
        size=(280.0, 200.0),
        view_center_point=(cx, cy),
        view_height=h * 1.18
    )

    # D. Isi Informasi Kop Gambar dari Sidebar
    layout.add_text(proj_info.get('span_name', ''), dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 3.5}).set_placement((315, 45))
    layout.add_text(proj_info.get('project_code', ''), dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 3.5}).set_placement((315, 35))
    layout.add_text(proj_info.get('drawn_by', 'RPS'), dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 3.0}).set_placement((345, 18))
    layout.add_text(proj_info.get('checked_by', 'IFORTE'), dxfattribs={'layer': 'KOP_TITLE_BLOCK', 'height': 3.0}).set_placement((345, 12))

    out_bytes = io.StringIO()
    doc.write(out_bytes)
    return out_bytes.getvalue()

# ==========================================
# 3. STREAMLIT UI INTERFACE
# ==========================================

# Sidebar Forms
st.sidebar.title("📇 Informasi Proyek (Kop Gambar)")
span_name = st.sidebar.text_input("SPAN NAME", "14PBG007_REMBANGPBLG - 14PBG03...")
name_project = st.sidebar.text_input("NAME OF PROJECT", "AS BUILD")
project_code = st.sidebar.text_input("PROJECT CODE / REVISION CODE", "RM-26-000327")

st.sidebar.title("✍️ Personel & Otorisasi")
drawn_by = st.sidebar.text_input("DRAWN BY (Initial)", "RPS")
checked_by = st.sidebar.text_input("CHECKED BY (Initial)", "IFORTE")
approved_by = st.sidebar.text_input("APPROVED BY (Initial)", "IFORTE")

st.sidebar.title("⚙️ Parameter Gambar")
revision = st.sidebar.text_input("REVISION", "0")

# Main Content
st.title("ASPLAN PRO v12.1 - KMZ to DXF Converter")

uploaded_file = st.file_uploader("Upload File KMZ Proyek", type=['kmz'])

if uploaded_file:
    parsed = parse_kmz(uploaded_file.read())
    
    st.subheader(f"📦 File: {uploaded_file.name}")
    st.write(f"Ditemukan {len(parsed['cables'])} segmen kabel dan {len(parsed['poles'])} tiang.")

    col1, col2 = st.columns([1, 1])

    with col1:
        st.markdown("### 📐 Viewport Preview Box")
        fig, ax = plt.subplots(figsize=(6, 5))
        ax.set_facecolor('#0e1117')
        fig.patch.set_facecolor('#0e1117')

        # Preview Plot Kabel
        for c in parsed['cables']:
            xs = [pt[0] for pt in c['coords']]
            ys = [pt[1] for pt in c['coords']]
            ax.plot(xs, ys, color='#ff4b4b', linewidth=2)

        # Preview Plot Tiang
        pxs = [p['coords'][0] for p in parsed['poles']]
        pys = [p['coords'][1] for p in parsed['poles']]
        ax.scatter(pxs, pys, color='#00ff7f', s=15)

        ax.tick_params(colors='white')
        ax.grid(True, color='#333333', linestyle='--')
        st.pyplot(fig)

    with col2:
        st.markdown("### 🔍 Precision & Quality Inspector")
        if parsed['inspector']:
            st.warning(f"Ditemukan {len(parsed['inspector'])} potensi kesalahan presisi:")
            st.table(parsed['inspector'])
        else:
            st.success("Semua geometri presisi! Tidak ditemukan tumpukan tiang.")

    # Generate DXF Button
    proj_data = {
        'span_name': span_name,
        'project_code': project_code,
        'drawn_by': drawn_by,
        'checked_by': checked_by
    }
    
    dxf_string = build_dxf_document(parsed, proj_data)

    st.download_button(
        label=f"💾 Download DXF (dengan Layout) - {uploaded_file.name}.dxf",
        data=dxf_string,
        file_name=f"{uploaded_file.name}.dxf",
        mime="application/dxf"
    )

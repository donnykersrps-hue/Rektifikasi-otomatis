import pandas as pd
import math

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

def add_road_label(msp, line_coords, road_name, layer_name):
    """Filter NaN dan Rotasi Teks Sejajar Jalan"""
    # 1. Filter NaN / None / Kosong
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
    
    # 2. Perhitungan Sudut Rotasi Teks Sejajar Garis Jalan
    angle_rad = math.atan2(dy, dx)
    angle_deg = math.degrees(angle_rad)
    
    # Normalisasi agar teks selalu terbaca dari kiri ke kanan / bawah ke atas
    while angle_deg > 90:
        angle_deg -= 180
    while angle_deg < -90:
        angle_deg += 180
    
    font_height = 2.5 if length > 150 else 1.8
    
    # Offset tegak lurus dari garis jalan
    offset_dist = font_height * 1.5
    offset_x = -offset_dist * math.sin(angle_rad)
    offset_y = offset_dist * math.cos(angle_rad)
    
    text_x = mid_point.x + offset_x
    text_y = mid_point.y + offset_y
    
    msp.add_text(
        road_str,
        dxfattribs={
            'layer': layer_name,
            'height': font_height,
            'rotation': angle_deg,
            'style': 'ARIAL_STD',
            'color': 7
        }
    ).set_placement((text_x, text_y))

from flask import Flask, render_template, request, send_file, jsonify, make_response, session, redirect, url_for, flash  # pyre-ignore
import json
import random
import requests  # pyre-ignore
import pandas as pd  # pyre-ignore
import os
import math
import re
import io   
from datetime import datetime
from datetime import timedelta
from io import BytesIO
import time
import threading
from collections import deque
import sys
import traceback
import csv
from functools import wraps
from werkzeug.security import generate_password_hash, check_password_hash
import secrets
from typing import Optional, List, Dict, Any, Union
try:
    from .sheets_handler import sheets_handler  # type: ignore
except (ImportError, ValueError):
    from sheets_handler import sheets_handler  # type: ignore
# Global Caching for Polling Efficiency - VERCEL OPTIMIZED
_last_fetch_time = 0
_cached_metar = None
CACHE_TTL = 20  # 20 detik untuk polling data (mempercepat real-time update)

# Tambahkan cache untuk history API
_cached_history = None
_history_cache_time = 0
HISTORY_CACHE_TTL = 600  # 10 menit untuk history

# Track last successful collection per station
_last_collection = {
    'WARR': {'time': None, 'metar': None, 'attempts': 0}
}

def format_indonesian_date(dt):
    """Format datetime ke format Indonesia: Kamis, 02 April 2026"""
    days = ["Senin", "Selasa", "Rabu", "Kamis", "Jumat", "Sabtu", "Minggu"]
    months = ["Januari", "Februari", "Maret", "April", "Mei", "Juni", 
              "Juli", "Agustus", "September", "Oktober", "November", "Desember"]
    
    day_name = days[dt.weekday()]
    month_name = months[dt.month - 1]   
    
    return f"{day_name}, {dt.day:02d} {month_name} {dt.year}"

def bin_wind_data(records):
    """
    Bin raw wind records into 16 sectors and 7 speed categories.
    Berdasarkan standar BMKG/WRPlot.
    Sectors: N, NE, E, SE, S, SW, W, NW (8 Sectors)
    Bins: 0-5, 5-10, 10-15, 15-20, 20-25, 25-30, >30 (knots)
    """
    sectors_list = [
        "N", "NE", "E", "SE", "S", "SW", "W", "NW"
    ]
    # Speed bins (upper limits)
    bins = [5, 10, 15, 20, 25, 30, float('inf')]
    bin_labels = ["0-5", "5-10", "10-15", "15-20", "20-25", "25-30", ">30"]
    
    # Initialize counts: matrix[sector_idx][bin_idx]
    matrix = [[0 for _ in range(len(bins))] for _ in range(len(sectors_list))]
    # Track times per sector/bin for hover
    time_matrix = [[[] for _ in range(len(bins))] for _ in range(len(sectors_list))]
    calm_count = 0
    total_valid = 0
    
    for r in records:
        try:
            speed_val = r.get("speed")
            speed = float(speed_val) if speed_val is not None else 0
            direction_raw = r.get("dir")
            
            # Count towards total for percentage base
            total_valid += 1
            
            # Calm check: 0 KT or VRB
            if speed <= 0 or direction_raw == "VRB" or direction_raw is None:
                calm_count += 1
                continue
                
            direction = float(direction_raw)
            # Map direction (0-360) to sector index (0-7)
            # North (0) is centered at -22.5 to 22.5.
            idx = int(((direction + 22.5) % 360) / 45)
            
            # Map speed to bin index
            bin_idx = 0
            for i, limit in enumerate(bins):
                if speed <= limit:
                    bin_idx = i
                    break
            
            if idx < len(sectors_list):
                matrix[idx][bin_idx] += 1
                # Store time info for hover
                utc_t = r.get("utc_time", "")
                wib_t = r.get("wib_time", "")
                if utc_t:
                    time_matrix[idx][bin_idx].append({"utc": utc_t, "wib": wib_t})
        except:
            continue
            
    # Calculate results
    binned_results = []
    calm_percent = (calm_count / total_valid * 100) if total_valid > 0 else 0
    
    for i, sector in enumerate(sectors_list):
        sector_bins = []
        for j, label in enumerate(bin_labels):
            count = matrix[i][j]
            percent = (count / total_valid * 100) if total_valid > 0 else 0
            # Build compact time summary for hover (each record on its own line)
            times = time_matrix[i][j]
            time_summary = ""
            if times:
                # Pair UTC only, one per line
                lines = []
                seen = set()
                for t in sorted(times, key=lambda x: x["utc"]):
                    utc_val = t["utc"]
                    if utc_val not in seen:
                        seen.add(utc_val)
                        lines.append(utc_val)
                time_summary = "<br>".join(lines)
            sector_bins.append({
                "label": label,
                "count": count,
                "percentage": round(percent, 2),
                "times": time_summary
            })
        binned_results.append({
            "sector": sector,
            "angle": i * 45,
            "bins": sector_bins
        })
        
    return {
        "sectors": binned_results,
        "calm_percent": round(calm_percent, 2),
        "total_count": total_valid,
        "bin_labels": bin_labels
    }

# Resolve absolute paths for Vercel
# Vercel structured as /var/task/api/index.py
current_dir = os.path.dirname(os.path.abspath(__file__))
project_root = os.path.dirname(current_dir)
template_dir = os.path.join(project_root, "templates")
static_dir = os.path.join(project_root, "static")

app = Flask(__name__, 
            template_folder=template_dir, 
            static_folder=static_dir)
application = app # Alias for compatibility

# ============ ADMIN AUTH CONFIGURATION ============
# Default fallback password hash adalah representasi aman dari "juanda$2026"
DEFAULT_HASH = "scrypt:32768:8:1$BtddDVF5DqG1GpWk$6aa36ddf49dd394f7e37ced0ff5bc61eab47fa4054b2397497b781d2d6e571992f4ee3c58b456b9548431e272e142e78a2199f99ca1ab3a87bdba8dce17b840b"

ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD_HASH = os.environ.get("ADMIN_PASSWORD_HASH", DEFAULT_HASH)

# GUEST AUTH CONFIGURATION (Public access)
GUEST_USERNAME = "guest"
GUEST_PASSWORD_HASH = "scrypt:32768:8:1$h5GjkYvtTBvYwzoZ$84a0e7593507070235793c04e16977f83400e7b9038c399a24d704cd49b5241be10bd1a6c5a8f681d5a2d76691f60c680b121e8a838cd0a6286425b856a5bfff"


# Session secret tetap agar session tidak expired saat cold start
app.secret_key = os.environ.get("SECRET_KEY", "46f05c593c66cab7e02c330ec1671298d03bedfdb08350bf485aed4a8b0d2b82")

# Session timeout (30 menit idle)
SESSION_TIMEOUT = 30 * 60

# Session cookie configuration (production-grade)
app.config.update(
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=30),
    SESSION_COOKIE_NAME='metar_session',
    SESSION_COOKIE_HTTPONLY=True,       # Tidak bisa diakses JavaScript
    SESSION_COOKIE_SAMESITE='Lax',      # CSRF protection
    SESSION_COOKIE_PATH='/',            # Berlaku untuk seluruh domain
)

# Hanya set SECURE cookie di production (HTTPS)
if os.environ.get("VERCEL") or os.environ.get("VERCEL_ENV"):
    app.config['SESSION_COOKIE_SECURE'] = True

def login_required(f):
    """Decorator proteksi untuk akses web/UI — redirect ke /login"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'admin_logged_in' not in session:
            if request.is_json or request.path.startswith('/api/'):
                return jsonify({"error": "Unauthorized", "login_required": True}), 401
            return redirect(url_for('login_page'))
        
        last_activity = session.get('last_activity', 0)
        if time.time() - last_activity > SESSION_TIMEOUT:
            session.clear()
            flash('Sesi berakhir karena tidak aktif selama 30 menit. Silakan login kembali.', 'warning')
            return redirect(url_for('login_page'))
        
        session['last_activity'] = time.time()
        return f(*args, **kwargs)
    return decorated_function

def admin_only_api(f):
    """Decorator proteksi khusus untuk endpoint API — return 401 JSON"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        # Allow Vercel/External Cron to bypass session checks via header/token
        is_vercel_cron = request.headers.get('x-vercel-cron') == '1'
        valid_cron_token = request.args.get('auth') == os.environ.get('CRON_TOKEN', 'bmkg-juanda-secret-123')
        
        if is_vercel_cron or valid_cron_token:
            return f(*args, **kwargs)

        if 'admin_logged_in' not in session:
            return jsonify({"error": "Unauthorized", "login_required": True}), 401
        
        last_activity = session.get('last_activity', 0)
        if time.time() - last_activity > SESSION_TIMEOUT:
            session.clear()
            return jsonify({"error": "Session expired", "login_required": True}), 401
            
        session['last_activity'] = time.time()
        return f(*args, **kwargs)
    return decorated_function

# ============ KONFIGURASI UNTUK VERCEL ============
# Vercel Environment Detection
IS_VERCEL = os.environ.get("VERCEL") == "true" or os.environ.get("VERCEL_ENV") is not None or os.path.exists("/var/task")

# Gunakan /tmp untuk writeable storage di Vercel
# Pada Vercel, hanya /tmp yang bisa ditulisi (writable)
ROOT_CSV = os.path.join(project_root, "metar_history.csv")

if IS_VERCEL:
    CSV_FILE = "/tmp/metar_history.csv"
    print("[INIT] Running on VERCEL detected - Using /tmp/ storage", file=sys.stderr)
    
    # 🔥 HYBRID HISTORY STRATEGY:
    # Jika /tmp/metar_history.csv belum ada, copykan dari root folder (Git)
    # ATAU sync dari Google Sheets untuk data terbaru
    if not os.path.exists(CSV_FILE):
        sync_success = False
        if IS_VERCEL:
            print("[INIT] Attempting sync from Google Sheets...", file=sys.stderr)
            sync_success = sheets_handler.sync_to_local(CSV_FILE)
        
        if not sync_success and os.path.exists(ROOT_CSV):
            try:
                import shutil
                shutil.copy2(ROOT_CSV, CSV_FILE)
                print("[INIT] Base history copied from project root to /tmp/", file=sys.stderr)
            except Exception as e:
                print(f"[INIT] Failed to copy base history: {e}", file=sys.stderr)
else:
    CSV_FILE = ROOT_CSV
    print("[INIT] Running locally - Using local storage", file=sys.stderr)

# Global in-memory deduplication for wind logs (prevents flooding during sync)
WIND_LOGGED_REGISTRY = set()

WIND_LOG_FILE = os.path.join(project_root if not IS_VERCEL else "/tmp", "wind_calculations.csv")

def init_wind_log():
    """Initialize CSV file untuk wind calculations"""
    if not os.path.exists(WIND_LOG_FILE):
        try:
            with open(WIND_LOG_FILE, 'w', newline='') as f:
                writer = csv.writer(f)
                writer.writerow([
                    'timestamp', 'metar_raw', 'station', 'runway', 'runway_heading',
                    'wind_dir', 'wind_speed', 'wind_gust',
                    'headwind', 'crosswind', 'tailwind',
                    'crosswind_status', 'tailwind_status'
                ])
        except Exception as e:
            print(f"[INIT] Failed to init wind log: {e}", file=sys.stderr)

def save_wind_calculation(data):
    """Simpan wind calculation - Prioritaskan Google Sheets, fallback ke CSV"""
    # 1. Coba simpan ke Google Sheets dulu (persistent di Vercel)
    try:
        success = sheets_handler.save_wind_calculation(data)
        if success:
            return True
    except Exception as e:
        print(f"[WIND SAVE] Sheets failed: {e}", file=sys.stderr)
    
    # 2. Fallback ke CSV (untuk local dev atau jika Sheets error)
    try:
        init_wind_log()
        with open(WIND_LOG_FILE, 'a', newline='') as f:
            writer = csv.writer(f)
            writer.writerow([
                data.get('timestamp', datetime.utcnow().isoformat()),
                data.get('metar_raw', ''),
                data.get('station', 'WARR'),
                data.get('runway', 'Unknown'),
                data.get('runway_heading', ''),
                data.get('wind_dir', ''),
                data.get('wind_speed', ''),
                data.get('wind_gust', ''),
                data.get('headwind', ''),
                data.get('crosswind', ''),
                data.get('tailwind', ''),
                data.get('crosswind_status', ''),
                data.get('tailwind_status', '')
            ])
        return True
    except Exception as e:
        print(f"[WIND LOG] Error saving: {e}", file=sys.stderr)
        return False

def process_server_wind_log(metar_raw):
    """
    Menghitung dan menyimpan log angin secara otomatis di server.
    Menduplikasi logika trigonometri dari dashboard.js ke Python.
    """
    try:
        if not metar_raw:
            return False
            
        # --- PERSISTENT DEDUPLICATION ---
        # 1. Cek di memori (Fast cache - Sesi ini)
        metar_key = normalize_metar(metar_raw)
        if metar_key in WIND_LOGGED_REGISTRY:
            return True
            
        # 2. Cek di Google Sheets (Persisten - Antar Device/Restart)
        try:
            if sheets_handler.check_if_metar_logged(metar_raw):
                WIND_LOGGED_REGISTRY.add(metar_key)
                print(f"[SERVER WIND] Duplicate detected in Sheets: {metar_raw[:20]}... Skipping.", file=sys.stderr)
                return True
        except Exception as e:
            print(f"[SERVER WIND] Persistence check failed: {e}", file=sys.stderr)

        # 3. Cek di CSV Lokal (Jika sedang dev lokal)
        if not IS_VERCEL and os.path.exists(WIND_LOG_FILE):
            try:
                df_tail = pd.read_csv(WIND_LOG_FILE).tail(40)
                if not df_tail.empty and metar_raw in df_tail['metar_raw'].values:
                    WIND_LOGGED_REGISTRY.add(metar_key)
                    print(f"[SERVER WIND] Duplicate detected in LOCAL CSV: {metar_raw[:20]}...", file=sys.stderr)
                    return True
            except Exception as e:
                pass
            
        # Extract wind from METAR: 3 digits dir, 2-3 digits speed, optional G gust
        wind_match = re.search(r'\b(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT\b', metar_raw)
        if not wind_match:
            return False
            
        wind_dir_raw = wind_match.group(1)
        if wind_dir_raw == 'VRB':
            return False # Tidak bisa hitung presisi untuk VRB
            
        wind_dir = int(wind_dir_raw)
        wind_speed = int(wind_match.group(2))
        wind_gust = int(wind_match.group(3)[1:]) if wind_match.group(3) else None
        
        station = "WARR" # Default for Juanda
        if "WARR" not in metar_raw and "WARS" in metar_raw: station = "WARS"
        
        runways = [
            {"name": "10", "hdg": 100},
            {"name": "28", "hdg": 280}
        ]
        
        timestamp = datetime.utcnow().isoformat()
        
        for rwy in runways:
            # Trigonometri
            angle_deg = wind_dir - rwy["hdg"]
            angle_rad = math.radians(angle_deg)
            
            raw_headwind = wind_speed * math.cos(angle_rad)
            raw_crosswind = wind_speed * math.sin(angle_rad)
            
            # Rounding precision 1 decimal (e.g. 8.45 -> 8.5)
            # Dibuat eksplisit untuk menghindari kesalahan scaling
            headwind = round(float(raw_headwind), 1)
            crosswind = round(float(abs(raw_crosswind)), 1)
            
            head_val = max(0.0, headwind)
            tailwind = abs(headwind) if headwind < 0 else 0.0
            
            # Status
            cross_status = 'DANGER' if crosswind >= 20 else ('CAUTION' if crosswind >= 10 else 'SAFE')
            tail_status = 'DANGER' if tailwind >= 10 else ('CAUTION' if tailwind >= 5 else 'SAFE')
            
            payload = {
                'timestamp': timestamp,
                'metar_raw': metar_raw,
                'station': station,
                'runway': rwy["name"],
                'runway_heading': rwy["hdg"],
                'wind_dir': wind_dir,
                'wind_speed': wind_speed,
                'wind_gust': wind_gust,
                'headwind': head_val,
                'crosswind': crosswind,
                'tailwind': tailwind,
                'crosswind_status': cross_status,
                'tailwind_status': tail_status
            }
            
            # Anti-duplikasi di log_crosswind sudah handle global state
            # Namun kita panggil save_wind_calculation langsung di sini untuk server-side
            save_wind_calculation(payload)
            
        # Mark as processed in this session
        WIND_LOGGED_REGISTRY.add(metar_key)
        
        # Keep registry size manageable
        if len(WIND_LOGGED_REGISTRY) > 200:
            # Remove oldest (roughly)
            WIND_LOGGED_REGISTRY.clear() 
            
        # DEBUG LOGGING UNTUK INVESTIGASI
        print(f"[SERVER WIND] Calculated: Spd={wind_speed}, Dir={wind_dir}, Angle={angle_deg}deg", file=sys.stderr)
        print(f"[SERVER WIND] Results: Lat={raw_headwind:.1f}, Cross={raw_crosswind:.1f}", file=sys.stderr)
        print(f"[SERVER WIND] [NEW] SAVED Wind Forensics for: {metar_raw[:40]}...", file=sys.stderr)
        return True
    except Exception as e:
        print(f"[SERVER LOG] Failed: {e}", file=sys.stderr)
        return False

# =========================
# FAVICON HANDLER
# =========================
@app.route('/favicon.ico')
def favicon_ico():
    """Handle favicon.ico requests"""
    return '', 204  # No content

@app.route('/favicon.png')
def favicon_png():
    """Handle favicon.png requests"""
    return '', 204  # No content

# =========================
# ERROR HANDLERS
# =========================
@app.errorhandler(404)
def not_found_error(error):
    """Handle 404 errors gracefully"""
    # Log to stderr but don't crash
    print(f"[404] Not Found: {request.path}", file=sys.stderr)
    
    # If request looks like favicon, return 204 without body
    if 'favicon' in request.path.lower():
        return '', 204
    
    # For API requests, return JSON
    if request.path.startswith('/api/'):
        return jsonify({"error": "Not found", "path": request.path}), 404
    
    # For web requests, return simple message
    return "Page not found", 404

@app.errorhandler(Exception)
def handle_exception(e):
    """Global error handler to catch all exceptions"""
    error_msg = f"ERROR: {str(e)}"
    print(error_msg, file=sys.stderr)
    
    # Detailed log for server/stderr
    import traceback
    traceback.print_exc()

    # Generic error for client
    return jsonify({
        "error": str(e)
    }), 500

# System Control State
auto_fetch = True
last_metar_update = None

# Cache for latest METAR data (used by polling endpoint)
latest_metar_data = {}

# ============ HELPER FUNCTIONS ============

def extract_temp(metar):
    """Extract temperature from METAR (XX/XX)"""
    if not metar: return None
    match = re.search(r'(\d{2})/(\d{2})', str(metar))
    return int(match.group(1)) if match else None

def extract_pressure(metar):
    """Extract QNH pressure from METAR (QXXXX)"""
    if not metar: return None
    match = re.search(r'Q(\d{4})', str(metar))
    return int(match.group(1)) if match else None

# ==========================================

# ==========================================

# Wind history storage for Wind Rose
wind_history = deque(maxlen=500)

# Store wind data for Wind Rose
def store_wind(parsed, station="WARR"):
    # Skip if wind direction is "VRB" (variable) or missing
    wind_dir = parsed.get("wind_dir")
    if not wind_dir or wind_dir == "VRB" or not parsed.get("wind_speed_kt"):
        return
    
    try:
        wind_history.append({
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "station": station,
            "dir": int(wind_dir),
            "speed": float(parsed["wind_speed_kt"])
        })
        # print(f"[WIND] Stored: dir={wind_dir}, speed={parsed['wind_speed_kt']}kt")
    except (ValueError, TypeError) as e:
        pass

def load_wind_history():
    """Load historical wind data from CSV into memory for Wind Rose"""
    if not os.path.exists(CSV_FILE):
        return
    try:
        df = pd.read_csv(CSV_FILE)
        if df.empty:
            return
        
        # Take last 500 rows for the Wind Rose
        df = df.tail(500)
        
        count = 0
        for _, row in df.iterrows():
            metar = str(row["metar"]) if pd.notna(row["metar"]) else ""
            if not metar:
                continue
            
            # Use regex to quickly extract wind dir and speed
            wind_match = re.search(r'\b(\d{3})(\d{2,3})(G\d{2,3})?KT\b', metar)
            if wind_match:
                try:
                    wind_history.append({
                        "time": str(row["time"]),
                        "station": row["station"],
                        "dir": int(wind_match.group(1)),
                        "speed": float(wind_match.group(2))
                    })
                    count += 1
                except (ValueError, TypeError):
                    continue
        print(f"[SUCCESS] Loaded {count} wind records from {CSV_FILE} for Wind Rose")
    except Exception as e:
        print(f"[ERROR] Failed to load wind history: {e}")




import math

def calculate_crosswind(wind_dir, wind_speed, runway_heading):
    angle = abs(wind_dir - runway_heading)
    angle_rad = math.radians(angle)
    return round(wind_speed * math.sin(angle_rad), 1)


#Deteksi thunderstorm dari raw METAR
def detect_thunderstorm(raw_metar: str) -> bool:
    if not raw_metar: return False
    ts_codes = ["TS", "TSRA", "VCTS", "+TS", "TSGR", "-TS", "TSRA", "+TSRA", "-TSRA"]
    return any(code in raw_metar for code in ts_codes)


# =========================
# GET METAR FROM NOAA
# =========================
def get_metar(station_code):
    """
    Fetch METAR with Cache Busting and Smart Source Switching.
    Prioritizes NOAA but falls back to AVWX if NOAA is stale or fails.
    """
    station_code = station_code.upper()
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
        'Cache-Control': 'no-cache, no-store, must-revalidate',
        'Pragma': 'no-cache',
        'Expires': '0'
    }
    
    def get_time_key(metar_str):
        if not metar_str: return None
        m = re.search(r'\b(\d{6}Z)\b', metar_str)
        return m.group(1) if m else None

    # 1. Try NOAA with Cache Busting
    # Adding timestamp query param to defeat Edge/CDN caching
    cache_buster = int(time.time())
    url = f"https://tgftp.nws.noaa.gov/data/observations/metar/stations/{station_code}.TXT?t={cache_buster}"
    
    noaa_metar = None
    try:
        print(f"[DEBUG] Fetching NOAA METAR (Buster: {cache_buster})", file=sys.stderr)
        response = requests.get(url, timeout=10, headers=headers)
        if response.status_code == 200:
            lines = response.text.strip().split("\n")
            # Usually the METAR is on the last line matching station
            for line in reversed(lines):
                line = line.strip()
                if line.startswith(station_code):
                    noaa_metar = line
                    break
    except Exception as e:
        print(f"[DEBUG] NOAA Fetch Error: {e}", file=sys.stderr)

    # 2. Check Freshness of NOAA data
    # If report is older than ~45 minutes, check alternative source
    is_stale = True
    if noaa_metar:
        time_key = get_time_key(noaa_metar)
        if time_key:
            try:
                # Basic check: is the report from the current hour or last 15-45 mins?
                now_utc = datetime.utcnow()
                report_min = int(time_key[4:6])
                report_hour = int(time_key[2:4])
                # If report hour matches current or previous hour, we consider it "potentially fresh"
                # but if we are at minute 10 of a new hour and have a :30 report, it might be stale.
                if report_hour == now_utc.hour or (now_utc.minute < 10 and report_hour == (now_utc.hour - 1) % 24):
                    is_stale = False
            except: pass

    # 3. Try AVWX if NOAA failed or is stale
    if not noaa_metar or is_stale:
        print(f"[DEBUG] NOAA is {'stale' if noaa_metar else 'unavailable'}. Trying AVWX...", file=sys.stderr)
        alt_url = f"https://avwx.rest/api/metar/{station_code}?t={cache_buster}"
        try:
            resp = requests.get(alt_url, timeout=10, headers=headers)
            if resp.status_code == 200:
                alt_data = resp.json()
                alt_metar = alt_data.get("raw")
                
                if not noaa_metar: return alt_metar
                
                # Compare timestamps if we have both
                noaa_key = get_time_key(noaa_metar)
                alt_key = get_time_key(alt_metar)
                
                if alt_key and noaa_key:
                    # Very simple chronological check (works within same day)
                    if int(alt_key[:6]) > int(noaa_key[:6]):
                        print(f"[DEBUG] AVWX has newer data ({alt_key}) than NOAA ({noaa_key})", file=sys.stderr)
                        return alt_metar
        except Exception as e:
            print(f"[DEBUG] AVWX Fallback Error: {e}", file=sys.stderr)

    return noaa_metar

# =========================
# WEATHER CODES
# =========================
WEATHER_CODES = [
    "DZ", "-RA", "RA","SN","SG","IC","PL","GR","GS",
    "UP","BR","FG","FU","VA","DU","SA","HZ",
    "PO","SQ","FC","SS","DS","TS","SH", "TSRA",
    "+TSRA", "-TSRA", "-TS", "+TS", "VCTS"
]

# =========================
# DETECT METAR SPECIAL REPORT TYPE
# =========================
def detect_metar_report_type(metar: str) -> str:
    """
    Detect if METAR is a special report (COR, CCA, AMD, SPECI)
    Returns: 'COR', 'AMD', 'SPECI', or 'METAR'
    """
    if not metar:
        return "METAR"
    
    metar = metar.upper()

    if " SPECI " in metar or metar.startswith("SPECI "):
        return "SPECI"

    if " AMD " in metar or metar.startswith("METAR AMD"):
        return "AMD"

    if " COR " in metar or metar.startswith("METAR COR") or " CCA " in metar or "CCA" in metar:
        return "COR"

    return "METAR"

# =========================
# PARSE METAR
# =========================
def parse_metar(metar: str) -> dict:

    data: dict = {
        "station": None,
        "day": None,
        "hour": None,
        "minute": None,
        "wind_dir": None,
        "wind_speed_kt": None,
        "wind_gust_kt": None,
        "visibility_m": None,
        "weather": None,
        "cloud": None,
        "temperature_c": None,
        "dewpoint_c": None,
        "pressure_hpa": None,
        "trend": None,
        "tempo": None,  # Add tempo field
        "report_type": "METAR"   # 🔥 NEW: Tracks COR, AMD, SPECI, METAR
    }

    # Detect special report type
    data["report_type"] = detect_metar_report_type(metar)

    clean_metar = metar.replace("=", "")
    parts = clean_metar.split()

    # First, extract TEMPO clause before the main parsing
    # This removes TEMPO from METAR so weather isn't captured from TEMPO section
    tempo_match = re.search(r'TEMPO\s+(.+)', metar)
    if tempo_match:
        tempo_content = tempo_match.group(1).strip()
        # Store the full TEMPO content
        data["tempo"] = tempo_content
        # Remove TEMPO clause from METAR for parsing (to avoid capturing weather from TEMPO)
        main_metar = re.sub(r'\s+TEMPO\s+.+', '', metar)
    else:
        main_metar = metar
    
    # Parse the main METAR (without TEMPO) for weather and other fields
    parts: list[str] = main_metar.replace("=", "").split()

    for part in parts:

        if len(part) == 4 and part.isalpha() and data["station"] is None:
            data["station"] = part

        if part.endswith("Z") and len(part) == 7:
            data["day"] = part[0] + part[1]
            data["hour"] = part[2] + part[3]
            data["minute"] = part[4] + part[5]

        # WIND PARSER (robust aviation parser)
        if part.endswith("KT"):

            wind_match = re.match(r"^(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT$", part)

            if wind_match:
                data["wind_dir"] = wind_match.group(1)
                data["wind_speed_kt"] = wind_match.group(2)

                if wind_match.group(4):
                    data["wind_gust_kt"] = wind_match.group(4)
                else:
                    data["wind_gust_kt"] = None

        if part.isdigit() and len(part) == 4:
            data["visibility_m"] = int(part)

        if part in ["HZ","BR","FG","DZ","SN","SG","IC","PL","GR","GS","UP","RA","+RA","-RA","TSRA","+TSRA","TS","+TS","-TS","VCTS","SH","DS","SS","-TSRA"]:
            # Only set weather if not already set (get first weather occurrence)
            if data["weather"] is None:
                data["weather"] = part

        if part.startswith(("FEW","SCT","BKN","OVC")):
            data["cloud"] = part

        if "/" in part and len(part) == 5:
            t, d = part.split("/")
            data["temperature_c"] = t
            data["dewpoint_c"] = d

        if part.startswith("Q"):
            qnh_match = re.match(r"Q(\d{4})", part)
            if qnh_match:
                data["pressure_hpa"] = qnh_match.group(1)

        if part == "NOSIG":
            data["trend"] = part

    # If there's TEMPO data, set trend to include it
    if data["tempo"]:
        data["trend"] = "TEMPO " + data["tempo"]

    # =========================
    # STATUS COLOR LOGIC
    # =========================
    status = "normal"  # default green
    
    # Check for danger conditions
    if detect_thunderstorm(metar):
        status = "danger"  # red - thunderstorm
    elif data["visibility_m"] is not None and data["visibility_m"] < 3000:
        status = "danger"  # red - low visibility < 3000m
    elif data["weather"] and data["weather"] != "NIL":
        # Check for warning conditions
        warning_weather = ["RA", "FG", "HZ", "BR", "SH", "DS", "SS", "FC"]
        if any(code in data["weather"] for code in warning_weather):
            status = "warning"  # yellow/orange - moderate conditions
        elif "+" in data["weather"] or "TS" in data["weather"]:
            status = "danger"  # red - severe weather
    
    # Check visibility for warning (3-5km)
    if data["visibility_m"] is not None and status != "danger":
        vis_val = data["visibility_m"]
        if 3000 <= vis_val <= 5000:
            status = "warning"  # yellow - moderate visibility
    
    data["status"] = status

    return data

# =========================
# HELPER: Format visibility value
# =========================
def format_visibility(vis_m):
    """Convert visibility in meters to display format"""
    if vis_m is None:
        return "NIL"
    
    # Specific visibility values
    if vis_m >= 10000 or vis_m == 9999:
        return "10 KM"
    elif vis_m == 8000:
        return "8 KM"
    elif vis_m == 7000:
        return "7 KM"
    elif vis_m == 6000:
        return "6 KM"
    elif vis_m == 5000:
        return "5 KM"
    elif vis_m == 4000:
        return "4 KM"
    elif vis_m == 3000:
        return "3 KM"
    elif vis_m == 2000:
        return "2 KM"
    elif vis_m == 1500:
        return "1.5 KM"
    elif vis_m == 1000:
        return "1 KM"
    elif vis_m >= 1000:
        return f"{vis_m // 1000} KM"
    else:
        return f"{vis_m} M"

# =========================
# HELPER: Convert parsed data to display format
# =========================
def format_parsed_for_display(parsed):
    """Convert parsed METAR data to display format for QAM and narrative"""
    display = {}
    
    # Station
    display["station"] = parsed.get("station") or "-"
    
    # Wind - format: 000°/00KT or 000°/00G00KT (with gust)
    if parsed.get("wind_dir") and parsed.get("wind_speed_kt"):
        if parsed.get("wind_gust_kt"):
            display["wind"] = f"{parsed['wind_dir']}°/{parsed['wind_speed_kt']}G{parsed['wind_gust_kt']}KT"
        else:
            display["wind"] = f"{parsed['wind_dir']}°/{parsed['wind_speed_kt']}KT"
    else:
        display["wind"] = "NIL"
    
    # Visibility - format: 10 KM or 5000 M
    display["visibility"] = format_visibility(parsed.get("visibility_m"))
    
    # Weather
    display["weather"] = parsed.get("weather") or "NIL"
    
    # Cloud - format: FEW010FT, BKN025FT CB, etc.
    if parsed.get("cloud"):
        cloud = str(parsed["cloud"])
        try:
            # cloud format expected: "BKN025" or "FEW015CB"
            amount = cloud[:3]
            height = int(cloud[3:6]) * 100
            
            # Rearrange according to requirement: [AMOUNT] [TYPE] [HEIGHT]FT
            type_part = ""
            if "CB" in cloud:
                type_part = " CB"
            elif "TCU" in cloud:
                type_part = " TCU"
            
            display["cloud"] = f"{amount}{type_part} {height}FT"
        except:
            display["cloud"] = cloud
    else:
        display["cloud"] = "NIL"
    
    # Temperature/Dewpoint - format: 28/24
    if parsed.get("temperature_c") and parsed.get("dewpoint_c"):
        display["temp_td"] = f"{parsed['temperature_c']}/{parsed['dewpoint_c']}"
    else:
        display["temp_td"] = "NIL"
    
    # Pressure QNH/QFE
    display["qnh"] = parsed.get("pressure_hpa") or "NIL"
    display["qfe"] = parsed.get("pressure_hpa") or "NIL"
    
    # Trend
    display["trend"] = parsed.get("trend") or "NIL"
    
    # Time info
    display["day"] = parsed.get("day") or "-"
    display["hour"] = parsed.get("hour") or "-"
    display["minute"] = parsed.get("minute") or "-"
    
    return display

# =========================
# EXTRACT SUPPLEMENTARY INFORMATION
# =========================
def extract_supplementary_info(metar: str) -> str:
    """
    Extract supplementary information indicators (Recent Weather) from METAR.
    Returns: Joined string of codes (e.g., "RERA, RETS") or "NIL"
    """
    if not metar:
        return "NIL"
        
    # List of common supplementary "RE" indicators
    supp_codes = [
        "RERA", "RETS", "RETSRA", "RESN", "REGR", "REDZ", "RESH", "REVC", 
        "REPL", "REGS", "REUP", "REBR", "REFG", "RESA", "REDU", "REHZ", "REPY"
    ]
    
    found_indicators = []
    metar_upper = metar.upper()
    
    # Check for each indicator in the METAR string
    for code in supp_codes:
        # Match exact word to avoid partial matches
        pattern = r'(?:^|\s)' + re.escape(code) + r'(?:\s|$|=)'
        if re.search(pattern, metar_upper):
            found_indicators.append(code)
    
    # Return formatted string or NIL
    if found_indicators:
        return ", ".join(found_indicators)
    return "NIL"

# =========================
# GENERATE QAM FORMAT
# =========================
def generate_qam(station, parsed, raw_metar):
    # Convert parsed data to display format
    display = format_parsed_for_display(parsed)
    
    # Extract supplementary info
    supp_info = extract_supplementary_info(raw_metar)
    if supp_info != "NIL":
        supp_info += "."

    # Get time from raw METAR
    match = re.search(r'(\d{2})(\d{2})(\d{2})Z', raw_metar)
    if match:
        day, hour, minute = match.groups()
        date_str = f"{day}/{datetime.utcnow().strftime('%m/%Y')}"
        time_str = f"{hour}.{minute} UTC"
    elif display["day"] != "-":
        date_str = f"{display['day']}/{datetime.utcnow().strftime('%m/%Y')}"
        time_str = f"{display['hour']}.{display['minute']} UTC"
    else:
        date_str = "-"
        time_str = "-"

    # ==========================================================
    # 🔥 WHATSAPP-FRIENDLY: Left-align dengan kolom tetap
    # Semua label di-left-align dalam lebar 9 karakter,
    # sehingga ':' selalu sejajar di posisi ke-10.
    # Saat di-copy, teks dibungkus triple backtick (```)
    # agar WhatsApp merender dengan font monospace.
    # ==========================================================
    
    # (label, value) — label None = baris tanpa label (separator)
    items = [
        ("DATE", date_str),
        ("TIME", time_str),
        (None, "=" * 25),
        ("WIND", display['wind']),
        ("VIS", display['visibility']),
        ("WEATHER", display['weather']),
        ("CLOUD", display['cloud']),
        ("TT/TD", display['temp_td']),
        ("QNH", f"{display['qnh']} MB"),
        ("QFE", f"{display['qfe']} MB"),
        ("REW¹W²", supp_info),
        ("TREND", display['trend']),
    ]
    
    lines = [
        "MET REPORT (QAM)",
        f"BANDARA JUANDA ({station})",
    ]
    
    for label, value in items:
        if label is None:
            lines.append(value)
        else:
            # ljust(9) = left-align, pad spasi ke kanan sampai total 9 karakter
            # sehingga ':' yang ditulis setelahnya selalu sejajar
            lines.append(f"{label.ljust(9)}: {value}")
    
    return "\n".join(lines)

# =========================
# GENERATE NARRATIVE TEXT - FINAL IMPROVED VERSION
# =========================
def generate_metar_narrative(parsed, raw_metar=None):
    """Generate Indonesian narrative text from METAR data with natural language format"""
    if not parsed:
        return "Data METAR tidak valid."
    
    display = format_parsed_for_display(parsed)
    # Rename 'text' to 'narrative' to avoid potential shadowing or LiteralString inference issues
    narrative: list[str] = []
    
    # Get station info
    station = display.get('station', 'Unknown')
    if raw_metar and (not station or station == "-"):
        station_match = re.match(r'([A-Z]{4})', raw_metar)
        if station_match:
            station = station_match.group(1)
    if not station or station == "-":
        station = "Unknown"
    
    # Get observation time
    day, hour, minute = "??", "??", "??"
    month_indonesian = ""
    year = datetime.utcnow().year
    
    current_month_name = datetime.utcnow().strftime("%B")
    
    if raw_metar:
        time_match = re.search(r'(\d{2})(\d{2})(\d{2})Z', raw_metar)
        if time_match:
            day, hour, minute = time_match.groups()
    elif display.get('day') != "-":
        day = display.get('day', '??')
        hour = display.get('hour', '??')
        minute = display.get('minute', '??')
    
    # Convert month name to Indonesian
    month_map = {
        "January": "Januari", "February": "Februari", "March": "Maret", "April": "April",
        "May": "Mei", "June": "Juni", "July": "Juli", "August": "Agustus",
        "September": "September", "October": "Oktober", "November": "November", "December": "Desember"
    }
    month_indonesian = month_map.get(current_month_name, current_month_name)
    
    # Opening sentence
    narrative.append(f"Observasi cuaca di Bandara Juanda ({station}) pada tanggal {day} {month_indonesian} {year} pukul {hour}:{minute} UTC menunjukkan kondisi berikut:")
    
    # Wind information - FORMAT: "160° derajat 13 Gust 27 Knot"
    wind = display.get('wind', '')
    if wind and wind != 'NIL':
        # Parse wind format: 160°/13G27KT, 160°/13KT, or VRB°/13KT
        wind_match = re.match(r'(\d{3}|VRB)°/(\d{2,3})(G(\d{2,3}))?KT', str(wind))
        if wind_match:
            dir_part = wind_match.group(1)
            wind_speed = wind_match.group(2)
            wind_gust = wind_match.group(4)
            
            if dir_part == "VRB":
                dir_text = "yang bervariasi"
            else:
                # Removed leading zeros (060 -> 60)
                dir_text = f"{int(dir_part)}° derajat"
            
            if wind_gust:
                wind_text = f"Angin dari arah {dir_text} dengan kecepatan angin {wind_speed} Gust {wind_gust} Knot."
            else:
                wind_text = f"Angin dari arah {dir_text} dengan kecepatan angin {wind_speed} Knot."
            narrative.append(wind_text)
        else:
            narrative.append(f"Angin dari arah {wind}.")
    
    # Visibility information
    vis = display.get('visibility', '')
    if vis and vis != 'NIL':
        if vis == "10 KM":
            narrative.append("Jarak pandang sekitar 10 kilometer.")
        elif "KM" in str(vis):
            km_val = str(vis).replace("KM", "").strip()
            # Hilangkan .0 jika ada
            km_val_clean = km_val.replace(".0", "") if ".0" in km_val else km_val
            narrative.append(f"Jarak pandang sekitar {km_val_clean} kilometer.")
        elif "M" in str(vis):
            m_val = str(vis).replace("M", "").strip()
            narrative.append(f"Jarak pandang sekitar {m_val} meter.")
        else:
            narrative.append(f"Visibilitas {vis}.")
    
    # Define weather map at function level for use in both Main and TEMPO sections
    weather_map: Dict[str, str] = {
        "HZ": "kabut asap", "RA": "hujan", "+RA": "hujan lebat", "-RA": "hujan ringan",
        "TS": "badai petir", "-TS": "badai petir ringan", "+TS": "badai petir kuat",
        "TSRA": "badai petir disertai hujan", "-TSRA": "badai petir ringan disertai hujan", 
        "+TSRA": "badai petir kuat disertai hujan", "VCTS": "badai petir di sekitar",
        "SH": "hujan shower", "SHRA": "hujan shower", "DS": "debu pasir", "SS": "pasir badai",
        "FG": "kabut", "BR": "kabut tipis", "DZ": "gerimis", "SN": "salju", "GR": "hujan es",
        "SQ": "angin kencang", "FC": "puting beliung", "VCTS": "badai petir di sekitar"
    }

    # Weather information
    weather = display.get('weather', '')
    if weather and weather != 'NIL':
        weather_desc = weather_map.get(str(weather), str(weather))
        narrative.append(f"Terdapat fenomena cuaca berupa {weather_desc}.")
    
    # Cloud information - FORMAT: "awan banyak pada ketinggian 1800 kaki CB (Cumulonimbus)"
    cloud = display.get('cloud', '')
    if cloud and cloud != 'NIL':
        cloud_map = {
            "FEW": "awan sedikit", "SCT": "awan tersebar", "BKN": "awan banyak", "OVC": "awan menutup langit"
        }
        # Parse cloud: BKN 1800FT CB, BKN018CB, atau SCT CB 1600FT
        # Regex fleksibel untuk menangkap CB/TCU baik sebelum atau sesudah angka ketinggian
        cloud_match = re.search(r'([A-Z]{3})\s*(CB|TCU)?\s*(\d+)(?:FT)?\s*(CB|TCU)?', str(cloud))
        if cloud_match:
            c_type, c_extra_pre, c_height, c_extra_post = cloud_match.groups()
            c_desc = cloud_map.get(c_type, c_type)
            
            # Tentukan info tambahan (CB/TCU)
            c_extra = c_extra_pre or c_extra_post
            c_extra_long = ""
            if c_extra == "CB":
                c_extra_long = "CB (Cumulonimbus)"
            elif c_extra == "TCU":
                c_extra_long = "TCU (Towering Cumulus)"
            
            if c_extra_long:
                # Format: Terdapat [deskripsi] [CB/TCU] pada ketinggian [X] kaki.
                narrative.append(f"Terdapat {c_desc} {c_extra_long} pada ketinggian {c_height} kaki.")
            else:
                narrative.append(f"Terdapat {c_desc} pada ketinggian {c_height} kaki.")
        else:
            narrative.append(f"Awan: {cloud}.")
    
    # Temperature and dewpoint
    temp_td = display.get('temp_td', '')
    if temp_td and temp_td != 'NIL':
        tt_match = re.match(r'(\d{2})/(\d{2})', str(temp_td))
        if tt_match:
            t_val, d_val = tt_match.groups()
            narrative.append(f"Suhu {t_val}°C dengan titik embun {d_val}°C.")
    
    # Pressure
    qnh = display.get('qnh', '')
    if qnh and qnh != 'NIL':
        narrative.append(f"Tekanan udara {qnh} hPa.")
    
    # TREND / TEMPO - FORMAT: "hingga pukul 08:30, dengan visibilitas 5 km, disertai hujan"
    trend_val = str(display.get('trend', ''))
    if trend_val and trend_val != 'NIL':
        if trend_val == 'NOSIG':
            narrative.append("Tidak ada perubahan signifikan dalam waktu dekat.")
        elif 'TEMPO' in trend_val.upper():
            tempo_items: list[str] = []
            # Extract time using groups to avoid variable slicing
            t_match = re.search(r'TL(\d{2})(\d{2})', trend_val)
            if t_match:
                hh, mm = t_match.groups()
                tempo_items.append(f"hingga pukul {hh}:{mm}")
            
            # Extract visibility (excluding digits inside time markers like TL0930)
            # We look for 4 digits NOT preceded by L (from TL), T (from AT), M (from FM) or Q
            v_match = re.search(r'(?<![LTMAQ\d])(\d{4})(?![\dZ])', trend_val)
            if v_match:
                raw_v = int(v_match.group(1))
                if raw_v >= 10000 or raw_v == 9999:
                    v_str = "10 km"
                elif raw_v >= 1000:
                    v_str = f"{raw_v // 1000} km" if raw_v % 1000 == 0 else f"{raw_v / 1000:.1f} km".replace(".0", "")
                else:
                    v_str = f"{raw_v} m"
                tempo_items.append(f"dengan visibilitas {v_str}")
            
            # Extract weather (Sync with main weather_map)
            # Find ALL matching phenomena in TEMPO segment
            tempo_weathers: list[str] = []
            codes = sorted(weather_map.keys(), key=len, reverse=True)
            
            # Divide trend_val into tokens to avoid partial matches (like 'RA' matching in 'TSRA')
            tempo_tokens = trend_val.split()
            for token in tempo_tokens:
                for w_code in codes:
                    if w_code == token:
                        w_desc = weather_map.get(w_code)
                        if w_desc:
                            tempo_weathers.append(w_desc)
                        break # Only one weather code per token
            
            if tempo_weathers:
                # Deduplicate while preserving order without using dict.fromkeys to satisfy linters
                unique_weathers: list[str] = []
                for w in tempo_weathers:
                    if w not in unique_weathers:
                        unique_weathers.append(w)
                
                tempo_items.append(f"disertai {' dan '.join(unique_weathers)}")
            
            if tempo_items:
                narrative.append(f"Dalam waktu dekat, diperkirakan akan terjadi {', '.join(tempo_items)}.")
            else:
                narrative.append(f"Tren: {trend_val}.")
        else:
            narrative.append(f"Tren: {trend_val}.")
    
    return " ".join(narrative)

# =========================
# HELPER FUNCTIONS FOR CHART DATA
# =========================
def extract_temp(metar):
    """Extract temperature from METAR string"""
    if not metar or not isinstance(metar, str):
        return 0
    try:
        parts = metar.split()
        for part in parts:
            if '/' in part and part != 'NIL':
                try:
                    temp = part.split('/')[0]
                    return int(temp) if temp.lstrip('-').isdigit() else 0
                except:
                    return 0
    except:
        return 0
    return 0

def extract_pressure(metar):
    """Extract pressure (QNH) from METAR string"""
    if not metar or not isinstance(metar, str):
        return 0
    try:
        if 'Q' in metar:
            try:
                idx = metar.find('Q')
                qnh = metar[idx+1:idx+5]  # pyre-ignore
                return int(qnh) if qnh.isdigit() else 0
            except:
                return 0
    except:
        return 0
    return 0

# API endpoints consolidated below (see get_history_api)


@app.route("/api/metar/<station_code>")
@admin_only_api
def api_metar_single(station_code):

    metar = get_metar(station_code.upper())
    if not metar:
        return jsonify({"error": "No METAR available"})
    
    parsed = parse_metar(metar)

    wind_direction = parsed.get("wind_dir")
    wind_speed = parsed.get("wind_speed_kt")

    if wind_direction == "VRB":
        wind_direction = "VRB"

    # format wind
    wind_text = None
    if parsed.get("wind_dir") and parsed.get("wind_speed_kt"):
        if parsed.get("wind_gust_kt"):
            wind_text = f"{parsed['wind_dir']}°/{parsed['wind_speed_kt']}G{parsed['wind_gust_kt']}KT"
        else:
            wind_text = f"{parsed['wind_dir']}°/{parsed['wind_speed_kt']}KT"

    return jsonify({
        "station": parsed.get("station"),
        "raw": metar,
        "wind": parsed.get("wind"),
        "wind_direction": parsed.get("wind_dir"),
        "wind_speed": parsed.get("wind_speed_kt"),
        "wind_gust": parsed.get("wind_gust_kt"),
        "visibility": format_visibility(parsed.get("visibility_m")),
        "weather": parsed.get("weather") or "NIL",
        "cloud": parsed.get("cloud") or "NIL",
        "qnh": parsed.get("pressure_hpa") or "NIL",
        "temp": parsed.get("temperature_c"),
        "dewpoint": parsed.get("dewpoint_c"),
        "visibility_m": parsed.get("visibility_m"),
        "status": parsed.get("status", "normal"),
        "report_type": parsed.get("report_type", "METAR")   # 🔥 UPDATED
    })

# =========================
# API GET NARRATIVE
# =========================
@app.route("/api/narrative/<station_code>")
@admin_only_api
def api_narrative(station_code):
    """API endpoint to get narrative text for a station"""
    metar = get_metar(station_code.upper())
    if not metar:
        return jsonify({"error": "No METAR available", "narrative": ""})
    
    parsed = parse_metar(metar)
    narrative = generate_metar_narrative(parsed, metar)
    
    return jsonify({
        "raw": metar,
        "narrative": narrative
    })

# =========================
# API CROSSWIND CALCULATOR
# =========================
@app.route("/api/crosswind")
@admin_only_api
def api_crosswind():
    """Calculate crosswind components"""
    wind_dir = request.args.get('wind_dir', type=int)
    wind_speed = request.args.get('wind_speed', type=float)
    runway_heading = request.args.get('runway_heading', type=int)
    
    if wind_dir is None or wind_speed is None or runway_heading is None:
        return jsonify({"error": "Missing parameters"}), 400
    
    angle_rad = math.radians(wind_dir - runway_heading)
    headwind = round(wind_speed * math.cos(angle_rad), 1)
    crosswind = round(abs(wind_speed * math.sin(angle_rad)), 1)
    tailwind = round(abs(headwind), 1) if headwind < 0 else 0
    headwind_val = headwind if headwind > 0 else 0
    
    return jsonify({
        "headwind": headwind_val,
        "crosswind": crosswind,
        "tailwind": tailwind,
        "wind_dir": wind_dir,
        "wind_speed": wind_speed,
        "runway_heading": runway_heading
    })

LAST_LOGGED_WIND = {
    '10': '',
    '28': ''
}

@app.route("/api/log-crosswind", methods=["POST"])
@admin_only_api
def log_crosswind():
    global LAST_LOGGED_WIND
    """Endpoint untuk menyimpan perhitungan crosswind dari frontend"""
    try:
        data = request.json
        if not data:
            return jsonify({"error": "No data provided"}), 400
            
        # Validasi data wajib
        required = ['runway', 'wind_dir', 'wind_speed', 'headwind', 'crosswind', 'tailwind']
        if not all(k in data for k in required):
            return jsonify({"error": "Missing required fields"}), 400
            
        # Tambahkan timestamp server
        data['timestamp'] = datetime.utcnow().isoformat()
        data['station'] = data.get('station', 'WARR')
        # Check duplication globally across server
        runway = str(data['runway'])
        metar_raw = data.get('metar_raw', '').strip()
        
        if metar_raw and LAST_LOGGED_WIND.get(runway) == metar_raw:
            return jsonify({
                "status": "success", 
                "message": "Duplicate calculation ignored",
                "timestamp": data['timestamp']
            }), 200
            
        LAST_LOGGED_WIND[runway] = metar_raw
        
        # Simpan
        success = save_wind_calculation(data)
        
        if success:
            return jsonify({
                "status": "success", 
                "message": "Wind calculation logged",
                "timestamp": data['timestamp']
            }), 200
        else:
            return jsonify({
                "status": "error",
                "error": "Database write failed (Sheets & CSV failed)"
            }), 500
            
    except Exception as e:
        print(f"[WIND LOG] Critical Exception: {traceback.format_exc()}", file=sys.stderr)
        return jsonify({"error": str(e)}), 500

@app.route("/api/wind-logs")
@admin_only_api
def get_wind_logs():
    """Ambil history perhitungan crosswind (hybrid)"""
    try:
        runway = request.args.get('runway')
        start_date = request.args.get('start')
        end_date = request.args.get('end')
        
        # Default to Today UTC (00:00:00 to 23:59:59)
        if not start_date and not end_date:
            now = datetime.utcnow()
            start_date = now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        
        # Coba dari Sheets dulu
        try:
            logs = sheets_handler.get_wind_logs(
                limit=1000, 
                runway=runway, 
                start_date=start_date, 
                end_date=end_date
            )
            if logs:
                # 🔥 DATA COERCION & AUTO-HEALING: 
                # Memperbaiki display 98,0 vs 9,8 secara otomatis jika terdeteksi anomali 10x
                for log in logs:
                    try:
                        w_spd_raw = log.get('wind_speed', 0)
                        w_speed = float(str(w_spd_raw).replace(',', '.')) if w_spd_raw is not None else 0.0
                        
                        for field in ['headwind', 'crosswind', 'tailwind']:
                            if field in log and log[field] is not None:
                                # Bersihkan string jika mengandung koma (format Indonesia dari Sheets)
                                val_str = str(log[field]).replace(',', '.')
                                val = float(val_str)
                                
                                # LOGIKA AUTO-HEALING: 
                                # Jika angka > speed + toleransi (fisik tidak mungkin), bagi 10
                                if abs(val) > (abs(w_speed) + 2) and abs(val) > 10:
                                    val = val / 10.0
                                    
                                log[field] = val
                    except (ValueError, TypeError):
                        pass

                return jsonify({
                    "logs": logs,
                    "count": len(logs),
                    "source": "Google Sheets"
                })
        except Exception as e:
            print(f"[WIND LOG] Fetch from sheets error: {e}", file=sys.stderr)
            
        # Fallback ke CSV
        if not os.path.exists(WIND_LOG_FILE):
            return jsonify({"logs": [], "count": 0, "source": "CSV"})
            
        df = pd.read_csv(WIND_LOG_FILE)
        
        if runway:
            df = df[df['runway'] == str(runway)]
        if start_date:
            df = df[df['timestamp'] >= start_date]
        if end_date:
            df = df[df['timestamp'] <= end_date]
            
        df = df.sort_values('timestamp', ascending=False).head(1000)
        
        # Ensure NaNs are converted to None for valid JSON output
        logs = df.where(pd.notnull(df), None).to_dict('records')
        
        # 🔥 DATA COERCION & AUTO-HEALING (CSV Path)
        for log in logs:
            try:
                w_spd_raw = log.get('wind_speed', 0)
                w_speed = float(str(w_spd_raw).replace(',', '.')) if w_spd_raw is not None else 0.0
                
                for field in ['headwind', 'crosswind', 'tailwind']:
                    if field in log and log[field] is not None:
                        val_str = str(log[field]).replace(',', '.')
                        val = float(val_str)
                        
                        # LOGIKA AUTO-HEALING
                        if abs(val) > (abs(w_speed) + 2) and abs(val) > 10:
                            val = val / 10.0
                        
                        log[field] = val
            except (ValueError, TypeError):
                pass
        
        return jsonify({
            "logs": logs,
            "count": len(logs),
            "source": "CSV Fallback"
        })
        
    except Exception as e:
        print(f"[WIND LOG] Error reading logs: {e}", file=sys.stderr)
        return jsonify({"error": str(e)}), 500

@app.route("/api/wind-logs/by-metar")
@admin_only_api
def get_wind_logs_by_metar():
    """Ambil wind logs yang dikelompokkan per METAR timestamp"""
    try:
        # Coba dari Sheets dulu
        try:
            groups = sheets_handler.get_wind_logs_by_metar(limit=50)
            if groups:
                return jsonify({
                    "metar_groups": groups,
                    "count": len(groups),
                    "source": "Google Sheets"
                })
        except Exception as e:
            print(f"[WIND LOG] Fetch from sheets error: {e}", file=sys.stderr)
            
        # Fallback ke CSV
        if not os.path.exists(WIND_LOG_FILE):
            return jsonify({"metar_groups": [], "count": 0, "source": "CSV"})
            
        df = pd.read_csv(WIND_LOG_FILE)
        
        # Group by timestamp 
        grouped = df.groupby('timestamp').apply(
            lambda x: {
                "timestamp": x['timestamp'].iloc[0],
                "metar_raw": x['metar_raw'].iloc[0],
                "wind": f"{x['wind_dir'].iloc[0]}°/{x['wind_speed'].iloc[0]}kt",
                "runways": x[['runway', 'headwind', 'crosswind', 'tailwind', 
                             'crosswind_status', 'tailwind_status']].to_dict('records')
            }
        ).tolist()
        
        # sort descending
        grouped = sorted(grouped, key=lambda x: str(x.get('timestamp', '')), reverse=True)
        
        return jsonify({
            "metar_groups": grouped,
            "count": len(grouped),
            "source": "CSV Fallback"
        })
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/wind-logs/export")
@admin_only_api
def export_wind_logs():
    """Export wind logs ke CSV untuk investigasi"""
    try:
        data_to_export = []
        
        # Ambil dari sheets
        try:
            data_to_export = sheets_handler.get_wind_logs(limit=1000)
        except Exception:
            pass
            
        # Fallback
        if not data_to_export and os.path.exists(WIND_LOG_FILE):
            df = pd.read_csv(WIND_LOG_FILE)
            data_to_export = df.to_dict('records')
            
        if not data_to_export:
            return "No data available", 404
            
        df_export = pd.DataFrame(data_to_export)
        
        buffer = BytesIO()
        df_export.to_csv(buffer, index=False)
        buffer.seek(0)
        
        timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
        
        return send_file(
            buffer,
            as_attachment=True,
            download_name=f"WIND_INVESTIGATION_LOG_{timestamp}.csv",
            mimetype="text/csv"
        )
        
    except Exception as e:
        return str(e), 500

# =========================
# API WIND ROSE - Historical Wind Data
# =========================
# =========================
# API WIND ROSE - Dual Time Range Filter
# =========================
@app.route("/api/windrose/<station>")
@admin_only_api
def windrose_api(station):
    """API endpoint untuk Wind Rose 24 jam terakhir - FETCH FROM SHEETS for Real-time Sync"""
    global CSV_FILE
    
    # 🔥 UTC-ONLY: Yesterday's Full Day window (00:00 to 23:59 UTC)
    now_utc = datetime.utcnow()
    yesterday = now_utc - timedelta(days=1)
    cutoff_time = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    end_cutoff_time = yesterday.replace(hour=23, minute=59, second=59, microsecond=999999)
    # y_end matching yesterday_records endpoint
    y_end_simple = yesterday.replace(hour=23, minute=59, second=59)
    
    print(f"[WINDROSE 24H] {station}: Yesterday UTC Range {cutoff_time} to {end_cutoff_time}", file=sys.stderr)
    
    filtered_data = []
    station_df = pd.DataFrame()
    # 🔥 FETCH DIRECTLY FROM GOOGLE SHEETS for consistent sync
    try:
        all_records = sheets_handler.get_all_data()
        if all_records:
            df = pd.DataFrame(all_records)
            df["time"] = pd.to_datetime(df["time"], errors='coerce')
            print(f"[WINDROSE 24H] Total records from Sheets: {len(df)}", file=sys.stderr)
            
            # Filter untuk station dan rentang hari ini (WIB 00.00 - 00.00)
            station_df = df[
                (df["station"].str.strip().str.upper() == station.upper()) &
                (df["time"] >= cutoff_time) &
                (df["time"] < end_cutoff_time)
            ]
            
            print(f"[WINDROSE 24H] Found {len(station_df)} rows for yesterday's range", file=sys.stderr)
            
            # If yesterday has no data, try today's range as fallback
            if len(station_df) == 0:
                print(f"[WINDROSE 24H] No data for yesterday, trying today's range...", file=sys.stderr)
                now_wib = now_utc + timedelta(hours=7)
                start_today_wib = now_wib.replace(hour=0, minute=0, second=0, microsecond=0)
                # Today: start_today_wib (UTC) to now
                today_cutoff = start_today_wib - timedelta(hours=7)
                station_df = df[
                    (df["station"].str.strip().str.upper() == station.upper()) &
                    (df["time"] >= today_cutoff) &
                    (df["time"] <= now_utc)
                ]
                if len(station_df) > 0:
                    print(f"[WINDROSE 24H] Found {len(station_df)} rows for today's range (fallback)", file=sys.stderr)
            
            for _, row in station_df.iterrows():
                metar = str(row["metar"]) if pd.notna(row["metar"]) else ""
                if not metar:
                    continue
                
                # Extract wind data using regex (standardize with monthly API)
                wind_match = re.search(r'\b(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT\b', metar)
                if wind_match:
                    try:
                        wind_dir = wind_match.group(1)
                        if wind_dir != "VRB":
                            utc_str = row['time'].strftime('%Y-%m-%d %H:%M UTC')
                            wib_time = row['time'] + timedelta(hours=7)
                            wib_str = wib_time.strftime('%Y-%m-%d %H:%M WIB')
                            filtered_data.append({
                                "time": row["time"].strftime("%Y-%m-%d %H:%M:%S"),
                                "utc_time": utc_str,
                                "wib_time": wib_str,
                                "station": station,
                                "dir": int(wind_dir),
                                "speed": float(wind_match.group(2))
                            })
                    except:
                        continue
        else:
            print(f"[WINDROSE 24H] No records returned from Sheets", file=sys.stderr)
    except Exception as e:
        print(f"[WINDROSE 24H] Sheets Error: {e}", file=sys.stderr)
        # Fallback to local CSV if Sheets fails
        if os.path.exists(CSV_FILE):
             try:
                df_local = pd.read_csv(CSV_FILE)
                df_local["time"] = pd.to_datetime(df_local["time"], errors='coerce')
                local_filtered = df_local[
                    (df_local["station"].str.strip().str.upper() == station.upper()) &
                    (df_local["time"] >= cutoff_time) &
                    (df_local["time"] < end_cutoff_time)
                ]
                for _, row in local_filtered.iterrows():
                    metar = str(row["metar"])
                    wind_match = re.search(r'\b(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT\b', metar)
                    if wind_match and wind_match.group(1) != "VRB":
                        filtered_data.append({
                            "time": row["time"].strftime("%Y-%m-%d %H:%M:%S"),
                            "utc_time": f"{row['time'].strftime('%Y-%m-%d %H:%M UTC')}",
                            "station": station,
                            "dir": int(wind_match.group(1)),
                            "speed": float(wind_match.group(2))
                        })
             except: pass

    # Binning data for BMKG Standard
    binned_data = bin_wind_data(filtered_data)
    
    # Determine source (logic matches implementation above)
    source_info = "Sheets" if IS_VERCEL else "Local CSV"
    total_found = len(station_df)
    
    # Use UTC boundaries for range labels (Include Indonesian Date)
    date_display = format_indonesian_date(cutoff_time)
    start_range = f"{date_display} • 00:00 UTC"
    end_range = "23:59 UTC"
    
    print(f"[WINDROSE 24H] Returning binned data with {len(filtered_data)} points", file=sys.stderr)

    return jsonify({
        "period": "24h",
        "data": filtered_data,
        "binned": binned_data,
        "count": total_found,
        "date_info": date_display,
        "range": {
            "start": start_range,
            "end": end_range
        },
        "source": source_info
    })

@app.route("/api/windrose-monthly/<station>")
@admin_only_api
def windrose_monthly_api(station):
    """API endpoint untuk Wind Rose 1 bulan penuh (bulan sebelumnya) - FETCH FROM SHEETS"""
    now = datetime.utcnow()
    
    # Hitung bulan sebelumnya
    if now.month == 1:
        target_year = now.year - 1
        target_month = 12  # Desember
    else:
        target_year = now.year
        target_month = now.month - 1
    
    # Buat rentang waktu: 1 hari target_month sampai akhir target_month
    start_date = datetime(target_year, target_month, 1)
    # Hitung akhir bulan (hari pertama bulan berikutnya dikurangi 1 detik)
    if target_month == 12:
        end_date = datetime(target_year + 1, 1, 1) - timedelta(seconds=1)
    else:
        end_date = datetime(target_year, target_month + 1, 1) - timedelta(seconds=1)
    
    print(f"[WINDROSE MONTHLY] {station}: Fetching from Google Sheets for {target_year}-{target_month:02d}", file=sys.stderr)
    
    monthly_data = []
    used_current_month = False
    station_df = pd.DataFrame()
    # 🔥 FETCH DIRECTLY FROM GOOGLE SHEETS AS REQUESTED
    try:
        all_records = sheets_handler.get_all_data()
        if all_records:
            df = pd.DataFrame(all_records)
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"], errors='coerce')
                print(f"[WINDROSE MONTHLY] Total records from Sheets: {len(df)}", file=sys.stderr)
                
                # Filter untuk station dan rentang bulan target
            station_df = df[
                (df["station"].str.strip().str.upper() == station.upper()) &
                (df["time"] >= start_date) &
                (df["time"] <= end_date)
            ]
            
            print(f"[WINDROSE MONTHLY] Found {len(station_df)} rows for {target_year}-{target_month:02d}", file=sys.stderr)
            
            # Fallback: if previous month has NO data, try CURRENT month
            if len(station_df) == 0:
                print(f"[WINDROSE MONTHLY] No data for previous month, trying current month...", file=sys.stderr)
                current_start = datetime(now.year, now.month, 1)
                station_df = df[
                    (df["station"].str.strip().str.upper() == station.upper()) &
                    (df["time"] >= current_start) &
                    (df["time"] <= now)
                ]
                if len(station_df) > 0:
                    used_current_month = True
                    target_month = now.month
                    target_year = now.year
                    start_date = current_start
                    end_date = now
                    print(f"[WINDROSE MONTHLY] Found {len(station_df)} rows for current month (fallback)", file=sys.stderr)
            
            for _, row in station_df.iterrows():
                metar = str(row["metar"]) if pd.notna(row["metar"]) else ""
                if not metar:
                    continue
                
                # Extract wind data using regex
                wind_match = re.search(r'\b(\d{3}|VRB)(\d{2,3})(G\d{2,3})?KT\b', metar)
                if wind_match:
                    try:
                        wind_dir = wind_match.group(1)
                        if wind_dir != "VRB":
                            utc_str = row['time'].strftime('%Y-%m-%d %H:%M UTC')
                            wib_time = row['time'] + timedelta(hours=7)
                            wib_str = wib_time.strftime('%Y-%m-%d %H:%M WIB')
                            monthly_data.append({
                                "time": row["time"].strftime("%Y-%m-%d %H:%M:%S"),
                                "utc_time": utc_str,
                                "wib_time": wib_str,
                                "station": station,
                                "dir": int(wind_dir),
                                "speed": float(wind_match.group(2))
                            })
                    except:
                        continue
        else:
            print(f"[WINDROSE MONTHLY] No records returned from Sheets", file=sys.stderr)
    except Exception as e:
        print(f"[WINDROSE MONTHLY] Sheets Error: {e}", file=sys.stderr)
    
    # Format nama bulan untuk display
    month_names = {
        1: "Januari", 2: "Februari", 3: "Maret", 4: "April",
        5: "Mei", 6: "Juni", 7: "Juli", 8: "Agustus",
        9: "September", 10: "Oktober", 11: "November", 12: "Desember"
    }
    month_name = month_names.get(target_month, str(target_month))
    
    # Binning data
    binned_data = bin_wind_data(monthly_data)
    
    print(f"[WINDROSE MONTHLY] Returning binned data for {month_name} {target_year}", file=sys.stderr)
    
    return jsonify({
        "period": "monthly" if not used_current_month else "current_month",
        "month": target_month,
        "year": target_year,
        "month_name": month_name,
        "data": monthly_data,
        "binned": binned_data,
        "count": len(station_df) if isinstance(station_df, pd.DataFrame) else 0,
        "range": {
            "start": start_date.strftime("%Y-%m-%d"),
            "end": end_date.strftime("%Y-%m-%d") if isinstance(end_date, datetime) else str(end_date)
        }
    })

# =========================
# API HISTORY - Technical History for Charts
# =========================
@app.route("/api/latest")
@app.route("/api/history")
@app.route("/api/metar/history")
@admin_only_api
def get_history_api():
    """Returns historical data in JSON format for charts and tables"""
    global last_metar_update, auto_fetch, _cached_history, _history_cache_time
    
    now_ts = time.time()
    
    # 🔥 CACHE HIT untuk history (jarang berubah)
    if _cached_history and (now_ts - _history_cache_time < HISTORY_CACHE_TTL):
        data = _cached_history.copy()
        data['cached'] = True
        response = make_response(jsonify(data))
        response.headers['Cache-Control'] = 'public, max-age=600'  # 10 menit cache
        return response
    
    # 🔥 VERCEL STALE CHECK:
    # Ensure history is relatively fresh from Sheets if on Vercel
    if IS_VERCEL and auto_fetch:
        now = datetime.utcnow()
        should_sync = False
        if not last_metar_update:
            should_sync = True
        else:
            try:
                last_dt = pd.to_datetime(last_metar_update.replace("Z", ""), format='mixed')
                if (now - last_dt).total_seconds() > 60: # History check can be slightly more relaxed
                    should_sync = True
            except:
                should_sync = True
        
        if should_sync:
            print("[HISTORY] Local cache stale, syncing from Sheets...", file=sys.stderr)
            sheets_handler.sync_to_local(CSV_FILE)
            last_metar_update = datetime.utcnow().isoformat() + "Z"

    if not os.path.exists(CSV_FILE):
        # Warmup: if no history, try to fetch current METAR to initialize
        station = "WARR"
        metar = get_metar(station)
        if metar:
            df = pd.DataFrame([{"station": station, "time": datetime.utcnow(), "metar": metar}])
            df.to_csv(CSV_FILE, index=False)
        else:
            return jsonify({"data": [], "labels": [], "temps": [], "pressures": []})

    try:
        df = fetch_history_from_source()
        if not df.empty:
            df = df.tail(30)
        df["metar"] = df.get("metar", pd.Series(dtype='str')).fillna("").astype(str)
        
        # Format for history table (newest first)
        data_list = []
        for _, row in df.iloc[::-1].iterrows():
            parsed = parse_metar(str(row["metar"]))
            # Parse time for better display
            dt = pd.to_datetime(row["time"])
            data_list.append({
                "day_name": dt.strftime('%A'),
                "time": dt.strftime('%H:%M'),
                "full_time": pd.to_datetime(row["time"]).strftime("%Y-%m-%d %H:%M UTC"),
                "station": row["station"],
                "metar": row["metar"],
                "temp": extract_temp(str(row["metar"])),
                "pressure": extract_pressure(str(row["metar"])),
                "wind": parsed.get("wind_speed_kt"),
                "gust": parsed.get("wind_gust_kt"),
                "validation_results": validate_metar(str(row["metar"]))
            })

        # Format for charts (oldest to newest)
        labels = [pd.to_datetime(t).strftime("%d/%m/%y %H:%M UTC") for t in df["time"]]
        temps = [extract_temp(m) for m in df["metar"]]
        pressures = [extract_pressure(m) for m in df["metar"]]
        
        # Calculate range and source
        start_time = pd.to_datetime(df["time"].iloc[0]).strftime("%Y-%m-%d %H:%M UTC") if not df.empty else ""
        end_time = pd.to_datetime(df["time"].iloc[-1]).strftime("%Y-%m-%d %H:%M UTC") if not df.empty else ""
        
        # In Vercel environment, data is synced from Sheets
        source_info = "Sheets" if IS_VERCEL else "Local CSV"

        result_dict = {
            "data": data_list,
            "labels": labels,
            "temps": temps,
            "pressures": pressures,
            "range": {
                "start": start_time,
                "end": end_time
            },
            "count": len(df),
            "source": source_info
        }
        
        # Simpan ke cache
        _cached_history = result_dict
        _history_cache_time = now_ts
        
        response = make_response(jsonify(result_dict))
        response.headers['Cache-Control'] = 'public, max-age=600, stale-while-revalidate=1200'
        return response
    except Exception as e:
        print(f"[API] History error: {e}", file=sys.stderr)
        return jsonify({"error": str(e), "data": []}), 500

@app.route("/api/metar/<station>")
@admin_only_api
def get_single_metar_api(station):
    """Returns the latest single METAR data for a station"""
    metar = get_metar(station)
    if not metar and os.path.exists(CSV_FILE):
        # Fallback to last known from CSV
        df = pd.read_csv(CSV_FILE)
        station_df = df[df["station"] == station]
        if not station_df.empty:
            metar = station_df.iloc[-1]["metar"]
    
    if metar:
        parsed = parse_metar(metar)
        return jsonify({
            "raw": metar,
            "station": station,
            "wind_direction": parsed.get("wind_dir"),
            "wind_speed": parsed.get("wind_speed_kt"),
            "visibility_m": parsed.get("visibility_m"),
            "status": parsed.get("status", "normal"),
            "report_type": parsed.get("report_type", "METAR")
        })
    return jsonify({"error": "No data available"}), 404

# =========================
# DATA SOURCE HELPER
# =========================
def fetch_history_from_source():
    """
    Unified fetcher for METAR history. 
    Prioritizes Google Sheets, fallbacks to local CSV if Sheets fails.
    """
    try:
        # 1. Try Google Sheets first (Preferred for Vercel/Cloud)
        print("[DATA] Fetching history from Google Sheets...", file=sys.stderr)
        all_data = sheets_handler.get_all_data()
        if all_data:
            df = pd.DataFrame(all_data)
            if not df.empty and "time" in df.columns:
                print(f"[DATA] Successfully fetched {len(df)} records from Sheets", file=sys.stderr)
                return df
                
        # 2. Fallback to local CSV
        if os.path.exists(CSV_FILE):
            print("[DATA] Falling back to local CSV...", file=sys.stderr)
            return pd.read_csv(CSV_FILE)
            
    except Exception as e:
        print(f"[DATA] [ERROR] Error fetching history: {e}", file=sys.stderr)
        
    return pd.DataFrame(columns=["station", "time", "metar"])

# ============ AUTH ROUTES ============
@app.route("/login", methods=["GET", "POST"])
def login_page():
    if 'admin_logged_in' in session:
        return redirect(url_for('home'))
    
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        
        # Validasi kredensial (Admin atau Guest)
        is_admin = (username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password))
        is_guest = (username == GUEST_USERNAME and check_password_hash(GUEST_PASSWORD_HASH, password))

        if is_admin or is_guest:
            session['admin_logged_in'] = True
            session['admin_user'] = username
            session['last_activity'] = time.time()
            session.permanent = True if request.form.get("remember") else False
            
            role = "Admin" if is_admin else "Guest"
            print(f"[AUTH] {role} '{username}' login sukses dari {request.remote_addr}", file=sys.stderr)
            return redirect(request.args.get('next') or url_for('home'))
        else:
            flash('Username atau password salah', 'danger')
            print(f"[AUTH] Login gagal dari {request.remote_addr}", file=sys.stderr)
    
    response = make_response(render_template("login.html"))
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response

@app.route("/logout")
def logout():
    user = session.get('admin_user', 'unknown')
    session.clear()
    session.permanent = False
    
    response = make_response(redirect(url_for('login_page')))
    # Hapus cookie secara eksplisit untuk keamanan tambahan
    response.set_cookie('metar_session', '', expires=0)
    
    flash('Anda telah berhasil logout', 'info')
    print(f"[AUTH] Admin '{user}' logged out", file=sys.stderr)
    return response

@app.route("/api/auth/status")
def auth_status():
    """Cek status autentikasi untuk frontend AuthManager"""
    is_logged_in = 'admin_logged_in' in session
    if is_logged_in:
        session['last_activity'] = time.time()
        
    return jsonify({
        "authenticated": is_logged_in,
        "user": session.get('admin_user') if is_logged_in else None
    })

# =========================
# HOME ROUTE
# =========================
@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    # Tambahkan header untuk mencegah caching halaman dashboard yang sensitif
    # agar setelah logout, tombol 'Back' atau 'Refresh' tidak menampilkan data lama
    response_headers = {
        'Cache-Control': 'no-store, no-cache, must-revalidate, max-age=0',
        'Pragma': 'no-cache',
        'Expires': '0'
    }

    global last_metar_update, auto_fetch
    station = "WARR"
    metar = None
    parsed = {}
    qam = None
    narrative = None
    latest = None
    latest_wind_obj = {}
    safe_recent_winds = []
    temps = []
    pressures = []
    has_history = False

    try:
        print("\n=== HOME ROUTE CALLED ===", file=sys.stderr)
        
        # 🔥 SELALU baca dari Sheets, bukan fetch live
        # Fetch dari Sheets (cached 5 menit di sheets_handler)
        all_data = sheets_handler.get_all_data()
        
        if not all_data:
            # Fallback: coba CSV lokal
            df = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else pd.DataFrame()
        else:
            df = pd.DataFrame(all_data)
        
        if not df.empty:
            # Gunakan data paling terbaru untuk display utama
            latest_row = df.iloc[-1]
            metar = str(latest_row['metar'])
            station = str(latest_row['station'])
            parsed = parse_metar(metar)
            qam = generate_qam(station, parsed, metar)
            narrative = generate_metar_narrative(parsed, metar)
            
            try:
                last_metar_update = pd.to_datetime(latest_row["time"], format='mixed').isoformat() + "Z"
            except:
                pass
            
            store_wind(parsed, station)
        else:
            # No data today - show warning tapi tetap render
            metar = None
            qam = "No data available"
            narrative = "Data collection may be delayed"
            parsed = {}

        if request.method == "POST":
            # Just updating station filter if POST
            station = request.form.get("icao", "WARR").upper()

    except Exception as e:
        print(f"[HOME] CRITICAL ERROR: {e}", file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)

    # Read history and prepare chart data
    history_start = ""
    history_end = ""
    history_count = 0
    history_source = "Sheets" if IS_VERCEL else "Local CSV"

    full_history = fetch_history_from_source()
    if not full_history.empty:
        # Calculate TODAY in UTC
        now_utc = datetime.utcnow()
        today_str = now_utc.strftime("%Y-%m-%d")
        current_day = now_utc.strftime("%A")
        
        # Filter for today's records (UTC date)
        # Ensure time column is string for startswith comparison
        full_history['time'] = full_history['time'].astype(str)
        history = full_history[full_history['time'].str.startswith(today_str)].copy()
        
        # Update history source if data actually came from Sheets
        # (This is a bit redundant but helps UI accuracy)
        if len(full_history) > 0 and history_source == "Local CSV":
             # We can't easily know if it came from Sheets unless we check sheets_handler status
             pass
        
        # Add day name for the table
        if not history.empty:
            history['day_name'] = pd.to_datetime(history['time']).dt.strftime('%A')
            history['time_short'] = pd.to_datetime(history['time']).dt.strftime('%H:%M')
            
            # Convert metar to string and fillna first to avoid errors
            history["metar"] = history["metar"].fillna("").astype(str)
            
            # Extract minute for METAR/SPECI status detection (0 or 30 = normal, else = SPECI)
            # Use regex to extract from METAR time group (DDHHMMZ) if available, fallback to system time
            def get_metar_minute(row):
                metar = str(row['metar'])
                match = re.search(r'\b\d{6}Z\b', metar)
                if match:
                    try:
                        return int(match.group(0)[4:6])
                    except: pass
                # Fallback to system timestamp
                try:
                    return pd.to_datetime(row['time']).minute
                except:
                    return -1
            
            history['status_minute'] = history.apply(get_metar_minute, axis=1)
        
        has_history = not history.empty
        if has_history:
            labels = [pd.to_datetime(t).strftime("%d/%m/%y %H:%M") for t in history['time'].tolist()]
            temps = [extract_temp(m) for m in history['metar'].tolist()]
            pressures = [extract_pressure(m) for m in history['metar'].tolist()]
            
            # Extract winds and gusts for trend charts
            winds = []
            gusts = []
            for m in history['metar'].tolist():
                wind_match = re.search(r'(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT', m)
                if wind_match:
                    winds.append(int(wind_match.group(2)))
                    gusts.append(int(wind_match.group(4)) if wind_match.group(4) else None)
                else:
                    winds.append(None)
                    gusts.append(None)
            
            history_start = pd.to_datetime(history['time'].iloc[0]).strftime("%Y-%m-%d %H:%M")
            history_end = pd.to_datetime(history['time'].iloc[-1]).strftime("%Y-%m-%d %H:%M")
            history_count = len(history)
        else:
            labels = []
            winds = []
            gusts = []
    else:
        history = pd.DataFrame(columns=["station", "time", "metar"])
        labels = []
        winds = []
        gusts = []
    
    # Convert recent winds for Wind Compass
    safe_recent_winds = []
    for w in wind_history:
        if w.get("station") == station:
            safe_recent_winds.append({
                "dir": w.get("dir"),
                "speed": w.get("speed"),
                "timestamp": w.get("time")
            })

    # Extract latest wind specifically
    latest_wind_obj = {}
    if parsed and parsed.get('wind_dir') is not None and parsed.get('wind_speed_kt') is not None:
        latest_wind_obj = {
            "dir": parsed['wind_dir'],
            "speed": parsed['wind_speed_kt']
        }

    # Create latest dict for the METAR display with status color
    latest = None
    if metar and parsed:
        latest = {
            "station": station,
            "metar": metar,
            "status": parsed.get("status", "normal"),
            "report_type": detect_metar_report_type(metar)
        }

    last_saved = history["time"].iloc[-1] if has_history else "N/A"
    print(f"[HOME] Rendering template with QAM: {qam is not None}")

    # Pre-format last_metar_update for the template (WIB)
    last_update_display = "--:-- WIB"
    if last_metar_update:
        try:
            # last_metar_update is ISO string (UTC)
            dt = pd.to_datetime(last_metar_update)
            # Manual WIB offset (UTC+7)
            wib_dt = dt + timedelta(hours=7)
            last_update_display = wib_dt.strftime("%H:%M") + " WIB"
        except:
            pass

    res = make_response(render_template(
        "index.html",
        station=station,
        latest=latest,
        qam=qam,
        narrative=narrative,
        history=history,
        current_day=current_day if 'current_day' in locals() else "",
        last_saved=last_saved,
        temps=temps,
        pressures=pressures,
        winds=winds,
        gusts=gusts,
        labels=labels,
        has_history=has_history,
        auto_fetch=auto_fetch,
        last_metar_update=last_update_display,
        history_start=history_start,
        history_end=history_end,
        history_count=history_count,
        history_source=history_source,
        recent_winds=json.dumps(safe_recent_winds),
        latest_wind=json.dumps(latest_wind_obj)
    ))
    
    for k, v in response_headers.items():
        res.headers[k] = v
        
    return res

def common_view_context_data():
    """Helper to get common metrics for all templates without rendering"""
    global last_metar_update, auto_fetch, latest_metar_data
    station = "WARR"
    metar = None
    parsed = {}
    qam = None
    narrative = None
    latest_wind_obj = {}
    
    # Fetch latest data from cache
    metar = str(latest_metar_data.get("raw") or "")
    if metar:
        parsed = parse_metar(metar)
        qam = latest_metar_data.get("qam")
        narrative = latest_metar_data.get("narrative")
    
    # Read history and prepare chart data
    history_start = ""
    history_end = ""
    history_count = 0
    history_source = "Sheets" if IS_VERCEL else "Local CSV"
    
    labels = []
    temps = []
    pressures = []
    winds = []
    gusts = []
    has_history = False
    history = pd.DataFrame()
    current_day = ""
    
    full_history = fetch_history_from_source()
    if not full_history.empty:
        # Calculate TODAY in UTC
        now_utc = datetime.utcnow()
        today_str = now_utc.strftime("%Y-%m-%d")
        current_day = now_utc.strftime("%A")
        
        # Filter for today's records (WIB date)
        full_history['time'] = full_history['time'].astype(str)
        history = full_history[full_history['time'].str.contains(today_str)].copy()
        
        # Add day name and formatted time for the table
        if not history.empty:
            history['day_name'] = pd.to_datetime(history['time']).dt.strftime('%A')
            history['time_short'] = pd.to_datetime(history['time']).dt.strftime('%H:%M')
            
        history["metar"] = history["metar"].fillna("").astype(str)
        has_history = not history.empty
        if has_history:
            for _, row in history.iterrows():
                m = str(row["metar"])
                labels.append(pd.to_datetime(row["time"]).strftime("%d/%m/%y %H:%M"))
                temps.append(extract_temp(m))
                pressures.append(extract_pressure(m))
                
                # Extract wind speed and gust for charts
                wind_match = re.search(r'(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT', m)
                if wind_match:
                    winds.append(int(wind_match.group(2)))
                    gusts.append(int(wind_match.group(4)) if wind_match.group(4) else None)
                else:
                    winds.append(None)
                    gusts.append(None)
            
            history_start = pd.to_datetime(history['time'].iloc[0]).strftime("%Y-%m-%d %H:%M")
            history_end = pd.to_datetime(history['time'].iloc[-1]).strftime("%Y-%m-%d %H:%M")
            history_count = len(history)

    # Convert recent winds strictly to a list of dicts
    safe_recent_winds = []
    for w in wind_history:
        if w.get("station") == station:
            safe_recent_winds.append({
                "dir": w.get("dir"),
                "speed": w.get("speed"),
                "timestamp": w.get("time")
            })
            
    # Extract latest wind
    latest_wind_obj = {}
    if parsed and parsed.get('wind_dir') is not None and parsed.get('wind_speed_kt') is not None:
        latest_wind_obj = {
            "dir": parsed['wind_dir'],
            "speed": parsed['wind_speed_kt']
        }

    return {
        "station": station,
        "latest": {"station": station, "metar": metar, "status": parsed.get("status", "normal"), "report_type": detect_metar_report_type(metar)} if parsed else None,
        "qam": qam,
        "narrative": narrative,
        "history": history,
        "current_day": current_day,
        "has_history": has_history,
        "temps": temps,
        "pressures": pressures,
        "winds": winds,
        "gusts": gusts,
        "labels": labels,
        "history_start": history_start,
        "history_end": history_end,
        "history_count": history_count,
        "history_source": history_source,
        "recent_winds": json.dumps(safe_recent_winds),
        "latest_wind": json.dumps(latest_wind_obj),
        "auto_fetch": auto_fetch,
        "last_metar_update": last_metar_update
    }

def common_view_context(template_name):
    """Helper to load common metrics for the new specialized pages"""
    data = common_view_context_data()
    response = make_response(render_template(template_name, **data))
    response.headers['Cache-Control'] = 'public, max-age=15, s-maxage=60, stale-while-revalidate=120'
    return response

@app.route("/charts")
@login_required
def charts_view():
    return common_view_context("charts.html")

@app.route("/wind_analysis")
@login_required
def wind_analysis_view():
    return common_view_context("wind_analysis.html")

@app.route("/metar", methods=["GET", "POST"])
@login_required
def metar_view():
    # Reuse manual parser logic inside the METAR view
    raw_metar = None
    parsed_qam = None
    validation_results = None
    station = "WARR"

    if request.method == "POST":
        raw_metar = request.form.get("raw_metar", "").strip()
        if raw_metar:
            tokens = raw_metar.split()
            station = "WARR"
            for token in tokens:
                if len(token) == 4 and token.isalpha() and token.isupper():
                    station = token
                    break
            parsed = parse_metar(raw_metar)
            parsed_qam = generate_qam(station, parsed, raw_metar)
            validation_results = validate_metar(raw_metar)

    # We use common_view_context logic but need to inject the manual parser results
    context = common_view_context_data() # I should create this helper to avoid code duplication
    context.update({
        "raw_metar": raw_metar,
        "parsed_qam": parsed_qam,
        "validation_results": validation_results
    })
    response = make_response(render_template("metar.html", **context))
    if request.method == "GET":
        response.headers['Cache-Control'] = 'public, max-age=15, s-maxage=60, stale-while-revalidate=120'
    return response

@app.route("/qam_report", methods=["GET", "POST"])
@login_required
def qam_report_view():
    raw_metar = None
    parsed_qam = None
    validation_results = None
    station = "WARR"

    if request.method == "POST":
        raw_metar = request.form.get("raw_metar", "").strip()
        if raw_metar:
            # Extract ICAO station code (4 uppercase letters) from METAR
            tokens = raw_metar.split()
            station = "WARR"
            for token in tokens:
                if len(token) == 4 and token.isalpha() and token.isupper():
                    station = token
                    break
            parsed = parse_metar(raw_metar)
            parsed_qam = generate_qam(station, parsed, raw_metar)
            validation_results = validate_metar(raw_metar)

    # We use common_view_context logic but need to inject the manual parser results
    context = common_view_context_data()
    context.update({
        "raw_metar": raw_metar,
        "parsed_qam": parsed_qam,
        "validation_results": validation_results
    })
    response = make_response(render_template("qam_report.html", **context))
    if request.method == "GET":
        response.headers['Cache-Control'] = 'public, max-age=15, s-maxage=60, stale-while-revalidate=120'
    return response

@app.route("/weather_analysis")
@login_required
def weather_analysis_view():
    return common_view_context("weather_analysis.html")

# =========================
# DOWNLOAD QAM
# =========================
@app.route("/download_qam")
@login_required
def download_qam():
    station = request.args.get("station")
    qam = request.args.get("qam")
    if not qam:
        return "Tidak ada QAM untuk di-download", 400

    buffer = BytesIO()
    buffer.write(qam.encode())
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name=f"QAM_{station}.txt",
        mimetype="text/plain"
    )

# =========================
# DOWNLOAD CSV HISTORY
# =========================
@app.route("/download_csv")
@login_required
def download_csv():
    if not os.path.exists(CSV_FILE):
        return "CSV belum tersedia", 400

    buffer = BytesIO()
    df = pd.read_csv(CSV_FILE)
    df.to_csv(buffer, index=False)
    buffer.seek(0)

    return send_file(
        buffer,
        as_attachment=True,
        download_name="metar_history.csv",
        mimetype="text/csv"
    )

# =========================
# HISTORY BY DATE RANGE
# =========================
@app.route("/history_by_date", methods=["GET", "POST"])
@login_required
def history_by_date():

    results = None
    station = "WARR"  # Default station
    labels: list = []
    temps: list = []
    pressures: list = []
    winds: list = []
    gusts: list = []
    thunder_flags: list = []
    wind_dirs: list = []
    start_date = ""
    end_date = ""
    utc_wib_labels: list = []

    if request.method == "POST":
        station = request.form.get("icao", "WARR").upper()
        start_date = request.form.get("start_date", "")
        end_date = request.form.get("end_date", "")
        
        print(f"[HISTORY] Station: {station}, Start: {start_date}, End: {end_date}")

        if start_date and end_date:
            # 🔥 DIRECT FETCH: Ambil data langsung dari Google Sheets (Bypass Cache)
            # Menjamin data yang dicari adalah yang paling terbaru di Sheets
            try:
                print(f"[HISTORY] Fetching fresh data from Sheets for {station}...", file=sys.stderr)
                all_records = sheets_handler.get_all_data(bypass_cache=True)
                if not all_records:
                    print("[HISTORY] No data returned from Sheets", file=sys.stderr)
                    df = pd.DataFrame()
                else:
                    df = pd.DataFrame(all_records)
                    print(f"[HISTORY] Loaded {len(df)} rows from Sheets memory", file=sys.stderr)
            except Exception as sheets_err:
                print(f"[HISTORY] Sheets fetch error: {sheets_err}", file=sys.stderr)
                # Fallback to local CSV as a last resort
                df = pd.read_csv(CSV_FILE) if os.path.exists(CSV_FILE) else pd.DataFrame()

            # Process DataFrame
            if not df.empty:
                try:
                    
                    # 1. Cleaning: Drop completely empty rows and handle NaT
                    df = df.dropna(subset=["time", "metar"])
                    df["time"] = pd.to_datetime(df["time"], errors='coerce', format='mixed')
                    df = df.dropna(subset=["time"])
                    
                    # 2. De-duplication: Ensure charts don't show redundant points
                    # Sort by time first to keep the most recent entries if duplicates exist
                    df = df.sort_values("time")
                    df = df.drop_duplicates(subset=["station", "time", "metar"], keep="last")
                    
                    # 3. Timezone Correction (WIB -> UTC)
                    # User inputs WIB (local), DB stores UTC. Shift back 7 hours.
                    start_dt = pd.to_datetime(start_date)
                    end_dt = pd.to_datetime(end_date)
                    
                    # If end date is exactly at midnight (T00:00), the user likely picked a date 
                    # without a time, implying they want the WHOLE day up to 23:59 WIB.
                    if "T00:00" in end_date:
                        end_dt = end_dt.replace(hour=23, minute=59, second=59)
                    
                    # Convert WIB to UTC (-7 hours)
                    start_utc = start_dt - timedelta(hours=7)
                    end_utc = end_dt - timedelta(hours=7)
                    
                    print(f"[HISTORY] Filter (UTC Range): {start_utc} to {end_utc}", file=sys.stderr)
                    
                    # 4. Apply Filter
                    station_clean = station.strip().upper()
                    results = df[
                        (df["station"].str.strip().str.upper() == station_clean) &
                        (df["time"] >= start_utc) &
                        (df["time"] <= end_utc)
                    ]
                    
                    print(f"[HISTORY] Found {len(results)} matching records", file=sys.stderr)
                    
                    # Identify missing 30-minute intervals
                    if results is not None and not results.empty:
                        def get_metar_obs_time(row_time, metar_str):
                            if not metar_str:
                                return None
                            match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', str(metar_str))
                            if not match:
                                return None
                            day = int(match.group(1))
                            hour = int(match.group(2))
                            minute = int(match.group(3))
                            try:
                                # Standard case: construct datetime using current row's year and month
                                obs_dt = datetime(row_time.year, row_time.month, day, hour, minute, 0)
                                diff = (row_time - obs_dt).total_seconds()
                                # Handle month transitions (e.g., if METAR day is 31 but row_time is day 1)
                                if abs(diff) > 15 * 24 * 3600:
                                    if obs_dt > row_time:
                                        # obs_dt is in the past month
                                        month = row_time.month - 1 if row_time.month > 1 else 12
                                        year = row_time.year if row_time.month > 1 else row_time.year - 1
                                        obs_dt = datetime(year, month, day, hour, minute, 0)
                                    else:
                                        # obs_dt is in the next month
                                        month = row_time.month + 1 if row_time.month < 12 else 1
                                        year = row_time.year if row_time.month < 12 else row_time.year + 1
                                        obs_dt = datetime(year, month, day, hour, minute, 0)
                                return obs_dt
                            except:
                                return None

                        def get_scheduled_time(dt):
                            if not dt:
                                return None
                            minute = dt.minute
                            if minute >= 45:
                                minute = 0
                                dt = dt + timedelta(hours=1)
                            elif minute >= 15:
                                minute = 30
                            else:
                                minute = 0
                            return dt.replace(minute=minute, second=0, microsecond=0)

                        existing_slots = set()
                        for _, row in results.iterrows():
                            obs_time = get_metar_obs_time(row["time"], row["metar"])
                            if obs_time:
                                sched_time = get_scheduled_time(obs_time)
                                existing_slots.add(sched_time)
                            else:
                                existing_slots.add(get_scheduled_time(row["time"]))

                        existing_slots = {s for s in existing_slots if s is not None}
                        
                        if existing_slots:
                            start_slot = min(existing_slots)
                            end_slot = max(existing_slots)
                            
                            missing_slots = []
                            curr_slot = start_slot
                            while curr_slot <= end_slot:
                                if curr_slot not in existing_slots:
                                    missing_slots.append(curr_slot)
                                curr_slot += timedelta(minutes=30)
                            
                            results = results.copy()
                            results["is_missing"] = False
                            
                            if missing_slots:
                                missing_rows = []
                                for slot in missing_slots:
                                    missing_rows.append({
                                        "station": station_clean,
                                        "time": slot,
                                        "metar": "DATA METAR TIDAK MASUK",
                                        "validation_json": json.dumps({"errors": [], "warnings": [], "status": "danger"}),
                                        "is_missing": True
                                    })
                                df_missing = pd.DataFrame(missing_rows)
                                df_missing["time"] = pd.to_datetime(df_missing["time"])
                                results = pd.concat([results, df_missing], ignore_index=True)

                    # Extract chart data if results exist
                    if results is not None and not results.empty:
                        results = results.sort_values("time")  # Sort by time for chart
                        
                        for _, row in results.iterrows():
                            if bool(row.get("is_missing", False)):
                                continue
                            
                            metar = str(row["metar"]) if pd.notna(row["metar"]) else ""
                            
                            # Format time for label
                            labels.append(pd.to_datetime(row["time"]).strftime("%d/%m/%y %H:%M UTC"))
                            
                            # Extract temperature (format: XX/XX)
                            temp_match = re.search(r'(\d{2})/(\d{2})', metar)
                            temps.append(int(temp_match.group(1)) if temp_match else None)
                            
                            # Extract pressure (QNH format: QXXXX)
                            qnh_match = re.search(r'Q(\d{4})', metar)
                            pressures.append(int(qnh_match.group(1)) if qnh_match else None)
                            
                            # Extract wind speed, gust, and direction
                            wind_match = re.search(r'(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT', metar)
                            
                            if wind_match:
                                wind_dir = wind_match.group(1)
                                wind_dirs.append(wind_dir if wind_dir != "VRB" else None)
                                winds.append(int(wind_match.group(2)))
                                gusts.append(int(wind_match.group(4)) if wind_match.group(4) else None)
                            else:
                                wind_dirs.append(None)
                                winds.append(None)
                                gusts.append(None)
                            
                            # Detect thunderstorm
                            thunder_codes = ["TS", "TSRA", "VCTS", "+TS", "TSGR", "-TS", "+TSRA", "-TSRA"]
                            thunder_flags.append(any(code in metar for code in thunder_codes))
                            
                            # Add full dual time label for Wind Rose
                            wib_time_row = row["time"] + timedelta(hours=7)
                            utc_wib_labels.append(f"{row['time'].strftime('%Y-%m-%d %H:%M UTC')} | {wib_time_row.strftime('%Y-%m-%d %H:%M WIB')}")
                        
                        # Pre-calculate validation for table display (allows for status badges/row colors)
                        if results is not None and not results.empty:
                            results = results.copy() # Avoid SettingWithCopyWarning
                            results["validation_json"] = results.apply(
                                lambda r: r["validation_json"] if r["is_missing"] else json.dumps(validate_metar(str(r["metar"]))),
                                axis=1
                            )
                        
                        print(f"[HISTORY] Chart data extracted: {len(labels)} points")
                        
                        # Reverse results for table display (newest first)
                        results = results.iloc[::-1]
                except Exception as e:
                    print(f"[HISTORY] Error processing CSV: {e}", file=sys.stderr)
                    results = None
            else:
                print("[HISTORY] CSV file does not exist")

    return render_template(
        "history_by_date.html",
        results=results,
        station=station,
        labels=labels,
        temps=temps,
        pressures=pressures,
        winds=winds,
        gusts=gusts,
        thunder_flags=thunder_flags,
        wind_dirs=wind_dirs,
        start_date=start_date,
        end_date=end_date,
        utc_wib_labels=utc_wib_labels,
        auto_fetch=auto_fetch,
        last_metar_update=last_metar_update
    )


# ============ SYSTEM CONTROL ENDPOINTS ============

@app.route("/ping")
def ping():
    """Keep-alive endpoint untuk frontend"""
    return jsonify({"status": "alive", "timestamp": datetime.utcnow().isoformat()})

@app.route("/health")
def health():
    """Health check untuk monitoring eksternal"""
    global last_metar_update
    
    # Fallback: if last_metar_update is None, try to get from CSV
    if last_metar_update is None and os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            if not df.empty:
                last_metar_update = pd.to_datetime(df.iloc[-1]["time"]).isoformat() + "Z"
        except:
            pass

    return jsonify({
        "status": "ok",
        "server": "online",
        "auto_fetch": auto_fetch,
        "last_update": last_metar_update
    })

@app.route("/api/toggle_fetch", methods=["POST"])
@admin_only_api
def toggle_fetch():
    """System Control ON/OFF"""
    global auto_fetch
    auto_fetch = not auto_fetch

    return jsonify({
        "auto_fetch": auto_fetch,
        "last_update": last_metar_update,
        "message": f"Auto fetch {'ENABLED' if auto_fetch else 'DISABLED'}"
    })

@app.route("/api/set_fetch", methods=["POST"])
@admin_only_api
def set_fetch():
    """Explicitly set system status from client"""
    global auto_fetch
    data = request.json
    if data and "enabled" in data:
        auto_fetch = bool(data["enabled"])
        print(f"[SYSTEM] Fetch status set to: {auto_fetch} (client sync)", file=sys.stderr)
    
    return jsonify({
        "auto_fetch": auto_fetch,
        "last_update": last_metar_update
    })

# =========================
@app.route('/favicon.ico')
def favicon():
    return '', 204

def _background_update_metar(station):
    """Background update tanpa blocking response"""
    try:
        # Pemicu dari browser/latest-data, gunakan is_cron=False
        update_metar_data_and_sync(station, is_cron=False)
    except Exception as e:
        print(f"[BG UPDATE] Error: {e}", file=sys.stderr)

# =========================
# POLLING ENDPOINT (replaces WebSocket)
# =========================
@app.route("/api/latest-data")
@admin_only_api
def latest_data():
    """Endpoint for frontend polling — optimized for VERCEL serverless consistency"""
    global last_metar_update, auto_fetch, latest_metar_data, _last_fetch_time, _cached_metar
    
    now_ts = time.time()
    now_dt = datetime.utcnow()
    
    # 1. 🔥 FAST CACHE HIT (Local Memory)
    # Jika data di memori masih sangat baru (< 15 detik), langsung kirim balik
    # Ini sangat cepat dan menghemat kuota Google Sheets API.
    if _cached_metar and (now_ts - _last_fetch_time < 15):
        data = _cached_metar.copy()
        data.update({
            "auto_fetch": auto_fetch,
            "cached": True,
            "cache_age": int(now_ts - _last_fetch_time)
        })
        response = make_response(jsonify(data))
        response.headers['Cache-Control'] = 'public, max-age=5, s-maxage=5'
        return response
    
    # 2. 🔥 SYNCHRONOUS SYNC (Vercel Compatibility)
    # Jika data sudah stale (> 20 detik) atau cache kosong, lakukan penarikan data baru secara sinkron.
    # PENTING: Jangan gunakan Threading di Vercel karena akan dimatikan sebelum selesai.
    if auto_fetch:
        should_sync = False
        
        if not last_metar_update or not latest_metar_data:
            should_sync = True
        else:
            try:
                # Cek kesegaran timestamp data terakhir
                # last_metar_update biasanya ISO format (UTC)
                last_dt = pd.to_datetime(last_metar_update.replace("Z", ""), format='mixed')
                # Jika data sudah lebih dari 20 detik berlalu sejak update terakhir
                if (now_dt - last_dt).total_seconds() > 20: 
                    should_sync = True
            except:
                should_sync = True
        
        if should_sync:
            print(f"[POLL] Data stale or missing, triggering synchronous sync...", file=sys.stderr)
            # Jalankan sync secara sinkron agar Vercel tidak mematikan process di tengah jalan
            update_metar_data_and_sync("WARR", is_cron=False)

    # 3. 🔥 FALLBACK: Populasikan data dari CSV jika cache memori hilang (serverless restart)
    if not latest_metar_data and os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            if not df.empty:
                last_row = df.iloc[-1]
                metar = str(last_row["metar"])
                parsed = parse_metar(metar)
                station = str(last_row["station"])
                qam = generate_qam(station, parsed, metar)
                narrative = generate_metar_narrative(parsed, metar)
                last_metar_update = pd.to_datetime(last_row["time"]).isoformat() + "Z"
                
                latest_metar_data = {
                    "status": "cached_fallback",
                    "raw": metar,
                    "qam": qam,
                    "narrative": narrative,
                    "last_update": last_metar_update,
                    "auto_fetch": auto_fetch,
                    "wind_dir": parsed.get("wind_dir"),
                    "wind_speed": parsed.get("wind_speed_kt"),
                    "temp": parsed.get("temperature_c"),
                    "qnh": parsed.get("pressure_hpa"),
                    "metar_status": parsed.get("status", "normal")
                }
        except Exception as e:
            print(f"[POLL] Fallback build failed: {e}", file=sys.stderr)

    # 4. PREPARE RESPONSE
    data = (latest_metar_data or {}).copy()
    data.update({
        "auto_fetch": auto_fetch,
        "last_update": last_metar_update,
        "server": "online",
        "sync_time": now_dt.isoformat() + "Z"
    })
    
    # Update memory cache
    _cached_metar = data
    _last_fetch_time = now_ts
    
    response = make_response(jsonify(data))
    # s-maxage=0 memberitahu Edge (Vercel) untuk TIDAK melakukan caching pada level CDN.
    # Dashboard kita sudah punya polling interval sendiri, lebih aman fetch langsung ke server
    # agar tidak ada data jam 15.00 yang "nyangkut" di CDN.
    response.headers['Cache-Control'] = 'private, no-cache, no-store, must-revalidate, s-maxage=0'
    return response

# =========================
# CRON JOB ENDPOINT (For 24/7 background sync)
# =========================
@app.route("/api/cron/sync")
def cron_sync():
    """
    24/7 Data Collection Endpoint
    Didesign untuk reliability meski dipanggil multiple sources
    """
    global _last_collection, _cached_metar, latest_metar_data
    
    # Auth check
    is_vercel = request.headers.get('x-vercel-cron') == '1'
    token = request.args.get('auth')
    expected = os.environ.get('CRON_TOKEN', 'bmkg-juanda-secret-123')
    
    if not is_vercel and token != expected:
        return jsonify({"error": "Unauthorized"}), 401
    
    station = "WARR"
    now = datetime.utcnow()
    last = _last_collection.get(station, {})
    
    # 🔥 ANTI-DUPLICATE: Jangan save jika sudah ada data < 2 menit yang lalu
    if last.get('time'):
        time_diff = (now - last['time']).total_seconds()
        cached_raw = _cached_metar.get('raw') if isinstance(_cached_metar, dict) else None
        if time_diff < 120 and last.get('metar') == cached_raw:
            return jsonify({
                "status": "skipped",
                "reason": "Duplicate prevention",
                "last_collection": last['time'].isoformat(),
                "next_eligible": (last['time'] + timedelta(minutes=2)).isoformat()
            }), 200
    
    # Execute collection
    try:
        # Pemicu dari Cron (GHA/Vercel Cron)
        success = update_metar_data_and_sync(station, is_cron=True)
        
        if success:
            _last_collection[station] = {
                'time': now,
                'metar': latest_metar_data.get('raw') if isinstance(latest_metar_data, dict) else None,
                'attempts': 0
            }
            
            # Safely get preview
            raw_preview = ""
            if isinstance(latest_metar_data, dict) and latest_metar_data.get('raw'):
                raw_preview = latest_metar_data.get('raw')[:50] + "..."
                
            return jsonify({
                "status": "success",
                "collected_at": now.isoformat(),
                "metar_preview": raw_preview,
                "stored_to": "Google Sheets"
            }), 200
        else:
            _last_collection[station]['attempts'] = last.get('attempts', 0) + 1
            return jsonify({
                "status": "failed",
                "attempt": _last_collection[station]['attempts'],
                "error": "Sync returned false"
            }), 500
            
    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

# =========================
# HELPER: Normalize METAR for accurate comparison
# =========================
def normalize_metar(metar: str) -> str:
    """
    Normalisasi string METAR untuk comparison yang akurat.
    Menghilangkan perbedaan formatting yang tidak signifikan.
    """
    if not metar:
        return ""
    # Remove trailing = (marker akhir METAR)
    metar = metar.replace("=", "")
    # Normalize whitespace (multiple spaces -> single space, strip ends)
    metar = " ".join(metar.split())
    # Uppercase untuk consistency
    metar = metar.upper().strip()
    return metar
# =========================
# CENTRALIZED METAR UPDATE DATA & SYNC (ANTI-DUPLIKASI)
# =========================
def update_metar_data_and_sync(station="WARR", is_cron=False):
    """
    Central function to fetch METAR, save locally, sync to Google Sheets, 
    and update global cache. 
    
    LOGIKA ANTI-DUPLIKASI:
    - Hanya menyimpan jika METAR benar-benar berbeda dari data yang sudah ada
      di Google Sheets dalam 10 record terakhir.
    - Mengecek isi string METAR dan uniknya kode waktu (DDHHMMZ).
    """
    global last_metar_update, latest_metar_data, auto_fetch
    
    # 🔥 UNIQUE REQUEST ID (For Traceability)
    req_id = "".join(random.choices("0123456789ABCDEF", k=4))
    
    # 🔥 STAGGERED DELAY (Anti-Collision)
    # Cron: minimal delay | Browser: short delay to let Cron win if simultaneous
    # PENTING: Total execution HARUS < 10s agar tidak timeout di Vercel
    delay = random.uniform(0.1, 0.5) if is_cron else random.uniform(1.0, 2.0)
    print(f"[SYNC][{req_id}] Initial delay: {delay:.2f}s (is_cron={is_cron})", file=sys.stderr)
    time.sleep(delay)
    
    try:
        print(f"[SYNC][{req_id}] Starting update for {station}...", file=sys.stderr)
        metar = get_metar(station)
        
        if not metar:
            print(f"[SYNC][{req_id}] [ERROR] No METAR received", file=sys.stderr)
            return False
        
        # Normalisasi untuk comparison
        metar_clean = normalize_metar(metar)
        
        # Ekstrak Time Group (misal: 191430Z) untuk key unik
        # Handle case METAR AMD/COR yang mungkin punya spasi berbeda
        time_match = re.search(r'\b(\d{6}Z)\b', metar)
        metar_time_key = time_match.group(1) if time_match else None
        
        print(f"[SYNC][{req_id}] Raw METAR: {str(metar)[:80]}...", file=sys.stderr)
        if metar_time_key: print(f"[SYNC][{req_id}] Time Key: {metar_time_key}", file=sys.stderr)

        # =====================================================
        # PRE-WRITE CHECK: Quick dedup dari Sheets context
        # Ini adalah filter cepat. Jika lolos, data akan disimpan,
        # lalu post-write dedup di sheets_handler.save_metar() 
        # akan membersihkan duplikat yang mungkin lolos karena race condition.
        # =====================================================
        print(f"[SYNC][{req_id}] Checking Sheets for duplicates...", file=sys.stderr)
        recent_data = sheets_handler.get_recent_data(limit=15, bypass_cache=True)
        
        should_save = True
        skip_reason = ""
        
        if recent_data:
            # Check 1: Identical METAR string
            past_metars_clean = [normalize_metar(str(row.get('metar', ''))) for row in recent_data]
            if metar_clean in past_metars_clean:
                should_save = False
                skip_reason = "METAR identik ditemukan di riwayat Sheets"
            
            # Check 2: Same Time Key (DDHHMMZ)
            if should_save and metar_time_key:
                for row in recent_data:
                    past_match = re.search(r'\b(\d{6}Z)\b', str(row.get('metar', '')))
                    if past_match and past_match.group(1) == metar_time_key:
                        should_save = False
                        skip_reason = f"Time key {metar_time_key} sudah ada di Sheets"
                        break

        # =====================================================
        # EXECUTE SAVE OR SKIP
        # =====================================================
        if not should_save:
            print(f"[SYNC][{req_id}] [SKIP] FINAL SKIP: {skip_reason}", file=sys.stderr)
            
            # 🔥 CHRONOLOGICAL CONSISTENCY: 
            # Jika skip karena duplikat, cari timestamp asli dari data yang sudah ada
            # agar last_metar_update tidak "lompat" ke waktu sekarang (mencegah stale warning di browser)
            existing_time_str = None
            if recent_data:
                for row in recent_data:
                    # Cek apakah metar row ini cocok dengan yang baru kita ambil
                    if normalize_metar(str(row.get('metar', ''))) == metar_clean:
                        existing_time_str = row.get('time')
                        break
            
            if existing_time_str:
                # Gunakan waktu asli dari DB
                try:
                    last_metar_update = pd.to_datetime(existing_time_str).isoformat() + "Z"
                except:
                    last_metar_update = datetime.utcnow().isoformat() + "Z"
            else:
                # Fallback ke waktu sekarang jika tidak ketemu di context terbatas
                last_metar_update = datetime.utcnow().isoformat() + "Z"
            
            # Update cache even if skip saving
            parsed = parse_metar(metar)
            qam = generate_qam(station, parsed, metar)
            narrative = generate_metar_narrative(parsed, metar)
            
            latest_metar_data = {
                "status": "duplicate_skipped",
                "qam": qam,
                "raw": metar,
                "narrative": narrative,
                "time": datetime.utcnow().strftime("%d-%m-%Y %H:%M:%S"),
                "wind_dir": parsed.get("wind_dir"),
                "wind_speed": parsed.get("wind_speed_kt"),
                "wind_gust": parsed.get("wind_gust_kt"),
                "temp": parsed.get("temperature_c"),
                "dewpoint": parsed.get("dewpoint_c"),
                "visibility_m": parsed.get("visibility_m"),
                "cloud": parsed.get("cloud"),
                "qnh": parsed.get("pressure_hpa"),
                "weather": parsed.get("weather"),
                "metar_status": parsed.get("status", "normal"),
                "report_type": parsed.get("report_type", "METAR"),
                "auto_fetch": auto_fetch,
                "last_update": last_metar_update
            }
            return True  # Return True karena bukan error
        
        # =====================================================
        # SAVE NEW DATA
        # =====================================================
        print(f"[SYNC][{req_id}] [NEW] NEW/REFRESHED METAR detected! Saving...", file=sys.stderr)
        
        new_row = {
            "station": station,
            "time": datetime.utcnow(),
            "metar": metar  # Simpan original, tidak normalized
        }
        
        # 🔥 SERVER-SIDE WIND LOGGING: Perekaman otomatis tanpa dashboard
        print(f"[SYNC][{req_id}] Processing server-side wind log...", file=sys.stderr)
        process_server_wind_log(metar)
        
        # Format waktu tanpa milliseconds untuk consistency
        new_row_df = pd.DataFrame([new_row])
        new_row_df["time"] = pd.to_datetime(new_row_df["time"]).dt.strftime("%Y-%m-%d %H:%M:%S")
        time_str = new_row_df["time"].iloc[0]
        
        # 🔥 FIX: Bangun df dari recent_data (atau kosong) untuk CSV backup
        if recent_data:
            df = pd.DataFrame(recent_data)
        else:
            df = pd.DataFrame(columns=["station", "time", "metar"])
        df = pd.concat([df, new_row_df], ignore_index=True)
        try:
            df.to_csv(CSV_FILE, index=False)
        except Exception as csv_err:
            print(f"[SYNC][{req_id}] CSV write warning: {csv_err}", file=sys.stderr)
        
        # 🔥 SYNC TO GOOGLE SHEETS
        try:
            print(f"[SYNC][{req_id}] Pushing to Google Sheets...", file=sys.stderr)
            sheets_handler.save_metar(station, time_str, metar)
            print(f"[SYNC][{req_id}] [SUCCESS] Google Sheets synced", file=sys.stderr)
        except Exception as e:
            print(f"[SYNC][{req_id}] [ERROR] Google Sheets Error: {e}", file=sys.stderr)
            # Tetap lanjut meski Sheets gagal, data sudah di CSV
        
        # Update cache dengan data baru
        parsed = parse_metar(metar)
        qam = generate_qam(station, parsed, metar)
        narrative = generate_metar_narrative(parsed, metar)
        
        # Simpan ke wind history
        store_wind(parsed, station)
        
        latest_metar_data = {
            "status": "new",
            "qam": qam,
            "raw": metar,
            "narrative": narrative,
            "time": datetime.utcnow().strftime("%d-%m-%Y %H:%M:%S"),
            "wind_dir": parsed.get("wind_dir"),
            "wind_speed": parsed.get("wind_speed_kt"),
            "wind_gust": parsed.get("wind_gust_kt"),
            "temp": parsed.get("temperature_c"),
            "dewpoint": parsed.get("dewpoint_c"),
            "visibility_m": parsed.get("visibility_m"),
            "cloud": parsed.get("cloud"),
            "qnh": parsed.get("pressure_hpa"),
            "weather": parsed.get("weather"),
            "metar_status": parsed.get("status", "normal"),
            "report_type": parsed.get("report_type", "METAR"),
            "auto_fetch": auto_fetch,
            "last_update": datetime.utcnow().isoformat() + "Z"
        }
        last_metar_update = latest_metar_data["last_update"]
        
        print(f"[SYNC] [SUCCESS] Data saved successfully at {last_metar_update}", file=sys.stderr)
        return True
        
    except Exception as e:
        print(f"[SYNC] [ERROR] Critical Update Error: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return False

# =========================

def background_metar_loop():
    """⚠️ DEPRECATED: Do not use in Vercel environment"""
    print("[SYSTEM] Background loop disabled - use Vercel Cron instead", file=sys.stderr)
    return  # Exit immediately

@app.route("/download_history", methods=["POST"])
@login_required
def download_history():
    station = request.form["icao"].upper()
    start_date = request.form["start_date"]
    end_date = request.form["end_date"]

    # Use unified fetcher to support Vercel/Sheets
    df = fetch_history_from_source()
    if df.empty:
        return "No data available in history", 404
        
    df["time"] = pd.to_datetime(df["time"])

    start = pd.to_datetime(start_date)
    end = pd.to_datetime(end_date)

    results = df[
        (df["station"] == station) &
        (df["time"] >= start) &
        (df["time"] <= end)
    ]

    if results.empty:
        return f"No records found for {station} between {start_date} and {end_date}", 404

    output = io.StringIO()
    results.to_csv(output, index=False)

    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"{station}_history.csv"
    )

# =========================
# METAR VALIDATOR
# =========================
def validate_metar(metar: str) -> list[str]:
    """
    Comprehensive METAR Validator for 10 groups
    Returns a list of error strings or ["✅ METAR Valid"]
    """
    if not metar:
        return ["❌ Data METAR kosong"]

    errors: list[str] = []
    
    # Pre-clean: remove = and handle multiple spaces
    clean_metar = metar.replace("=", "").strip()
    tokens = clean_metar.split()

    if len(tokens) < 5:
        return ["❌ Format METAR terlalu pendek atau tidak lengkap"]

    # Skip first token if it's "METAR" or "SPECI"
    idx = 0
    if tokens[idx] in ["METAR", "SPECI"]:
        idx += 1

    # 1. ICAO Station (4 letters)
    if idx < len(tokens):
        if not re.match(r'^[A-Z]{4}$', tokens[idx]):
            errors.append(f"❌ ICAO station salah: {tokens[idx]} (harus 4 huruf Kapital)")
        idx += 1

    # 2. Time Group (6 digits + Z)
    if idx < len(tokens):
        if not re.match(r'^\d{6}Z$', tokens[idx]):
            errors.append(f"❌ Format waktu salah: {tokens[idx]} (harus 6 digit + Z)")
        idx += 1

    # 3. Wind Group
    if idx < len(tokens):
        # Supports: 05006KT, 18012G20KT, VRB03KT, 00000KT
        wind_match = re.match(r'^(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT$', tokens[idx])
        if wind_match:
            dir_part = wind_match.group(1)
            speed_part = int(wind_match.group(2))
            gust_part = int(wind_match.group(4)) if wind_match.group(4) else None
            
            # 🔥 RULE 1: VRB Wind (Speed 00-02KT)
            if dir_part == "VRB" and speed_part > 2:
                errors.append(f"❌ Angin VRB harus 00-02KT (Terbaca: {speed_part}KT)")
            
            # 🔥 RULE 2: Gust Difference (>= 10KT)
            if gust_part is not None:
                if (gust_part - speed_part) < 10:
                    errors.append(f"❌ Selisih Gust ({gust_part}KT) harus >= 10KT dari angin utama ({speed_part}KT)")
            
            idx += 1  # Valid wind format, advance
        elif tokens[idx].endswith('KT'):
            errors.append(f"❌ Format angin salah: {tokens[idx]}")
            idx += 1
        else:
            errors.append("❌ Angin tidak ditemukan atau format salah")

    # From here on, groups can be more dynamic.
    remaining_tokens = tokens[idx:] if idx < len(tokens) else []
    
    # Extract data for cross-validation
    vis_val = None
    for t in remaining_tokens:
        if re.match(r'^\d{4}$', t):
            vis_val = int(t)
            break
        elif t == "CAVOK":
            vis_val = 9999
            break

    # 4. Visibility Group
    if vis_val is None:
        errors.append("❌ Cek Visibility (harus 4 digit atau CAVOK)")

    # 5. Weather Group & 6. Cloud Group
    cloud_prefixes = ["FEW", "SCT", "BKN", "OVC", "SKC", "NSC", "NCD", "VV"]
    cloud_found = False
    weather_phenomena = []
    has_ts = False
    has_cb = False
    
    for t in remaining_tokens:
        # 🔥 Detect TS/VCTS for cross-validation
        if ("TS" in t or "VCTS" in t) and not t.startswith("RE"):
            has_ts = True

        # Track weather for visibility rule
        if t in ["HZ", "BR", "FG", "RA", "TS", "DZ", "SN"]:
             weather_phenomena.append(t)
             
        if any(t.startswith(p) for p in cloud_prefixes):
            cloud_found = True
            if "CB" in t:
                has_cb = True
            if t in ["SKC", "NSC", "NCD"]: continue
            
            height_part = re.search(r'\d{3}', t)
            if not height_part:
                errors.append(f"❌ Tinggi awan salah: {t} (harus 3 digit)")
            
            prefix = t[:3]
            if prefix in ["FEW", "SCT", "BKN", "OVC"]:
                height = t[3:6]
                if not height.isdigit() or len(height) != 3:
                     errors.append(f"❌ Format kelompok awan salah: {t} (tinggi harus 3 digit)")

    # 🔥 NEW RULE: Missing Cloud Info
    has_cavok = any(t == "CAVOK" for t in tokens)
    if not cloud_found and not has_cavok:
        errors.append("❌ metar tidak valid dan awan tidak ditemukan")
    
    # 🔥 RULE 3: Visibility with HZ or BR (Max 5000m)
    if any(code in weather_phenomena for code in ["HZ", "BR"]):
        if vis_val is not None and vis_val > 5000:
            codes = [c for c in weather_phenomena if c in ["HZ", "BR"]]
            errors.append(f"❌ Visibility {', '.join(codes)} harus <= 5000m (Terbaca: {vis_val}m)")

    # 🔥 RULE 4: TS/VCTS requires CB
    if has_ts and not has_cb:
        errors.append("❌ METAR mengandung TS/VCTS tapi tidak ada indikator awan CB")

    # 7. Temperature/Dewpoint (M?dd/M?dd)
    temp_pattern = r'^M?\d{2}/M?\d{2}$'
    if not any(re.match(temp_pattern, t) for t in remaining_tokens):
        has_cavok = any(t == "CAVOK" for t in remaining_tokens)
        if not has_cavok or not any("/" in t for t in remaining_tokens):
            errors.append("❌ Format suhu TT/TdTd salah (contoh: 31/24)")

    # 8. Pressure Group (Q + 4 digits)
    if not any(re.match(r'^Q\d{4}$', t) for t in remaining_tokens):
        errors.append("❌ Tekanan (QNH) salah (contoh: Q1010)")

    # 9. Trend Group (Optional check)
    trend_keywords = ["NOSIG", "TEMPO", "BECMG"]
    # No hard error if missing, but can check format if present

    if len(errors) == 0:
        return ["✅ METAR Valid"]

    return errors

@app.route("/api/validate", methods=["POST"])
@admin_only_api
def api_validate():
    data = request.get_json()
    if not data or "metar" not in data:
        return jsonify({"results": ["❌ Input tidak ditemukan"]}), 400
    
    metar = data["metar"].strip()
    results = validate_metar(metar)
    return jsonify({"results": results})

# =========================
# MANUAL METAR PARSER
# =========================
@app.route("/manual_parser", methods=["GET", "POST"])
@login_required
def manual_parser():

    station = "WARR"
    raw_metar = None
    parsed_qam = None
    validation_results = None

    if request.method == "POST":
        raw_metar = request.form["raw_metar"].strip()
        # Extract ICAO station code (4 uppercase letters) from METAR
        tokens = raw_metar.split()
        station = "WARR"
        for token in tokens:
            if len(token) == 4 and token.isalpha() and token.isupper():
                station = token
                break
        parsed = parse_metar(raw_metar)
        parsed_qam = generate_qam(station, parsed, raw_metar)
        validation_results = validate_metar(raw_metar)

    return render_template(
        "manual_parser.html",
        station=station,
        raw_metar=raw_metar,
        parsed_qam=parsed_qam,
        validation_results=validation_results,
        auto_fetch=auto_fetch,
        last_metar_update=last_metar_update
    )

# ============================================
# DAILY METAR RECORD MANAGER - BACKEND
# ============================================
def get_missing_slots_helper(df, station, start_limit=None, end_limit=None):
    def get_metar_obs_time(row_time, metar_str):
        if not metar_str:
            return None
        match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', str(metar_str))
        if not match:
            return None
        day = int(match.group(1))
        hour = int(match.group(2))
        minute = int(match.group(3))
        try:
            obs_dt = datetime(row_time.year, row_time.month, day, hour, minute, 0)
            diff = (row_time - obs_dt).total_seconds()
            if abs(diff) > 15 * 24 * 3600:
                if obs_dt > row_time:
                    month = row_time.month - 1 if row_time.month > 1 else 12
                    year = row_time.year if row_time.month > 1 else row_time.year - 1
                    obs_dt = datetime(year, month, day, hour, minute, 0)
                else:
                    month = row_time.month + 1 if row_time.month < 12 else 1
                    year = row_time.year if row_time.month < 12 else row_time.year + 1
                    obs_dt = datetime(year, month, day, hour, minute, 0)
            return obs_dt
        except:
            return None

    def get_scheduled_time(dt):
        if not dt:
            return None
        minute = dt.minute
        if minute >= 45:
            minute = 0
            dt = dt + timedelta(hours=1)
        elif minute >= 15:
            minute = 30
        else:
            minute = 0
        return dt.replace(minute=minute, second=0, microsecond=0)

    existing_slots = set()
    if df is not None and not df.empty:
        for _, row in df.iterrows():
            if "is_missing" in row and row["is_missing"]:
                continue
            obs_time = get_metar_obs_time(row["time"], row["metar"])
            if obs_time:
                existing_slots.add(get_scheduled_time(obs_time))
            else:
                existing_slots.add(get_scheduled_time(row["time"]))
            
    existing_slots = {s for s in existing_slots if s is not None}
    
    missing_slots = []
    # If start_limit is set, or if we have existing slots to find min/max from
    if start_limit or existing_slots:
        start_slot = start_limit if start_limit else min(existing_slots)
        end_slot = end_limit if end_limit else (max(existing_slots) if existing_slots else start_limit)
        
        # Round start/end to scheduled time
        start_slot = get_scheduled_time(start_slot)
        end_slot = get_scheduled_time(end_slot)
        
        curr_slot = start_slot
        while curr_slot <= end_slot:
            if curr_slot not in existing_slots:
                missing_slots.append(curr_slot)
            curr_slot += timedelta(minutes=30)
            
    return missing_slots

@app.route("/api/records/today")
@admin_only_api
def get_today_records():
    """Mengambil data METAR khusus hari ini (Reset otomatis 00:00 UTC)"""
    now_utc = datetime.utcnow()
    today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    
    try:
        # Mengambil data langsung dari Google Sheets
        all_records = sheets_handler.get_all_data()
        if not all_records:
            return jsonify({"records": [], "date": format_indonesian_date(now_utc)})
        
        df = pd.DataFrame(all_records)
        df["time"] = pd.to_datetime(df["time"], errors='coerce')
        
        # Filter: Hanya data dari 00:00 UTC hari ini
        today_df = df[df["time"] >= today_start].copy()
        today_df["is_missing"] = False
        
        # Cari slot data yang hilang/missing
        end_limit = now_utc - timedelta(minutes=15)
        # Exclude 00:00 UTC by starting from 00:30 UTC
        missing_slots = get_missing_slots_helper(today_df, "WARR", start_limit=today_start + timedelta(minutes=30), end_limit=end_limit)
            
        if missing_slots:
            missing_rows = []
            for slot in missing_slots:
                missing_rows.append({
                    "station": "WARR",
                    "time": slot,
                    "metar": "DATA METAR TIDAK MASUK",
                    "is_missing": True
                })
            df_missing = pd.DataFrame(missing_rows)
            df_missing["time"] = pd.to_datetime(df_missing["time"])
            if today_df.empty:
                today_df = df_missing
            else:
                today_df = pd.concat([today_df, df_missing], ignore_index=True)
            
        today_df = today_df.sort_values("time", ascending=False)
        
        records = []
        for _, row in today_df.iterrows():
            metar = str(row["metar"])
            is_missing = bool(row.get("is_missing", False))
            
            if is_missing:
                record_status = "missing"
                validation_results = []
            else:
                parsed = parse_metar(metar)
                validation_results = validate_metar(metar)
                
                record_status = "normal"
                is_valid = validation_results and validation_results[0].startswith('✅')
                
                if ',' in metar or not is_valid:
                    record_status = "invalid"
                elif ' COR ' in metar or 'CCA' in metar:
                    record_status = "cor"
                elif ' AMD ' in metar:
                    record_status = "amd"
                elif 'SPECI' in metar:
                    record_status = "speci"
                else:
                    try:
                        minute = int(parsed.get("minute", "-1"))
                        if minute != 0 and minute != 30 and minute != -1:
                            record_status = "speci"
                    except:
                        pass
                        
            records.append({
                "time": pd.to_datetime(row["time"]).strftime("%Y-%m-%d %H:%M UTC"),
                "station": row["station"],
                "metar": metar,
                "record_status": record_status,
                "validation_results": validation_results,
                "is_missing": is_missing
            })
            
        # Chart Data extraction (Ascending order)
        chart_df = today_df.sort_values("time", ascending=True)
        chart_labels = []
        chart_temps = []
        chart_pressures = []
        chart_winds = []
        chart_gusts = []
        
        for _, row in chart_df.iterrows():
            is_missing = bool(row.get("is_missing", False))
            if is_missing:
                continue
            chart_labels.append(row["time"].strftime("%H:%M"))
            p = parse_metar(str(row["metar"]))
            chart_temps.append(float(p.get("temperature_c")) if p.get("temperature_c") else None)
            chart_pressures.append(float(p.get("pressure_hpa")) if p.get("pressure_hpa") else None)
            chart_winds.append(float(p.get("wind_speed_kt")) if p.get("wind_speed_kt") else None)
            chart_gusts.append(float(p.get("wind_gust_kt")) if p.get("wind_gust_kt") else None)

        return jsonify({
            "date": format_indonesian_date(now_utc),
            "records": records,
            "count": len(records),
            "chart_data": {
                "labels": chart_labels,
                "temps": chart_temps,
                "pressures": chart_pressures,
                "winds": chart_winds,
                "gusts": chart_gusts
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/records/yesterday")
@admin_only_api
def get_yesterday_records():
    """Mengambil data METAR lengkap dari hari kemarin"""
    now_utc = datetime.utcnow()
    yesterday = now_utc - timedelta(days=1)
    y_start = yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
    y_end = yesterday.replace(hour=23, minute=59, second=59)
    
    try:
        # 🔥 BYPASS CACHE: Menjamin data "Kemarin" selalu sinkron dengan Sheets
        all_records = sheets_handler.get_all_data(bypass_cache=True)
        df = pd.DataFrame(all_records)
        
        records = []
        yesterday_df = pd.DataFrame()
        
        if not df.empty and "time" in df.columns:
            df["time"] = pd.to_datetime(df["time"], errors='coerce')
            yesterday_df = df[(df["time"] >= y_start) & (df["time"] <= y_end)].copy()
        else:
            yesterday_df = pd.DataFrame(columns=["station", "time", "metar"])
            
        yesterday_df["is_missing"] = False
        
        # Cari slot data yang hilang/missing
        # Use 23:30 as end limit for missing slot detection (last scheduled METAR slot of the day)
        missing_end = yesterday.replace(hour=23, minute=30, second=0, microsecond=0)
        missing_slots = get_missing_slots_helper(yesterday_df, "WARR", start_limit=y_start, end_limit=missing_end)
        
        if missing_slots:
            missing_rows = []
            for slot in missing_slots:
                missing_rows.append({
                    "station": "WARR",
                    "time": slot,
                    "metar": "DATA METAR TIDAK MASUK",
                    "is_missing": True
                })
            df_missing = pd.DataFrame(missing_rows)
            df_missing["time"] = pd.to_datetime(df_missing["time"])
            if yesterday_df.empty:
                yesterday_df = df_missing
            else:
                yesterday_df = pd.concat([yesterday_df, df_missing], ignore_index=True)
            
        yesterday_df = yesterday_df.sort_values("time", ascending=False)
        
        for _, row in yesterday_df.iterrows():
            metar = str(row["metar"])
            is_missing = bool(row.get("is_missing", False))
            
            if is_missing:
                record_status = "missing"
                validation_results = []
            else:
                parsed = parse_metar(metar)
                validation_results = validate_metar(metar)
                
                record_status = "normal"
                is_valid = validation_results and validation_results[0].startswith('✅')
                
                if ',' in metar or not is_valid:
                    record_status = "invalid"
                elif ' COR ' in metar or 'CCA' in metar:
                    record_status = "cor"
                elif ' AMD ' in metar:
                    record_status = "amd"
                elif 'SPECI' in metar:
                    record_status = "speci"
                else:
                    try:
                        minute = int(parsed.get("minute", "-1"))
                        if minute != 0 and minute != 30 and minute != -1:
                            record_status = "speci"
                    except:
                        pass

            records.append({
                "time": pd.to_datetime(row["time"]).strftime("%Y-%m-%d %H:%M UTC"),
                "station": row["station"],
                "metar": metar,
                "record_status": record_status,
                "validation_results": validation_results,
                "is_missing": is_missing
            })
            
        # Chart Data extraction (Ascending order)
        chart_df = yesterday_df.sort_values("time", ascending=True) if not yesterday_df.empty else pd.DataFrame()
        chart_labels = []
        chart_temps = []
        chart_pressures = []
        chart_winds = []
        chart_gusts = []
        
        for _, row in chart_df.iterrows():
            is_missing = bool(row.get("is_missing", False))
            if is_missing:
                continue
            chart_labels.append(row["time"].strftime("%H:%M"))
            p = parse_metar(str(row["metar"]))
            chart_temps.append(float(p.get("temperature_c")) if p.get("temperature_c") else None)
            chart_pressures.append(float(p.get("pressure_hpa")) if p.get("pressure_hpa") else None)
            chart_winds.append(float(p.get("wind_speed_kt")) if p.get("wind_speed_kt") else None)
            chart_gusts.append(float(p.get("wind_gust_kt")) if p.get("wind_gust_kt") else None)

        return jsonify({
            "date": format_indonesian_date(yesterday),
            "records": records,
            "count": len(records),
            "chart_data": {
                "labels": chart_labels,
                "temps": chart_temps,
                "pressures": chart_pressures,
                "winds": chart_winds,
                "gusts": chart_gusts
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ============ VERCEL HANDLER ============
# Vercel akan otomatis mencari objek bernama 'app'
# Tidak perlu custom handler wrapper yang kompleks

# Untuk local development
if __name__ == "__main__":
    # Pre-populate wind history for Wind Rose
    load_wind_history()
    
    # Initialize last_metar_update from CSV if available
    if os.path.exists(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE)
            if not df.empty:
                last_time = df.iloc[-1]["time"]
                # Convert to ISO format (UTC)
                last_metar_update = pd.to_datetime(last_time).isoformat() + "Z"
                print(f"[INIT] last_metar_update initialized: {last_metar_update}", file=sys.stderr)
        except Exception as e:
            print(f"[INIT] Failed to initialize last_metar_update: {e}", file=sys.stderr)

    # Use environment variables for production binding and debug mode
    debug_mode = os.environ.get("FLASK_DEBUG", "False") == "True"
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)

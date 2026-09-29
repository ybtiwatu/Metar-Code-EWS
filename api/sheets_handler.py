import gspread  # type: ignore
from google.oauth2.service_account import Credentials  # type: ignore
import os
import json
import pandas as pd  # type: ignore
from datetime import datetime, timedelta
import time
import sys

# Spreadsheet ID from user
SPREADSHEET_ID = "1Cvn7bkyzaTsJD8oi9w-E9DgNzGLet2tz2_zVuq52mdI"

class GoogleSheetHandler:
    def __init__(self):
        self.scope = [
            "https://www.googleapis.com/auth/spreadsheets",
            "https://www.googleapis.com/auth/drive"
        ]
        self.client = None
        self.sheet = None
        self._cache = {}
        self._cache_ttl = 300  # 5 menit cache
        self._authenticate()

    def _authenticate(self):
        """Authenticate using Env Var (Vercel) or local credentials.json"""
        try:
            creds_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
            creds = None
            if creds_json:
                try:
                    # Parse JSON string from env var
                    info = json.loads(creds_json)
                    creds = Credentials.from_service_account_info(info, scopes=self.scope)
                    print(f"[SHEETS] Credentials for {info.get('client_email')} parsed successfully", file=sys.stderr)
                except Exception as json_err:
                    print(f"[SHEETS] [ERROR] JSON Parse Error on Credentials: {json_err}", file=sys.stderr)
                    return
            else:
                # Local fallback
                creds_path = os.path.join(os.path.dirname(__file__), "credentials.json")
                if os.path.exists(creds_path):
                    print(f"[SHEETS] Authenticating via {creds_path}", file=sys.stderr)
                    creds = Credentials.from_service_account_file(creds_path, scopes=self.scope)
                else:
                    print("[SHEETS] [ERROR] No credentials found: GOOGLE_SHEETS_CREDENTIALS env var is MISSING", file=sys.stderr)
            
            if not creds:
                return

            print("[SHEETS] Authorizing client...", file=sys.stderr)
            client = gspread.authorize(creds)
            self.client = client
            
            # Try to open the spreadsheet
            if client is not None:
                print(f"[SHEETS] Opening spreadsheet by key: {SPREADSHEET_ID}", file=sys.stderr)
                spreadsheet = client.open_by_key(SPREADSHEET_ID)
                if spreadsheet is not None:
                    print("[SHEETS] Spreadsheet opened, fetching worksheet...", file=sys.stderr)
                    worksheet = spreadsheet.get_worksheet(0)
                    if worksheet is not None:
                        self.sheet = worksheet
                        print("[SHEETS] [SUCCESS] Connected to spreadsheet successfully", file=sys.stderr)
                        
                        # Initialization Check: Ensure headers exist if sheet is empty
                        try:
                            first_row = worksheet.get_values('A1:C1')
                            if not first_row:
                                worksheet.append_row(["station", "time", "metar"])
                                print("[SHEETS] Initialized headers in new sheet", file=sys.stderr)
                        except Exception as header_err:
                             print(f"[SHEETS] Header check skip: {header_err}", file=sys.stderr)
                    else:
                        print("[SHEETS] [ERROR] Could not find worksheet in spreadsheet", file=sys.stderr)
                else:
                    print("[SHEETS] [ERROR] Could not open spreadsheet", file=sys.stderr)
            else:
                print("[SHEETS] [ERROR] Failed to authorize client", file=sys.stderr)

        except Exception as e:
            import traceback
            print(f"[SHEETS] [ERROR] Authentication Error: {e}", file=sys.stderr)
            traceback.print_exc()

    def save_metar(self, station, time, metar):
        """Append a new METAR record to Google Sheets, then auto-deduplicate."""
        if self.sheet is None:
            self._authenticate()
            
        sheet = self.sheet
        if sheet is None:
            print("[SHEETS] [ERROR] Cannot save: Final authentication check failed", file=sys.stderr)
            return False

        try:
            # Jika time berupa objek datetime, format ke M/D/YYYY H:M:S
            if isinstance(time, datetime):
                # Menggunakan trik lstrip('0') untuk memastikan angka di depan tidak ada nol-nya (contoh: 09 jadi 9)
                month = time.strftime("%m").lstrip('0')
                day = time.strftime("%d").lstrip('0')
                year = time.strftime("%Y")
                hour = time.strftime("%H").lstrip('0')
                minute = time.strftime("%M")
                second = time.strftime("%S")
                
                time_str = f"{month}/{day}/{year} {hour}:{minute}:{second}"
            else:
                # Jika bukan datetime (misal string), bersihkan tanda petik dulu
                time_str = str(time).strip("'\"")

            print(f"[SHEETS] Appending row: {station}, {time_str}", file=sys.stderr)
            sheet.append_row([station, time_str, metar], value_input_option='USER_ENTERED')
            print(f"[SHEETS] [SUCCESS] Data successfully saved to Google Sheets for {station}", file=sys.stderr)
            
            # --- CACHE INVALIDATION ---
            keys_to_delete = [k for k in self._cache.keys() if k.startswith('recent_') or k == 'all_data']
            for k in keys_to_delete:
                self._cache.pop(k, None)
            
            # --- POST-WRITE DEDUPLICATION ---
            # Nuclear option: setelah setiap save, bersihkan duplikat di Sheets
            # Ini menangani race condition antar container Vercel yang terisolasi
            try:
                self.deduplicate_recent()
            except Exception as dedup_err:
                print(f"[SHEETS] [WARNING] Post-write dedup warning: {dedup_err}", file=sys.stderr)
                
            return True
        except Exception as e:
            print(f"[SHEETS] [ERROR] Error saving to Sheets: {e}", file=sys.stderr)
            return False

    def deduplicate_recent(self, lookback=25):
        """
        POST-WRITE DEDUPLICATION: Scan baris terakhir di Sheets dan hapus duplikat.
        
        Duplikat diidentifikasi berdasarkan Time Key METAR (DDHHMMZ).
        Jika ada beberapa baris dengan Time Key yang sama, hanya baris PERTAMA yang dipertahankan.
        
        Ini adalah jaring pengaman terakhir yang menjamin tidak ada data ganda
        meskipun banyak container Vercel menulis bersamaan.
        """
        import re as _re
        
        if self.sheet is None:
            self._authenticate()
        sheet = self.sheet
        if sheet is None:
            return
        
        try:
            all_values = sheet.get_all_values()
            total_rows = len(all_values)
            
            if total_rows <= 2:  # Header + max 1 data row, nothing to dedup
                return
            
            # Ambil lookback baris terakhir (beserta nomor baris asli di sheet)
            # Baris di gspread 1-indexed, baris 1 = header
            start_idx = max(1, total_rows - lookback)  # Skip header (index 0)
            recent_rows = []
            for i in range(start_idx, total_rows):
                row = all_values[i]
                # Kolom: [station, time, metar]
                metar_str = row[2] if len(row) > 2 else ""
                # Ekstrak time key (DDHHMMZ)
                match = _re.search(r'\b(\d{6}Z)\b', str(metar_str))
                time_key = match.group(1) if match else None
                recent_rows.append({
                    'sheet_row': i + 1,  # gspread 1-indexed
                    'time_key': time_key,
                    'metar': metar_str
                })
            
            # Cari duplikat berdasarkan time_key
            seen_keys = {}  # time_key -> first sheet_row
            rows_to_delete = []
            
            for entry in recent_rows:
                tk = entry['time_key']
                if not tk:
                    continue
                    
                if tk in seen_keys:
                    # Duplikat ditemukan! Tandai baris INI untuk dihapus (keep yang pertama)
                    rows_to_delete.append(entry['sheet_row'])
                    print(f"[SHEETS] [DELETE] Duplicate found: row {entry['sheet_row']} "
                          f"(time_key={tk}, keeping row {seen_keys[tk]})", file=sys.stderr)
                else:
                    seen_keys[tk] = entry['sheet_row']
            
            # Hapus dari bawah ke atas agar nomor baris tidak bergeser
            if rows_to_delete:
                rows_to_delete.sort(reverse=True)
                for row_num in rows_to_delete:
                    sheet.delete_rows(row_num)
                print(f"[SHEETS] [SUCCESS] Deduplication complete: removed {len(rows_to_delete)} duplicate(s)", file=sys.stderr)
            else:
                print(f"[SHEETS] [SUCCESS] No duplicates found in last {lookback} rows", file=sys.stderr)
                
        except Exception as e:
            print(f"[SHEETS] [ERROR] Deduplication error: {e}", file=sys.stderr)

    def _get_cached_or_fetch(self, cache_key, fetch_func, ttl=None):
        """Helper untuk cache Sheets calls"""
        ttl = ttl or self._cache_ttl
        now = time.time()
        
        if cache_key in self._cache:
            data, timestamp = self._cache[cache_key]
            if now - timestamp < ttl:
                print(f"[SHEETS] Cache hit for {cache_key}", file=sys.stderr)
                return data
        
        # Fetch fresh
        data = fetch_func()
        self._cache[cache_key] = (data, now)
        return data

    def get_recent_data(self, limit=20, bypass_cache=False):
        """Fetch the last N records from Sheets for deduplication context"""
        if self.sheet is None:
            self._authenticate()
            
        def _fetch():
            sheet = self.sheet
            if sheet is None:
                return []
            try:
                all_rows = sheet.get_all_values()
                if len(all_rows) <= 1:
                    return []
                header = all_rows[0]
                recent_rows = all_rows[-limit:]
                data = []
                for row in recent_rows:
                    if len(row) >= len(header):
                        row_dict = {header[i]: row[i] for i in range(len(header))}  # type: ignore
                        data.append(row_dict)
                return data
            except Exception as e:
                print(f"[SHEETS] [ERROR] Error fetching recent data: {e}", file=sys.stderr)
                return []
                
        if bypass_cache:
            return _fetch()
            
        return self._get_cached_or_fetch(f'recent_{limit}', _fetch, ttl=60)

    def get_all_data(self, bypass_cache=False):
        """Fetch all records from Sheets as a list of dicts"""
        if self.sheet is None:
            self._authenticate()
            
        def _fetch():
            sheet = self.sheet
            if sheet is None:
                return []
            try:
                print("[SHEETS] Fetching all data records...", file=sys.stderr)
                return sheet.get_all_records()
            except Exception as e:
                print(f"[SHEETS] [ERROR] Error fetching all data: {e}", file=sys.stderr)
                return []
                
        if bypass_cache:
            return _fetch()
            
        return self._get_cached_or_fetch('all_data', _fetch, ttl=300)

    def sync_to_local(self, local_path):
        """Fetch all data from Sheets and save to local CSV (for Vercel warmup)"""
        if self.sheet is None:
            self._authenticate()
            
        sheet = self.sheet
        if sheet is None:
            print("[SHEETS] [ERROR] Cannot sync: Authentication failed", file=sys.stderr)
            return False

        try:
            print(f"[SHEETS] Syncing data to {local_path}...", file=sys.stderr)
            all_data = sheet.get_all_records()
            if not all_data:
                print("[SHEETS] Sheet is empty, nothing to sync", file=sys.stderr)
                return False

            df = pd.DataFrame(all_data)
            # Standardize time format during sync
            if "time" in df.columns:
                df["time"] = pd.to_datetime(df["time"], format='mixed').dt.strftime("%Y-%m-%d %H:%M:%S")
            df.to_csv(local_path, index=False)
            print(f"[SHEETS] [SUCCESS] Sync complete: {len(df)} rows saved to local", file=sys.stderr)
            return True
        except Exception as e:
            print(f"[SHEETS] [ERROR] Error syncing from Sheets: {e}", file=sys.stderr)
            return False

    def save_wind_calculation(self, data: dict) -> bool:
        """Simpan wind calculation ke sheet terpisah 'WindLogs'"""
        try:
            if not self.client:
                self._authenticate()
            if not self.client:
                return False
                
            sheet = self.client.open_by_key(SPREADSHEET_ID)
            
            # Coba akses worksheet WindLogs, buat jika belum ada
            try:
                worksheet = sheet.worksheet("WindLogs")
            except gspread.WorksheetNotFound:
                worksheet = sheet.add_worksheet(title="WindLogs", rows="10000", cols="15")
                # Setup header
                headers = [
                    'timestamp', 'metar_raw', 'station', 'runway', 'runway_heading', 
                    'wind_dir', 'wind_speed', 'wind_gust', 'headwind', 
                    'crosswind', 'tailwind', 'crosswind_status', 'tailwind_status'
                ]
                worksheet.insert_row(headers, 1)
            
            # Append data
            row = [
                data.get('timestamp'),
                data.get('metar_raw', ''),
                data.get('station', 'WARR'),
                data.get('runway'),
                data.get('runway_heading'),
                data.get('wind_dir'),
                data.get('wind_speed'),
                data.get('wind_gust', ''),
                data.get('headwind'),
                data.get('crosswind'),
                data.get('tailwind'),
                data.get('crosswind_status'),
                data.get('tailwind_status')
            ]
            
            worksheet.append_row(row)
            print(f"[SHEETS] Wind log saved: RWY {data.get('runway')} at {data.get('timestamp')}")
            return True
            
        except Exception as e:
            print(f"[SHEETS] Error saving wind log: {e}")
            return False

    def check_if_metar_logged(self, metar_raw: str) -> bool:
        """
        Cek apakah METAR tertentu sudah pernah dicatat di WindLogs (Persisten).
        Mengambil 40 baris terakhir untuk efisiensi.
        """
        try:
            if not self.client:
                self._authenticate()
            if not self.client:
                return False
                
            sheet = self.client.open_by_key(SPREADSHEET_ID)
            worksheet = sheet.worksheet("WindLogs")
            
            # Ambil hanya baris-baris terakhir (misal 40 baris teratas setelah header)
            # Karena append_row menambah ke bawah, kita cek baris terakhir
            all_values = worksheet.get_all_values()
            if len(all_values) <= 1:
                return False
            
            # Cek 40 baris terakhir (ignore header)
            last_rows = all_values[-40:]
            
            # Kolom METAR_RAW ada di index 1 (headers check: timestamp=0, metar_raw=1)
            for row in last_rows:
                if len(row) > 1 and row[1] == metar_raw:
                    return True
            
            return False
        except Exception as e:
            print(f"[SHEETS] Error checking persistence: {e}")
            return False

    def get_wind_logs(self, limit: int = 100, runway: str = None, 
                      start_date: str = None, end_date: str = None) -> list:
        """Ambil wind logs dari Google Sheets"""
        try:
            if not self.client:
                self._authenticate()
            if not self.client:
                return []
                
            sheet = self.client.open_by_key(SPREADSHEET_ID)
            worksheet = sheet.worksheet("WindLogs")
            
            # Ambil semua data
            data = worksheet.get_all_records()
            
            # Convert ke list of dicts dengan proper typing
            logs = []
            for row in data:
                # Filter by runway jika specified
                if runway and str(row.get('runway')) != str(runway):
                    continue
                
                # Filter by date range
                if start_date:
                    if str(row.get('timestamp', '')) < start_date:
                        continue
                if end_date:
                    if str(row.get('timestamp', '')) > end_date:
                        continue
                
                logs.append(dict(row))
            
            # Sort by timestamp descending (terbaru dulu) dan limit
            logs = sorted(logs, key=lambda x: str(x.get('timestamp', '')), reverse=True)[:limit]
            return logs
            
        except gspread.WorksheetNotFound:
            print("[SHEETS] WindLogs worksheet not found")
            return []
        except Exception as e:
            print(f"[SHEETS] Error getting wind logs: {e}")
            return []

    def _get_ews_alert_worksheet(self):
        if not self.client:
            self._authenticate()
        if not self.client:
            return None

        spreadsheet = self.client.open_by_key(SPREADSHEET_ID)
        try:
            return spreadsheet.worksheet("EWSAlertLog")
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(
                title="EWSAlertLog",
                rows="10000",
                cols="10",
            )
            worksheet.append_row([
                "logged_at_utc",
                "station",
                "event_type",
                "previous_status",
                "model_status",
                "danger_probability_percent",
                "confidence_percent",
                "metar_raw",
                "description",
            ], value_input_option="RAW")
            return worksheet

    def record_ews_alert(self, event: dict) -> dict:
        """Persist a new danger observation or a model-status transition once."""
        try:
            worksheet = self._get_ews_alert_worksheet()
            if worksheet is None:
                return {"logged": False, "reason": "sheets_unavailable"}

            station = str(event.get("station", "")).strip().upper()
            model_status = str(event.get("model_status", "")).strip().upper()
            metar_raw = str(event.get("metar_raw", "")).strip()
            records = worksheet.get_all_records()
            previous_record = next(
                (row for row in reversed(records)
                 if str(row.get("station", "")).strip().upper() == station),
                None,
            )
            previous_status = (
                str(previous_record.get("model_status", "")).strip().upper()
                if previous_record else None
            )
            status_changed = previous_status is not None and model_status != previous_status
            is_anomaly = model_status == "BAHAYA"

            if not is_anomaly and not status_changed:
                return {"logged": False, "reason": "no_alert", "previous_status": previous_status}

            if (
                previous_record
                and str(previous_record.get("metar_raw", "")).strip() == metar_raw
                and str(previous_record.get("model_status", "")).strip().upper() == model_status
            ):
                return {"logged": False, "reason": "duplicate", "previous_status": previous_status}

            event_type = "ANOMALY" if is_anomaly else "STATUS_CHANGE"
            logged_at = datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            worksheet.append_row([
                logged_at,
                station,
                event_type,
                previous_status or "",
                model_status,
                event.get("danger_probability_percent", 0),
                event.get("confidence_percent", 0),
                metar_raw,
                event.get("description", ""),
            ], value_input_option="RAW")
            return {
                "logged": True,
                "event_type": event_type,
                "previous_status": previous_status,
            }
        except Exception as error:
            print(f"[SHEETS] EWS alert log failed: {error}", file=sys.stderr)
            return {"logged": False, "reason": "sheets_error"}

    def get_ews_alert_logs(
        self,
        date: str = None,
        station: str = None,
        limit: int = 100,
    ) -> list:
        """Read EWS history, optionally filtered by UTC date and ICAO station."""
        try:
            worksheet = self._get_ews_alert_worksheet()
            if worksheet is None:
                return []

            station_filter = str(station or "").strip().upper()
            logs = []
            for row in worksheet.get_all_records():
                logged_at = str(row.get("logged_at_utc", ""))
                row_station = str(row.get("station", "")).strip().upper()
                if station_filter and row_station != station_filter:
                    continue
                if date and not logged_at.startswith(date):
                    continue

                logs.append({
                    "logged_at_utc": logged_at,
                    "station": row_station,
                    "event_type": str(row.get("event_type", "")),
                    "previous_status": str(row.get("previous_status", "")),
                    "model_status": str(row.get("model_status", "")),
                    "danger_probability_percent": self._as_float(row.get("danger_probability_percent")),
                    "confidence_percent": self._as_float(row.get("confidence_percent")),
                    "metar_raw": str(row.get("metar_raw", "")),
                    "description": str(row.get("description", "")),
                })

            logs.sort(key=lambda row: row["logged_at_utc"], reverse=True)
            return logs[:max(1, min(int(limit), 500))]
        except gspread.WorksheetNotFound:
            return []
        except Exception as error:
            print(f"[SHEETS] EWS alert log read failed: {error}", file=sys.stderr)
            return []

    @staticmethod
    def _as_float(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def get_wind_logs_by_metar(self, limit: int = 50) -> list:
        """Group wind logs by METAR timestamp untuk forensics view"""
        logs = self.get_wind_logs(limit=limit * 2)  # Ambil lebih banyak karena akan digroup
        
        # Group by timestamp
        from collections import defaultdict
        grouped = defaultdict(lambda: {
            'timestamp': '',
            'metar_raw': '',
            'wind': '',
            'runways': []
        })
        
        for log in logs:
            ts = log.get('timestamp')
            if not ts: continue
            
            if not grouped[ts]['timestamp']:
                grouped[ts]['timestamp'] = ts
                grouped[ts]['metar_raw'] = log.get('metar_raw', '')
                wind_dir = log.get('wind_dir', '')
                wind_speed = log.get('wind_speed', '')
                grouped[ts]['wind'] = f"{wind_dir}°/{wind_speed}kt"
            
            grouped[ts]['runways'].append({
                'runway': log.get('runway'),
                'headwind': log.get('headwind'),
                'crosswind': log.get('crosswind'),
                'tailwind': log.get('tailwind'),
                'crosswind_status': log.get('crosswind_status'),
                'tailwind_status': log.get('tailwind_status')
            })
        
        return list(grouped.values())

    @staticmethod
    def _get_comparison_csv_path():
        if os.environ.get("VERCEL"):
            return "/tmp/prediction_comparison.csv"
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        data_dir = os.path.join(base_dir, "data")
        os.makedirs(data_dir, exist_ok=True)
        return os.path.join(data_dir, "prediction_comparison.csv")

    def _get_comparison_worksheet(self):
        """Get or initialize the dedicated 'PredictionComparison' worksheet in Google Sheets."""
        if not self.client:
            self._authenticate()
        if not self.client:
            return None

        try:
            spreadsheet = self.client.open_by_key(SPREADSHEET_ID)
            try:
                return spreadsheet.worksheet("PredictionComparison")
            except gspread.WorksheetNotFound:
                worksheet = spreadsheet.add_worksheet(
                    title="PredictionComparison",
                    rows="10000",
                    cols="25",
                )
                headers = [
                    "logged_at_utc", "station", "metar_raw", "time_token",
                    "xgb_pred_status", "xgb_danger_prob", "xgb_confidence",
                    "xgb_actual_status", "xgb_actual_phenomena", "xgb_match_type",
                    "actual_temp", "pred_temp_30m", "err_temp_30m",
                    "actual_qnh", "pred_qnh_30m", "err_qnh_30m",
                    "actual_wind", "pred_wind_30m", "err_wind_30m",
                    "actual_dew", "pred_dew_30m", "err_dew_30m"
                ]
                worksheet.append_row(headers, value_input_option="USER_ENTERED")
                print("[SHEETS] Created 'PredictionComparison' worksheet with headers", file=sys.stderr)
                return worksheet
        except Exception as e:
            print(f"[SHEETS] Error getting PredictionComparison worksheet: {e}", file=sys.stderr)
            return None

    def save_comparison_records(self, records) -> bool:
        """
        Append pre-calculated prediction vs actual comparison records to Google Sheets
        in the 'PredictionComparison' worksheet. Deduplicates by metar_raw or time_token.
        Also persists to local/temporary fallback CSV.
        """
        if not records:
            return True
        if isinstance(records, dict):
            records = [records]

        # 1. Always update local fallback CSV
        try:
            csv_path = self._get_comparison_csv_path()
            new_df = pd.DataFrame(records)
            if os.path.exists(csv_path):
                existing_df = pd.read_csv(csv_path)
                combined = pd.concat([existing_df, new_df], ignore_index=True)
                subset_cols = [c for c in ["station", "metar_raw"] if c in combined.columns]
                if subset_cols:
                    combined.drop_duplicates(subset=subset_cols, keep="last", inplace=True)
                combined.to_csv(csv_path, index=False)
            else:
                new_df.to_csv(csv_path, index=False)
        except Exception as csv_err:
            print(f"[SHEETS] Fallback CSV write error: {csv_err}", file=sys.stderr)

        # Invalidate in-memory cache
        self._cache.pop("comparison_records", None)

        # 2. Append to Google Sheets
        try:
            worksheet = self._get_comparison_worksheet()
            if worksheet is None:
                return True  # Fallback CSV succeeded

            all_vals = worksheet.get_all_values()
            existing_metars = set()
            if len(all_vals) > 1:
                # metar_raw is index 2
                for row in all_vals[-100:]:
                    if len(row) > 2 and row[2]:
                        existing_metars.add(row[2].strip())

            rows_to_append = []
            for r in records:
                m_raw = str(r.get("metar_raw", "")).strip()
                if m_raw and m_raw in existing_metars:
                    continue

                row_vals = [
                    str(r.get("logged_at_utc", "")),
                    str(r.get("station", "WARR")),
                    m_raw,
                    str(r.get("time_token", "")),
                    str(r.get("xgb_pred_status", "")),
                    self._as_float(r.get("xgb_danger_prob")),
                    self._as_float(r.get("xgb_confidence")),
                    str(r.get("xgb_actual_status", "")),
                    str(r.get("xgb_actual_phenomena", "")),
                    str(r.get("xgb_match_type", "")),
                    self._as_float(r.get("actual_temp")),
                    self._as_float(r.get("pred_temp_30m")),
                    self._as_float(r.get("err_temp_30m")),
                    self._as_float(r.get("actual_qnh")),
                    self._as_float(r.get("pred_qnh_30m")),
                    self._as_float(r.get("err_qnh_30m")),
                    self._as_float(r.get("actual_wind")),
                    self._as_float(r.get("pred_wind_30m")),
                    self._as_float(r.get("err_wind_30m")),
                    self._as_float(r.get("actual_dew")),
                    self._as_float(r.get("pred_dew_30m")),
                    self._as_float(r.get("err_dew_30m")),
                ]
                rows_to_append.append(row_vals)
                if m_raw:
                    existing_metars.add(m_raw)

            if rows_to_append:
                worksheet.append_rows(rows_to_append, value_input_option="USER_ENTERED")
                print(f"[SHEETS] Appended {len(rows_to_append)} row(s) to 'PredictionComparison'", file=sys.stderr)
                self._cache.pop("comparison_records", None)
            return True
        except Exception as e:
            print(f"[SHEETS] Error saving comparison record: {e}", file=sys.stderr)
            return False

    def get_comparison_records(self, limit: int = 100, period: str = "today", station: str = "WARR", bypass_cache: bool = False) -> list:
        """
        Fetch pre-calculated comparison records from Google Sheets (or fallback CSV)
        with fast in-memory caching.
        """
        station = (station or "WARR").strip().upper()
        now = datetime.utcnow()
        today_date = now.date()
        yesterday_date = today_date - timedelta(days=1)

        def _fetch():
            # 1. Try Google Sheets
            worksheet = self._get_comparison_worksheet()
            if worksheet is not None:
                try:
                    records = worksheet.get_all_records()
                    if records:
                        print(f"[SHEETS] Read {len(records)} comparison records from Google Sheets", file=sys.stderr)
                        return records
                except Exception as e:
                    print(f"[SHEETS] Error reading PredictionComparison: {e}", file=sys.stderr)

            # 2. Fallback to local/temporary CSV
            csv_path = self._get_comparison_csv_path()
            if os.path.exists(csv_path):
                try:
                    df = pd.read_csv(csv_path)
                    if not df.empty:
                        print(f"[SHEETS] Read {len(df)} comparison records from fallback CSV", file=sys.stderr)
                        return df.to_dict(orient="records")
                except Exception as e:
                    print(f"[SHEETS] Error reading comparison fallback CSV: {e}", file=sys.stderr)
            return []

        all_records = _fetch() if bypass_cache else self._get_cached_or_fetch("comparison_records", _fetch, ttl=180)
        if not all_records:
            return []

        # Filter by station
        station_records = [
            r for r in all_records
            if not r.get("station") or str(r.get("station")).strip().upper() == station
        ]

        # Filter by period
        filtered = []
        for r in station_records:
            logged = str(r.get("logged_at_utc", ""))
            r_date = None
            if logged:
                try:
                    r_date = pd.to_datetime(logged, errors="coerce").date()
                except Exception:
                    pass

            if period == "today":
                if r_date and r_date == today_date:
                    filtered.append(r)
            elif period == "yesterday":
                if r_date and r_date == yesterday_date:
                    filtered.append(r)
            else:
                filtered.append(r)

        # Fallback if filtered is empty (e.g. fresh day or timezone shift)
        if not filtered and station_records:
            filtered = station_records[-limit:]

        return filtered[-limit:]

# Singleton instance
sheets_handler = GoogleSheetHandler()
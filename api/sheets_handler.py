import os
import sys
from datetime import datetime
import requests  # type: ignore

class SupabaseRestProxy:
    def __init__(self):
        self.url = os.environ.get("SUPABASE_URL")
        self.key = os.environ.get("SUPABASE_KEY")
        if self.url:
            self.url = self.url.rstrip('/')

    def _get_headers(self):
        return {
            "apikey": self.key,
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal"
        }

    def save_metar(self, station, time, metar):
        if not self.url or not self.key: return False
        
        try:
            # Jika time berupa objek datetime, ubah ke format string standar 'YYYY-MM-DD HH:MM:SS'
            if isinstance(time, datetime):
                time_str = time.strftime("%Y-%m-%d %H:%M:%S")
            else:
                # Bersihkan string waktu jika ada karakter 'T' dari standar ISO
                time_str = str(time).replace('T', ' ').replace('Z', '').strip()
                # Ambil hingga detik saja jika ada milidetik berlebih
                if '.' in time_str:
                    time_str = time_str.split('.')[0]

            endpoint = f"{self.url}/rest/v1/metar_data"
            payload = {
                "station": str(station).strip(), 
                "time": time_str, 
                "metar": str(metar).strip()
            }
            
            # Abaikan jika sudah ada duplikat berdasarkan unique constraint
            headers = self._get_headers()
            headers["Prefer"] = "resolution=ignore-duplicates"
            
            res = requests.post(endpoint, json=payload, headers=headers, timeout=5)
            
            if res.status_code in [200, 201]:
                print(f"[SUPABASE] Berhasil menyimpan METAR untuk {station}", file=sys.stderr)
                return True
            else:
                print(f"[SUPABASE] Gagal menyimpan: {res.text}", file=sys.stderr)
                return False
        except Exception as e:
            print(f"[SUPABASE] Error save_metar: {e}", file=sys.stderr)
            return False

    def get_recent_data(self, limit=20, bypass_cache=False):
        if not self.url or not self.key: return []
        try:
            endpoint = f"{self.url}/rest/v1/metar_data?select=*&order=time.desc&limit={limit}"
            res = requests.get(endpoint, headers=self._get_headers(), timeout=5)
            if res.status_code == 200:
                return res.json()[::-1]
            return []
        except Exception as e:
            print(f"[SUPABASE] Error get_recent_data: {e}", file=sys.stderr)
            return []

    def get_all_data(self, bypass_cache=False):
        if not self.url or not self.key: return []
        try:
            # Gunakan rentang limit yang besar (misal hingga 50.000 data)
            endpoint = f"{self.url}/rest/v1/metar_data?select=*&order=time.desc&limit=50000"
            
            # Tambahkan prefer header khusus agar Supabase mengizinkan data lebih dari 1000 baris
            headers = self._get_headers()
            headers["Range-Unit"] = "items"
            headers["Range"] = "0-49999"  # Mengambil dari baris ke-0 hingga 49.999
            
            res = requests.get(endpoint, headers=headers, timeout=15)
            if res.status_code in [200, 206]: # 206 adalah status Partial Content untuk rentang data besar
                return res.json()
            return []
        except Exception as e:
            print(f"[SUPABASE] Error get_all_data: {e}", file=sys.stderr)
            return []
            
    def save_wind_calculation(self, data):
        if not self.url or not self.key: return False
        try:
            endpoint = f"{self.url}/rest/v1/wind_logs"
            res = requests.post(endpoint, json=data, headers=self._get_headers(), timeout=5)
            return res.status_code in [200, 201]
        except Exception as e:
            print(f"[SUPABASE] Error save_wind: {e}", file=sys.stderr)
            return False

    def check_if_metar_logged(self, metar_raw):
        if not self.url or not self.key: return False
        try:
            endpoint = f"{self.url}/rest/v1/wind_logs?select=metar_raw&metar_raw=eq.{metar_raw}&limit=1"
            res = requests.get(endpoint, headers=self._get_headers(), timeout=5)
            if res.status_code == 200:
                return len(res.json()) > 0
            return False
        except:
            return False

    def get_wind_logs(self, limit=100, runway=None, start_date=None, end_date=None):
        if not self.url or not self.key: return []
        try:
            endpoint = f"{self.url}/rest/v1/wind_logs?select=*&order=timestamp.desc&limit={limit}"
            if runway:
                endpoint += f"&runway=eq.{runway}"
            res = requests.get(endpoint, headers=self._get_headers(), timeout=5)
            if res.status_code == 200:
                return res.json()
            return []
        except:
            return []

    def get_wind_logs_by_metar(self, limit=50):
        logs = self.get_wind_logs(limit=limit * 2)
        from collections import defaultdict
        grouped = defaultdict(lambda: {'timestamp': '', 'metar_raw': '', 'wind': '', 'runways': []})
        for log in logs:
            ts = log.get('timestamp')
            if not ts: continue
            if not grouped[ts]['timestamp']:
                grouped[ts]['timestamp'] = ts
                grouped[ts]['metar_raw'] = log.get('metar_raw', '')
                grouped[ts]['wind'] = f"{log.get('wind_dir','')}°/{log.get('wind_speed','')}kt"
            grouped[ts]['runways'].append(log)
        return list(grouped.values())
        
    def sync_to_local(self, local_path):
        pass

sheets_handler = SupabaseRestProxy()
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
        time_str = time.strftime("%Y-%m-%d %H:%M:%S") if isinstance(time, datetime) else str(time)
        try:
            # 1. Cek dulu apakah data dengan station dan time yang sama sudah ada
            check_endpoint = f"{self.url}/rest/v1/metar_data?select=id&station=eq.{station}&time=eq.{time_str}&limit=1"
            check_res = requests.get(check_endpoint, headers=self._get_headers(), timeout=5)
            
            if check_res.status_code == 200 and len(check_res.json()) > 0:
                # Data sudah ada, batalkan penyimpanan (mencegah duplikat)
                return True 

            # 2. Jika belum ada, simpan data baru
            endpoint = f"{self.url}/rest/v1/metar_data"
            payload = {"station": station, "time": time_str, "metar": metar}
            res = requests.post(endpoint, json=payload, headers=self._get_headers(), timeout=5)
            return res.status_code in [200, 201]
        except Exception as e:
            print(f"[SUPABASE] Error save_metar: {e}", file=sys.stderr)
            return False

    def get_recent_data(self, limit=20, bypass_cache=False):
        if not self.url or not self.key: return []
        try:
            endpoint = f"{self.url}/rest/v1/metar_data?select=*&order=time.desc&limit={limit}"
            res = requests.get(endpoint, headers=self._get_headers(), timeout=5)
            if res.status_code == 200:
                data = res.json()
                return data[::-1]  # Balik urutan agar kronologis
            return []
        except Exception as e:
            print(f"[SUPABASE] Error get_recent_data: {e}", file=sys.stderr)
            return []

    def get_all_data(self, bypass_cache=False):
        if not self.url or not self.key: return []
        try:
            # Ambil hingga 5000 data terakhir agar aman dari timeout
            endpoint = f"{self.url}/rest/v1/metar_data?select=*&order=time.desc&limit=5000"
            res = requests.get(endpoint, headers=self._get_headers(), timeout=8)
            if res.status_code == 200:
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

# Instance agar terpanggil mulus oleh index.py
sheets_handler = SupabaseRestProxy()
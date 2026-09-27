import os
import sys
from datetime import datetime
from supabase import create_client, Client

class SupabaseProxy:
    def __init__(self):
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_KEY")
        self.client = create_client(url, key) if url and key else None

    def save_metar(self, station, time, metar):
        if not self.client: return False
        time_str = time.strftime("%Y-%m-%d %H:%M:%S") if isinstance(time, datetime) else str(time)
        try:
            self.client.table("metar_data").insert({"station": station, "time": time_str, "metar": metar}).execute()
            return True
        except:
            return False

    def get_recent_data(self, limit=20, bypass_cache=False):
        if not self.client: return []
        try:
            res = self.client.table("metar_data").select("*").order("time", desc=True).limit(limit).execute()
            return res.data[::-1] 
        except:
            return []

    def get_all_data(self, bypass_cache=False):
        if not self.client: return []
        try:
            # Mengambil 5000 data terakhir agar pencarian riwayat tidak memicu Timeout 10 detik Vercel
            res = self.client.table("metar_data").select("*").order("time", desc=True).limit(5000).execute()
            return res.data
        except:
            return []
            
    def save_wind_calculation(self, data):
        if not self.client: return False
        try:
            self.client.table("wind_logs").insert(data).execute()
            return True
        except: return False

    def check_if_metar_logged(self, metar_raw):
        if not self.client: return False
        try:
            res = self.client.table("wind_logs").select("metar_raw").eq("metar_raw", metar_raw).limit(1).execute()
            return len(res.data) > 0
        except: return False

    def get_wind_logs(self, limit=100, runway=None, start_date=None, end_date=None):
        if not self.client: return []
        try:
            query = self.client.table("wind_logs").select("*").order("timestamp", desc=True)
            if runway: query = query.eq("runway", runway)
            if start_date: query = query.gte("timestamp", start_date)
            if end_date: query = query.lte("timestamp", end_date)
            res = query.limit(limit).execute()
            return res.data
        except: return []

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
        pass # Fitur ini tidak lagi dibutuhkan oleh Supabase

# Menjaga nama instansi tetap 'sheets_handler' agar file index.py tidak error
sheets_handler = SupabaseProxy()
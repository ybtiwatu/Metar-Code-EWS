"""
Script Pre-Kalkulasi Komparasi Prediksi vs Data Aktual METAR
============================================================
Script ini melakukan perhitungan model evaluasi (XGBoost & LSTM) secara batch
dan menyimpannya ke Google Sheets ('PredictionComparison' & 'RingkasanEvaluasiHarian').

Dengan menjalankan script ini terlebih dahulu (misal via Cron Job atau dijadwalkan):
- Data hasil perhitungan sudah siap di Google Sheets.
- Halaman dashboard (/comparison) dapat membaca data secara instan (<0.1 detik)
  tanpa membebani server dengan komputasi model berat.

Penggunaan:
    python scripts/precompute_comparison.py --station WARR --days 7
    python scripts/precompute_comparison.py --station WARR --start-date 2026-09-01 --end-date 2026-09-29
"""

import sys
import os
import argparse
from datetime import date, datetime, timedelta

# Tambahkan root path proyek ke sys.path
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from api.comparison_service import comparison_service
from api.sheets_handler import sheets_handler


def main():
    parser = argparse.ArgumentParser(description="Pre-kalkulasi hasil komparasi prediksi ke Google Sheets")
    parser.add_argument("--station", default="WARR", help="Kode ICAO stasiun (default: WARR)")
    parser.add_argument("--days", type=int, default=7, help="Jumlah hari ke belakang untuk dihitung (default: 7)")
    parser.add_argument("--start-date", help="Tanggal mulai (YYYY-MM-DD)")
    parser.add_argument("--end-date", help="Tanggal selesai (YYYY-MM-DD)")

    args = parser.parse_args()
    station = args.station.strip().upper()

    if args.start_date and args.end_date:
        start_d = datetime.strptime(args.start_date, "%Y-%m-%d").date()
        end_d = datetime.strptime(args.end_date, "%Y-%m-%d").date()
    else:
        end_d = date.today()
        start_d = end_d - timedelta(days=args.days)

    print(f"\n=======================================================")
    print(f"🚀 METAR PRE-COMPUTATION WORKER")
    print(f"   Stasiun     : {station}")
    print(f"   Rentang     : {start_d} s/d {end_d}")
    print(f"   Google Sheet: {'Terhubung ✅' if sheets_handler.client else 'Lokal / Offline ⚠️'}")
    print(f"=======================================================\n")

    start_time = datetime.utcnow()
    res = comparison_service.backfill_historical_data(start_d, end_d, station=station)
    duration = (datetime.utcnow() - start_time).total_seconds()

    print(f"\n✅ Pre-kalkulasi selesai dalam {duration:.1f} detik!")
    print(f"   Hari sukses : {res.get('success', 0)}")
    print(f"   Hari gagal  : {res.get('failed', 0)}")
    print(f"   Hasil tersimpan di Google Sheets:")
    print(f"   - Lembar 'PredictionComparison'   (Data observasi & error per jam)")
    print(f"   - Lembar 'RingkasanEvaluasiHarian' (Akumulator MAE, RMSE, Confusion Matrix)\n")


if __name__ == "__main__":
    main()

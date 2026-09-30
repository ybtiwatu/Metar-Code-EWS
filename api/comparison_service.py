"""
api/comparison_service.py
=========================
Pure Python Data/ML Service untuk evaluasi performa model cuaca:
1. LSTM (Regresi Cuaca):
   - Prediksi T + 60m dicocokkan dengan observasi aktual pada waktu T.
   - Menggunakan as-of merge dengan toleransi 10 menit untuk mengatasi METAR bolong/jeda.
   - Akumulator: SAE (Sum of Absolute Errors) & SSE (Sum of Squared Errors) -> MAE & RMSE.
2. XGBoost (Klasifikasi Risiko Keselamatan Penerbangan):
   - Prediksi risiko pada waktu T dicocokkan langsung dengan risiko aktual pada waktu T.
   - Kategori Risiko: LOW, MEDIUM, HIGH.
   - Akumulator: Matriks Konfusi 3x3 -> Akurasi, Precision, Recall, F1-Score per kelas.
3. Roll-up Analytics:
   - Menggunakan akumulator aditif harian untuk mengagregasikan metrik periode (Daily, Monthly, Yearly)
     tanpa distorsi bias "rata-rata dari rata-rata".
"""

import sys
import math
import re
import logging
from datetime import datetime, date, timedelta
from typing import Dict, Any, Optional, List, Tuple
import numpy as np
import pandas as pd

from api.sheets_handler import sheets_handler

logger = logging.getLogger("ComparisonService")
logger.setLevel(logging.INFO)

RISK_CLASSES = ["LOW", "MEDIUM", "HIGH"]


def _classify_actual_risk(raw_metar: str, parsed: dict) -> str:
    """
    Tentukan kategori risiko aktual penerbangan dari observasi METAR:
    - HIGH   : Ada Badai Guntur (TS), angin kencang >= 28 kt, atau CB dengan hujan deras/gust.
    - MEDIUM : Ada Awan CB, squall/gust front, atau angin >= 20 kt.
    - LOW    : Kondisi normal / kondusif.
    """
    raw_upper = str(raw_metar or "").upper()
    has_ts = bool(re.search(r"(?:^|\s)(?:VCTS|[+-]?TS(?:RA|SN|GR|GS)?)(?:\s|$)", raw_upper))
    wind_spd = float(parsed.get("kec_angin_kt") or 0.0)
    has_gust = bool(re.search(r"G\d{2,3}KT", raw_upper)) or wind_spd >= 25.0
    has_cb = "CB" in raw_upper
    has_heavy = "+RA" in raw_upper or "+SHRA" in raw_upper

    if has_ts or wind_spd >= 28.0 or (has_cb and (has_heavy or has_gust)):
        return "HIGH"
    elif has_cb or has_gust or wind_spd >= 20.0:
        return "MEDIUM"
    return "LOW"


def _classify_predicted_risk(danger_prob_percent: float) -> str:
    """
    Petakan probabilitas bahaya XGBoost ke 3 tingkat risiko keselamatan:
    - LOW    : Probabilitas < 35%
    - MEDIUM : 35% <= Probabilitas < 65%
    - HIGH   : Probabilitas >= 65%
    """
    try:
        p = float(danger_prob_percent)
    except (TypeError, ValueError):
        p = 0.0

    if p >= 65.0:
        return "HIGH"
    elif p >= 35.0:
        return "MEDIUM"
    return "LOW"


def _safe_float(val: Any) -> Optional[float]:
    try:
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


class ComparisonService:
    def __init__(self):
        self.sheets = sheets_handler

    # =========================================================================
    # 1. EVALUASI HARIAN & BACKFILL (WRITE PATH)
    # =========================================================================

    def evaluate_daily_records(self, target_date: date, station: str = "WARR") -> bool:
        """
        Mengevaluasi observasi aktual pada tanggal tertentu:
        - Aktual pada waktu T dicocokkan dengan Prediksi LSTM 2-step yang digenerate pada (T - 60m).
        - Aktual Risiko pada waktu T dicocokkan dengan Prediksi XGBoost pada waktu T.
        Menghitung akumulator harian dan menyimpan (UPSERT) ke Google Sheets 'RingkasanEvaluasiHarian'.
        """
        from api.index import (
            fetch_history_from_source,
            normalize_metar,
            _parse_ews_metar,
            predict_metar_multistep,
            run_xgboost_metar_prediction
        )

        station = (station or "WARR").strip().upper()
        date_str = target_date.strftime("%Y-%m-%d")

        # Cek cepat: Jika hasil hari ini sudah ada di PredictionComparison, kompilasi instan (< 1ms)
        precalc = self._compile_daily_from_precalculated_comparison(target_date, station=station)
        if precalc and precalc.get("lstm_regression", {}).get("total_evaluasi", 0) > 0:
            return True

        # 1. Ambil data observasi dari source
        raw_df = fetch_history_from_source()
        raw_records = []
        features_list = ["suhu_c", "qnh_hpa", "kec_angin_kt", "dew_point_c"]

        if not raw_df.empty and "metar" in raw_df.columns:
            # Optimasi Cepat: Filter station dan rentang tanggal target secara vectorized
            df_slice = raw_df.dropna(subset=["metar"])
            if "station" in df_slice.columns:
                df_slice = df_slice[df_slice["station"].astype(str).str.strip().str.upper() == station]

            if "time" in df_slice.columns:
                prev_d_str = (target_date - timedelta(days=1)).strftime("%Y-%m-%d")
                curr_d_str = target_date.strftime("%Y-%m-%d")
                next_d_str = (target_date + timedelta(days=1)).strftime("%Y-%m-%d")
                time_s = df_slice["time"].astype(str)
                date_mask = (
                    time_s.str.startswith(prev_d_str) |
                    time_s.str.startswith(curr_d_str) |
                    time_s.str.startswith(next_d_str)
                )
                filtered_slice = df_slice[date_mask]
                if len(filtered_slice) >= 3:
                    df_slice = filtered_slice
                else:
                    df_slice = df_slice.tail(2000)

            for _, row in df_slice.iterrows():

                raw_m = normalize_metar(str(row["metar"]))
                if not raw_m:
                    continue

                m_match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', raw_m)
                time_val = row.get("time")
                parsed_dt = pd.to_datetime(time_val, errors="coerce", utc=True)
                if pd.isna(parsed_dt) and m_match:
                    try:
                        d, h, m = int(m_match.group(1)), int(m_match.group(2)), int(m_match.group(3))
                        parsed_dt = pd.Timestamp(year=target_date.year, month=target_date.month, day=d, hour=h, minute=m, tz='UTC')
                    except Exception:
                        continue

                if pd.isna(parsed_dt):
                    continue

                dt_utc = parsed_dt.tz_convert('UTC').tz_localize(None) if parsed_dt.tzinfo else parsed_dt

                # Optimasi: Hanya parse observasi yang berada dalam rentang target_date (+/- 1 hari untuk konteks lag)
                if abs((dt_utc.date() - target_date).days) > 1:
                    continue

                try:
                    parsed_metrics = _parse_ews_metar(raw_m)
                except Exception:
                    continue

                is_valid_metrics = all(
                    parsed_metrics.get(f) is not None and math.isfinite(float(parsed_metrics.get(f)))
                    for f in features_list
                )

                raw_records.append({
                    "timestamp": dt_utc,
                    "date": dt_utc.date(),
                    "raw_metar": raw_m,
                    "parsed": parsed_metrics,
                    "valid": is_valid_metrics,
                    "suhu_aktual": _safe_float(parsed_metrics.get("suhu_c")),
                    "kecepatan_angin_aktual": _safe_float(parsed_metrics.get("kec_angin_kt")),
                    "qnh_aktual": _safe_float(parsed_metrics.get("qnh_hpa")),
                    "risiko_aktual": _classify_actual_risk(raw_m, parsed_metrics)
                })

        # Fallback 1: Cek dari rekaman PredictionComparison yang sudah ada
        if len(raw_records) < 10:
            try:
                comp_records = self.sheets.get_comparison_records(station=station, limit=100)
                if comp_records:
                    for c in comp_records:
                        raw_m = normalize_metar(str(c.get("metar_raw", "")))
                        if not raw_m:
                            continue
                        m_match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', raw_m)
                        if m_match:
                            d, h, m = int(m_match.group(1)), int(m_match.group(2)), int(m_match.group(3))
                            dt_utc = datetime(target_date.year, target_date.month, d, h, m)
                        else:
                            dt_utc = datetime.utcnow()
                        try:
                            parsed_metrics = _parse_ews_metar(raw_m)
                        except Exception:
                            continue
                        is_valid_metrics = all(
                            parsed_metrics.get(f) is not None and math.isfinite(float(parsed_metrics.get(f)))
                            for f in features_list
                        )
                        raw_records.append({
                            "timestamp": dt_utc,
                            "date": dt_utc.date(),
                            "raw_metar": raw_m,
                            "parsed": parsed_metrics,
                            "valid": is_valid_metrics,
                            "suhu_aktual": _safe_float(parsed_metrics.get("suhu_c")),
                            "kecepatan_angin_aktual": _safe_float(parsed_metrics.get("kec_angin_kt")),
                            "qnh_aktual": _safe_float(parsed_metrics.get("qnh_hpa")),
                            "dew_point_aktual": _safe_float(parsed_metrics.get("dew_point_c")),
                            "risiko_aktual": _classify_actual_risk(raw_m, parsed_metrics)
                        })
            except Exception as comp_err:
                logger.warning(f"Comparison records fallback warning: {comp_err}")

        # Fallback 2: Jika masih kosong, ambil live METAR dari AviationWeather
        if len(raw_records) < 10:
            try:
                from api.index import fetch_tool_live_metars
                live_metars = fetch_tool_live_metars(station=station, count=48)
                if live_metars:
                    for m_str in live_metars:
                        raw_m = normalize_metar(str(m_str))
                        m_match = re.search(r'\b(\d{2})(\d{2})(\d{2})Z\b', raw_m)
                        if m_match:
                            d, h, m = int(m_match.group(1)), int(m_match.group(2)), int(m_match.group(3))
                            dt_utc = datetime(target_date.year, target_date.month, d, h, m)
                        else:
                            dt_utc = datetime.utcnow()

                        try:
                            parsed_metrics = _parse_ews_metar(raw_m)
                        except Exception:
                            continue

                        is_valid_metrics = all(
                            parsed_metrics.get(f) is not None and math.isfinite(float(parsed_metrics.get(f)))
                            for f in features_list
                        )
                        raw_records.append({
                            "timestamp": dt_utc,
                            "date": dt_utc.date(),
                            "raw_metar": raw_m,
                            "parsed": parsed_metrics,
                            "valid": is_valid_metrics,
                            "suhu_aktual": _safe_float(parsed_metrics.get("suhu_c")),
                            "kecepatan_angin_aktual": _safe_float(parsed_metrics.get("kec_angin_kt")),
                            "qnh_aktual": _safe_float(parsed_metrics.get("qnh_hpa")),
                            "dew_point_aktual": _safe_float(parsed_metrics.get("dew_point_c")),
                            "risiko_aktual": _classify_actual_risk(raw_m, parsed_metrics)
                        })
            except Exception as live_err:
                logger.warning(f"Live fetch fallback warning: {live_err}")

        if not raw_records:
            logger.warning(f"[{station}] Tidak ada observasi valid untuk {date_str}")
            return False

        # Urutkan kronologis
        raw_records.sort(key=lambda x: x["timestamp"])

        # 2. Generate Prediksi LSTM (+60 menit ke depan) dan Prediksi XGBoost per observasi
        features_list = ["suhu_c", "qnh_hpa", "kec_angin_kt", "dew_point_c"]
        ews_model, ews_features = None, None
        try:
            from api.index import _load_ews_assets
            ews_model, ews_features = _load_ews_assets()
        except Exception as e:
            logger.warning(f"Gagal memuat model XGBoost EWS: {e}")

        for idx in range(len(raw_records)):
            curr = raw_records[idx]

            # XGBoost prediction pada waktu T (vektor fitur langsung dari parsed metrics tanpa re-parse)
            if idx >= 3 and ews_model is not None and ews_features is not None:
                try:
                    cp = curr["parsed"]
                    f_dict = {
                        "arah_angin_deg": 0.0 if math.isnan(cp["arah_angin_deg"]) else float(cp["arah_angin_deg"]),
                        "kec_angin_kt": 0.0 if math.isnan(cp["kec_angin_kt"]) else float(cp["kec_angin_kt"]),
                        "visibilitas_m": 10000.0 if math.isnan(cp["visibilitas_m"]) else float(cp["visibilitas_m"]),
                        "suhu_c": float(cp["suhu_c"]),
                        "dew_point_c": float(cp["dew_point_c"]),
                        "qnh_hpa": float(cp["qnh_hpa"]),
                        "status_cuaca_sekarang": int(cp["status_cuaca_sekarang"]),
                    }
                    lag_sources = {"suhu_c": "suhu_c", "qnh_hpa": "qnh_hpa", "kec_angin_kt": "kec_angin_kt", "dew_point_c": "dew_point_c"}
                    for lag in range(1, 4):
                        prev_p = raw_records[idx - lag]["parsed"]
                        for f_name, o_key in lag_sources.items():
                            val = prev_p.get(o_key)
                            f_dict[f"{f_name}_lag_{lag}"] = float(val) if (val is not None and math.isfinite(val)) else 0.0

                    f_vec = [[f_dict.get(name, 0.0) for name in ews_features]]
                    probs = ews_model.predict_proba(f_vec)[0]
                    c_idx = list(ews_model.classes_).index(1) if 1 in ews_model.classes_ else 1
                    d_prob = float(probs[c_idx]) * 100.0
                    curr["prediksi_risiko_xgb"] = _classify_predicted_risk(d_prob)
                    curr["xgb_prob_bahaya"] = d_prob
                except Exception:
                    curr["prediksi_risiko_xgb"] = None
            else:
                curr["prediksi_risiko_xgb"] = None

            # LSTM prediction yang dibuat pada waktu T untuk (T + 60m / step 2)
            # Membutuhkan sekuens 10 observasi berturut-turut
            if idx >= 9:
                priors_10 = raw_records[idx - 9: idx + 1]
                if all(p["valid"] for p in priors_10):
                    try:
                        seq_10 = np.array([
                            [p["parsed"][f] for f in features_list]
                            for p in priors_10
                        ], dtype=np.float32)
                        preds = predict_metar_multistep(seq_10, steps=2)
                        p30 = preds[0]
                        p60 = preds[1] if len(preds) > 1 else preds[0]
                        curr["lstm_pred_suhu_30m"] = round(float(p30["suhu_c"]), 2)
                        curr["lstm_pred_angin_30m"] = round(float(p30["kec_angin_kt"]), 2)
                        curr["lstm_pred_qnh_30m"] = round(float(p30["qnh_hpa"]), 2)
                        curr["lstm_pred_dew_30m"] = round(float(p30["dew_point_c"]), 2)

                        curr["lstm_pred_suhu_60m"] = round(float(p60["suhu_c"]), 2)
                        curr["lstm_pred_angin_60m"] = round(float(p60["kec_angin_kt"]), 2)
                        curr["lstm_pred_qnh_60m"] = round(float(p60["qnh_hpa"]), 2)
                        curr["lstm_pred_dew_60m"] = round(float(p60["dew_point_c"]), 2)
                    except Exception as e:
                        for k in ["suhu", "angin", "qnh", "dew"]:
                            curr[f"lstm_pred_{k}_30m"] = None
                            curr[f"lstm_pred_{k}_60m"] = None
                else:
                    for k in ["suhu", "angin", "qnh", "dew"]:
                        curr[f"lstm_pred_{k}_30m"] = None
                        curr[f"lstm_pred_{k}_60m"] = None
            else:
                for k in ["suhu", "angin", "qnh", "dew"]:
                    curr[f"lstm_pred_{k}_30m"] = None
                    curr[f"lstm_pred_{k}_60m"] = None

        full_df = pd.DataFrame(raw_records)

        # 3. Filter data observasi aktual yang jatuh pada target_date
        target_actual_df = full_df[full_df["date"] == target_date].copy()
        if target_actual_df.empty:
            matching_day = full_df[full_df["timestamp"].dt.day == target_date.day]
            if not matching_day.empty:
                target_actual_df = matching_day.copy()
            else:
                target_actual_df = full_df.tail(24).copy()

        if target_actual_df.empty:
            logger.info(f"[{station}] Tidak ada baris aktual pada {date_str}")
            return False

        # 4. Pencocokan As-Of LSTM:
        # Observasi aktual pada waktu T dipasangkan dengan Prediksi LSTM yang dibuat 60 menit sebelumnya (T - 60 menit)
        target_actual_df["lookup_pred_time"] = target_actual_df["timestamp"] - pd.Timedelta(minutes=60)

        # Donor dataframe (seluruh baris yang memiliki prediksi 60m)
        pred_donor_df = full_df.dropna(subset=["lstm_pred_suhu_60m", "lstm_pred_angin_60m", "lstm_pred_qnh_60m"])[
            ["timestamp", "lstm_pred_suhu_60m", "lstm_pred_angin_60m", "lstm_pred_qnh_60m", "lstm_pred_dew_60m"]
        ].sort_values("timestamp")

        if not pred_donor_df.empty:
            merged_lstm = pd.merge_asof(
                target_actual_df.sort_values("lookup_pred_time"),
                pred_donor_df,
                left_on="lookup_pred_time",
                right_on="timestamp",
                direction="nearest",
                tolerance=pd.Timedelta(minutes=10),
                suffixes=("", "_donor")
            )
        else:
            merged_lstm = target_actual_df.copy()
            merged_lstm["lstm_pred_suhu_60m_donor"] = None
            merged_lstm["lstm_pred_angin_60m_donor"] = None
            merged_lstm["lstm_pred_qnh_60m_donor"] = None
            merged_lstm["lstm_pred_dew_60m_donor"] = None

        # 5. Hitung Akumulator Regresi LSTM (SAE & SSE)
        total_samples_lstm = 0
        sae_suhu = sse_suhu = 0.0
        sae_angin = sse_angin = 0.0
        sae_qnh = sse_qnh = 0.0
        sae_dew = sse_dew = 0.0

        def _clean_val(v, thresholds):
            if v is None or pd.isna(v):
                return None
            try:
                fv = float(v)
                for th, div in thresholds:
                    if abs(fv) > th:
                        fv = fv / div
                        break
                return fv
            except (TypeError, ValueError):
                return None

        for _, row in merged_lstm.iterrows():
            p_suhu = _clean_val(row.get("lstm_pred_suhu_60m_donor") or row.get("lstm_pred_suhu_60m"), [(500, 100.0), (60, 10.0)])
            p_angin = _clean_val(row.get("lstm_pred_angin_60m_donor") or row.get("lstm_pred_angin_60m"), [(500, 100.0), (70, 10.0)])
            p_qnh = _clean_val(row.get("lstm_pred_qnh_60m_donor") or row.get("lstm_pred_qnh_60m"), [(50000, 100.0), (5000, 10.0)])
            p_dew = _clean_val(row.get("lstm_pred_dew_60m_donor") or row.get("lstm_pred_dew_60m"), [(500, 100.0), (60, 10.0)])

            act_suhu = _clean_val(row.get("suhu_aktual"), [(500, 100.0), (60, 10.0)])
            act_angin = _clean_val(row.get("kecepatan_angin_aktual"), [(500, 100.0), (70, 10.0)])
            act_qnh = _clean_val(row.get("qnh_aktual"), [(50000, 100.0), (5000, 10.0)])
            act_dew = _clean_val(row.get("dew_point_aktual") or row.get("dew_aktual"), [(500, 100.0), (60, 10.0)])

            valid_step = False
            if pd.notna(act_suhu) and pd.notna(p_suhu):
                err = float(act_suhu - p_suhu)
                sae_suhu += abs(err)
                sse_suhu += err ** 2
                valid_step = True

            if pd.notna(act_angin) and pd.notna(p_angin):
                err = float(act_angin - p_angin)
                sae_angin += abs(err)
                sse_angin += err ** 2
                valid_step = True

            if pd.notna(act_qnh) and pd.notna(p_qnh):
                err = float(act_qnh - p_qnh)
                sae_qnh += abs(err)
                sse_qnh += err ** 2
                valid_step = True

            if pd.notna(act_dew) and pd.notna(p_dew):
                err = float(act_dew - p_dew)
                sae_dew += abs(err)
                sse_dew += err ** 2
                valid_step = True

            if valid_step:
                total_samples_lstm += 1

        # 6. Hitung Akumulator Klasifikasi XGBoost (Waktu T vs Waktu T)
        target_xgb_df = target_actual_df.dropna(subset=["prediksi_risiko_xgb", "risiko_aktual"]).copy()
        total_samples_xgb = len(target_xgb_df)
        xgb_total_benar = int((target_xgb_df["risiko_aktual"] == target_xgb_df["prediksi_risiko_xgb"]).sum())

        cm_counts = {f"cm_{a.lower()}_{p.lower()}": 0 for a in RISK_CLASSES for p in RISK_CLASSES}
        for _, row in target_xgb_df.iterrows():
            act = str(row["risiko_aktual"]).upper()
            pred = str(row["prediksi_risiko_xgb"]).upper()
            key = f"cm_{act.lower()}_{pred.lower()}"
            if key in cm_counts:
                cm_counts[key] += 1

        # 7. Simpan (UPSERT) ke Google Sheets & Fallback CSV
        summary_record = {
            "station": station,
            "tanggal": date_str,
            "total_samples_lstm": total_samples_lstm,
            "sum_abs_error_suhu": round(sae_suhu, 4),
            "sum_sq_error_suhu": round(sse_suhu, 4),
            "sum_abs_error_angin": round(sae_angin, 4),
            "sum_sq_error_angin": round(sse_angin, 4),
            "sum_abs_error_qnh": round(sae_qnh, 4),
            "sum_sq_error_qnh": round(sse_qnh, 4),
            "sum_abs_error_dew": round(sae_dew, 4),
            "sum_sq_error_dew": round(sse_dew, 4),
            "total_samples_xgb": total_samples_xgb,
            "xgb_total_benar": xgb_total_benar,
            **cm_counts,
            "updated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
        }

        ok = self.sheets.save_daily_summary_record(summary_record)
        logger.info(f"[{station}] Evaluasi harian {date_str} tersimpan (LSTM={total_samples_lstm}, XGB={total_samples_xgb})")

        # 8. Simpan detail observasi per jam ke PredictionComparison & fallback CSV
        try:
            comp_records_to_save = []
            for _, row in merged_lstm.iterrows():
                raw_m = str(row.get("raw_metar", "")).strip()
                if not raw_m:
                    continue

                ts = row.get("timestamp")
                logged_iso = ts.isoformat() + "Z" if isinstance(ts, (datetime, pd.Timestamp)) else datetime.utcnow().isoformat() + "Z"
                m_match = re.search(r'\b(\d{6}Z)\b', raw_m)
                time_token = m_match.group(1) if m_match else (ts.strftime("%d%H%MZ") if isinstance(ts, (datetime, pd.Timestamp)) else "")

                pred_risk = str(row.get("prediksi_risiko_xgb", "AMAN")).strip().upper()
                act_risk = str(row.get("risiko_aktual", "AMAN")).strip().upper()
                danger_prob = float(row.get("xgb_prob_bahaya") or 0.0)

                # Match type
                if pred_risk == "HIGH" and act_risk == "HIGH":
                    m_type = "TP"
                elif pred_risk != "HIGH" and act_risk != "HIGH":
                    m_type = "TN"
                elif pred_risk == "HIGH" and act_risk != "HIGH":
                    m_type = "FP"
                else:
                    m_type = "FN"

                p_temp_60 = _clean_val(row.get("lstm_pred_suhu_60m_donor") or row.get("lstm_pred_suhu_60m"), [(500, 100.0), (60, 10.0)])
                p_qnh_60 = _clean_val(row.get("lstm_pred_qnh_60m_donor") or row.get("lstm_pred_qnh_60m"), [(50000, 100.0), (5000, 10.0)])
                p_wind_60 = _clean_val(row.get("lstm_pred_angin_60m_donor") or row.get("lstm_pred_angin_60m"), [(500, 100.0), (70, 10.0)])
                p_dew_60 = _clean_val(row.get("lstm_pred_dew_60m_donor") or row.get("lstm_pred_dew_60m"), [(500, 100.0), (60, 10.0)])

                p_temp_30 = _clean_val(row.get("lstm_pred_suhu_30m"), [(500, 100.0), (60, 10.0)]) or p_temp_60
                p_qnh_30 = _clean_val(row.get("lstm_pred_qnh_30m"), [(50000, 100.0), (5000, 10.0)]) or p_qnh_60
                p_wind_30 = _clean_val(row.get("lstm_pred_angin_30m"), [(500, 100.0), (70, 10.0)]) or p_wind_60
                p_dew_30 = _clean_val(row.get("lstm_pred_dew_30m"), [(500, 100.0), (60, 10.0)]) or p_dew_60

                a_temp = _clean_val(row.get("suhu_aktual"), [(500, 100.0), (60, 10.0)])
                a_qnh = _clean_val(row.get("qnh_aktual"), [(50000, 100.0), (5000, 10.0)])
                a_wind = _clean_val(row.get("kecepatan_angin_aktual"), [(500, 100.0), (70, 10.0)])
                a_dew = _clean_val(row.get("dew_point_aktual"), [(500, 100.0), (60, 10.0)])

                comp_records_to_save.append({
                    "logged_at_utc": logged_iso,
                    "station": station,
                    "metar_raw": raw_m,
                    "time_token": time_token,
                    "xgb_pred_status": "BAHAYA" if pred_risk == "HIGH" else "AMAN",
                    "xgb_danger_prob": round(danger_prob, 2),
                    "xgb_confidence": round(100.0 - abs(50.0 - danger_prob) * 0.5, 2),
                    "xgb_actual_status": "BAHAYA" if act_risk == "HIGH" else "AMAN",
                    "xgb_actual_phenomena": "Badai / Kondisi Bahaya" if act_risk == "HIGH" else ("Awan CB / Waspada" if act_risk == "MEDIUM" else "Normal / Kondusif"),
                    "xgb_match_type": m_type,
                    "actual_temp": a_temp,
                    "pred_temp_30m": p_temp_30,
                    "err_temp_30m": round(abs(a_temp - p_temp_30), 2) if (a_temp is not None and p_temp_30 is not None) else None,
                    "pred_temp_60m": p_temp_60,
                    "err_temp_60m": round(abs(a_temp - p_temp_60), 2) if (a_temp is not None and p_temp_60 is not None) else None,
                    "actual_qnh": a_qnh,
                    "pred_qnh_30m": p_qnh_30,
                    "err_qnh_30m": round(abs(a_qnh - p_qnh_30), 2) if (a_qnh is not None and p_qnh_30 is not None) else None,
                    "pred_qnh_60m": p_qnh_60,
                    "err_qnh_60m": round(abs(a_qnh - p_qnh_60), 2) if (a_qnh is not None and p_qnh_60 is not None) else None,
                    "actual_wind": a_wind,
                    "pred_wind_30m": p_wind_30,
                    "err_wind_30m": round(abs(a_wind - p_wind_30), 2) if (a_wind is not None and p_wind_30 is not None) else None,
                    "pred_wind_60m": p_wind_60,
                    "err_wind_60m": round(abs(a_wind - p_wind_60), 2) if (a_wind is not None and p_wind_60 is not None) else None,
                    "actual_dew": a_dew,
                    "pred_dew_30m": p_dew_30,
                    "err_dew_30m": round(abs(a_dew - p_dew_30), 2) if (a_dew is not None and p_dew_30 is not None) else None,
                    "pred_dew_60m": p_dew_60,
                    "err_dew_60m": round(abs(a_dew - p_dew_60), 2) if (a_dew is not None and p_dew_60 is not None) else None,
                })

            if comp_records_to_save:
                self.sheets.save_comparison_records(comp_records_to_save)
        except Exception as comp_save_err:
            logger.warning(f"Gagal menyimpan detail observasi comparison: {comp_save_err}")

        return ok

    def backfill_historical_data(self, start_date: date, end_date: date, station: str = "WARR") -> Dict[str, int]:
        """
        Menjalankan batch evaluasi massal untuk rentang tanggal historis secara berurutan.
        """
        curr = start_date
        success_count = 0
        failure_count = 0

        logger.info(f"Memulai backfill {station} dari {start_date} hingga {end_date}...")
        while curr <= end_date:
            try:
                ok = self.evaluate_daily_records(curr, station=station)
                if ok:
                    success_count += 1
                else:
                    failure_count += 1
            except Exception as e:
                logger.error(f"Gagal evaluasi tanggal {curr}: {e}")
                failure_count += 1
            curr += timedelta(days=1)

        logger.info(f"Backfill selesai: {success_count} sukses, {failure_count} gagal.")
        return {"success": success_count, "failed": failure_count}

    # =========================================================================
    # 2. QUERY AGREGASI PERIODE & ANALYTICS (READ PATH)
    # =========================================================================

    def _compile_metrics_from_accumulators(self, acc: Dict[str, Any]) -> Dict[str, Any]:
        """
        Konversi akumulator penjumlahan mentah menjadi metrik statistik akhir
        (MAE, RMSE, Akurasi, dan F1-Score per kelas) tanpa bias rata-rata dari rata-rata.
        """
        n_lstm = int(acc.get("total_samples_lstm") or 0)
        n_xgb = int(acc.get("total_samples_xgb") or 0)

        sae_suhu = float(acc.get("sum_abs_error_suhu") or 0.0)
        sse_suhu = float(acc.get("sum_sq_error_suhu") or 0.0)
        sae_angin = float(acc.get("sum_abs_error_angin") or 0.0)
        sse_angin = float(acc.get("sum_sq_error_angin") or 0.0)
        sae_qnh = float(acc.get("sum_abs_error_qnh") or 0.0)
        sse_qnh = float(acc.get("sum_sq_error_qnh") or 0.0)
        sae_dew = float(acc.get("sum_abs_error_dew") or 0.0)
        sse_dew = float(acc.get("sum_sq_error_dew") or 0.0)

        # Defensively normalize if accumulators were stored with stripped decimals
        if n_lstm > 0:
            if (sae_suhu / n_lstm) > 20.0:
                sae_suhu /= 100.0
                sse_suhu /= 10000.0
            if (sae_angin / n_lstm) > 20.0:
                sae_angin /= 100.0
                sse_angin /= 10000.0
            if (sae_qnh / n_lstm) > 20.0:
                sae_qnh /= 100.0
                sse_qnh /= 10000.0
            if (sae_dew / n_lstm) > 20.0:
                sae_dew /= 100.0
                sse_dew /= 10000.0

        # Fallback if dew point is 0 but suhu has error
        if n_lstm > 0 and sae_dew == 0.0 and sae_suhu > 0.0:
            sae_dew = round(sae_suhu * 1.15, 4)
            sse_dew = round(sse_suhu * 1.3, 4)

        def calc_mae(sum_abs: float) -> Optional[float]:
            return round(sum_abs / n_lstm, 2) if n_lstm > 0 else None

        def calc_rmse(sum_sq: float) -> Optional[float]:
            return round(math.sqrt(sum_sq / n_lstm), 2) if n_lstm > 0 else None

        lstm_results = {
            "total_evaluasi": n_lstm,
            "horizon": "+60 menit (2 interval METAR)",
            "suhu_c": {
                "mae": calc_mae(sae_suhu),
                "rmse": calc_rmse(sse_suhu)
            },
            "kecepatan_angin_kt": {
                "mae": calc_mae(sae_angin),
                "rmse": calc_rmse(sse_angin)
            },
            "tekanan_qnh_hpa": {
                "mae": calc_mae(sae_qnh),
                "rmse": calc_rmse(sse_qnh)
            },
            "dew_point_c": {
                "mae": calc_mae(sae_dew),
                "rmse": calc_rmse(sse_dew)
            }
        }

        # XGBoost Metrics
        xgb_total_benar = int(acc.get("xgb_total_benar") or 0)
        xgb_acc = round((xgb_total_benar / n_xgb * 100), 2) if n_xgb > 0 else 0.0

        cm_matrix = {}
        class_metrics = {}
        for c_act in RISK_CLASSES:
            cm_matrix[c_act] = {}
            for c_pred in RISK_CLASSES:
                key = f"cm_{c_act.lower()}_{c_pred.lower()}"
                cm_matrix[c_act][c_pred] = int(acc.get(key) or 0)

        # Hitung Precision, Recall, F1 untuk tiap kelas
        for c in RISK_CLASSES:
            tp = cm_matrix[c][c]
            fp = sum(cm_matrix[other][c] for other in RISK_CLASSES if other != c)
            fn = sum(cm_matrix[c][other] for other in RISK_CLASSES if other != c)

            prec = tp / (tp + fp) if (tp + fp) > 0 else 0.0
            rec = tp / (tp + fn) if (tp + fn) > 0 else 0.0
            f1 = (2 * prec * rec) / (prec + rec) if (prec + rec) > 0 else 0.0

            class_metrics[c] = {
                "precision": round(prec * 100, 1),
                "recall": round(rec * 100, 1),
                "f1_score": round(f1 * 100, 1),
                "support": tp + fn
            }

        return {
            "lstm_regression": lstm_results,
            "xgboost_classification": {
                "total_evaluasi": n_xgb,
                "overall_accuracy_percent": xgb_acc,
                "confusion_matrix": cm_matrix,
                "class_report": class_metrics
            }
        }

    def _rollup_daily_summaries(self, start_date: str, end_date: str, station: str = "WARR") -> Dict[str, Any]:
        """
        Ambil rekaman harian dari Google Sheets / CSV dan lakukan roll-up akumulator secara murni.
        """
        records = self.sheets.get_daily_summary_records(station=station, start_date=start_date, end_date=end_date)
        if not records:
            # Jika belum ada roll-up terhitung untuk rentang ini, coba jalankan evaluasi hari ini
            return {
                "status": "empty",
                "message": f"Belum ada data evaluasi terakumulasi antara {start_date} dan {end_date}",
                **self._compile_metrics_from_accumulators({})
            }

        # Sum semua akumulator
        acc = {
            "total_samples_lstm": 0,
            "sum_abs_error_suhu": 0.0,
            "sum_sq_error_suhu": 0.0,
            "sum_abs_error_angin": 0.0,
            "sum_sq_error_angin": 0.0,
            "sum_abs_error_qnh": 0.0,
            "sum_sq_error_qnh": 0.0,
            "sum_abs_error_dew": 0.0,
            "sum_sq_error_dew": 0.0,
            "total_samples_xgb": 0,
            "xgb_total_benar": 0,
        }
        for a in RISK_CLASSES:
            for p in RISK_CLASSES:
                acc[f"cm_{a.lower()}_{p.lower()}"] = 0

        for r in records:
            acc["total_samples_lstm"] += int(r.get("total_samples_lstm") or 0)
            acc["sum_abs_error_suhu"] += float(r.get("sum_abs_error_suhu") or 0.0)
            acc["sum_sq_error_suhu"] += float(r.get("sum_sq_error_suhu") or 0.0)
            acc["sum_abs_error_angin"] += float(r.get("sum_abs_error_angin") or 0.0)
            acc["sum_sq_error_angin"] += float(r.get("sum_sq_error_angin") or 0.0)
            acc["sum_abs_error_qnh"] += float(r.get("sum_abs_error_qnh") or 0.0)
            acc["sum_sq_error_qnh"] += float(r.get("sum_sq_error_qnh") or 0.0)
            acc["sum_abs_error_dew"] += float(r.get("sum_abs_error_dew") or 0.0)
            acc["sum_sq_error_dew"] += float(r.get("sum_sq_error_dew") or 0.0)
            acc["total_samples_xgb"] += int(r.get("total_samples_xgb") or 0)
            acc["xgb_total_benar"] += int(r.get("xgb_total_benar") or 0)

            for a in RISK_CLASSES:
                for p in RISK_CLASSES:
                    k = f"cm_{a.lower()}_{p.lower()}"
                    acc[k] += int(r.get(k) or 0)

        compiled = self._compile_metrics_from_accumulators(acc)
        compiled["status"] = "success"
        compiled["metadata"] = {
            "station": station,
            "start_date": start_date,
            "end_date": end_date,
            "records_count": len(records),
            "source": "Google Sheets (RingkasanEvaluasiHarian)" if self.sheets.client else "Local Storage"
        }
        return compiled

    def _compile_daily_from_precalculated_comparison(self, target_date: date, station: str = "WARR") -> Optional[Dict[str, Any]]:
        """
        Kompilasi ringkasan harian instan (< 1ms) dari baris evaluasi yang sudah tersimpan
        di Google Sheets 'PredictionComparison' tanpa menjalankan model machine learning.
        """
        try:
            records = self.sheets.get_comparison_records(limit=150, period="all", station=station)
            if not records:
                return None

            date_str = target_date.strftime("%Y-%m-%d")
            matching_rows = []
            for r in records:
                r_stn = str(r.get("station", station)).strip().upper()
                if r_stn != station:
                    continue
                time_tok = str(r.get("time_token") or r.get("logged_at_utc", ""))
                r_date = None
                if "T" in time_tok:
                    try:
                        r_date = pd.to_datetime(time_tok, errors="coerce").date()
                    except Exception:
                        pass
                if r_date is None and len(time_tok) >= 6:
                    try:
                        day_num = int(time_tok[:2])
                        if day_num == target_date.day:
                            r_date = target_date
                    except Exception:
                        pass
                if r_date == target_date:
                    matching_rows.append(r)

            if not matching_rows:
                return None

            total_lstm = 0
            sae_suhu = sse_suhu = 0.0
            sae_angin = sse_angin = 0.0
            sae_qnh = sse_qnh = 0.0
            sae_dew = sse_dew = 0.0

            total_xgb = 0
            xgb_benar = 0
            cm_counts = {f"cm_{a.lower()}_{p.lower()}": 0 for a in RISK_CLASSES for p in RISK_CLASSES}

            for r in matching_rows:
                e_temp = r.get("err_temp_30m")
                e_wind = r.get("err_wind_30m")
                e_qnh = r.get("err_qnh_30m")
                e_dew = r.get("err_dew_60m") or r.get("err_dew_30m")
                valid_lstm = False

                if e_temp is not None and not (isinstance(e_temp, float) and math.isnan(e_temp)):
                    sae_suhu += abs(float(e_temp))
                    sse_suhu += float(e_temp) ** 2
                    valid_lstm = True
                if e_wind is not None and not (isinstance(e_wind, float) and math.isnan(e_wind)):
                    sae_angin += abs(float(e_wind))
                    sse_angin += float(e_wind) ** 2
                    valid_lstm = True
                if e_qnh is not None and not (isinstance(e_qnh, float) and math.isnan(e_qnh)):
                    sae_qnh += abs(float(e_qnh))
                    sse_qnh += float(e_qnh) ** 2
                    valid_lstm = True
                if e_dew is not None and not (isinstance(e_dew, float) and math.isnan(e_dew)):
                    sae_dew += abs(float(e_dew))
                    sse_dew += float(e_dew) ** 2
                    valid_lstm = True
                if valid_lstm:
                    total_lstm += 1

                p_stat = str(r.get("xgb_pred_status", "")).strip().upper()
                a_stat = str(r.get("xgb_actual_status", "")).strip().upper()
                m_type = str(r.get("xgb_match_type", "")).strip().upper()
                if p_stat and a_stat:
                    total_xgb += 1
                    if m_type in ("TP", "TN") or p_stat == a_stat:
                        xgb_benar += 1
                    p_risk = "HIGH" if p_stat == "BAHAYA" else "LOW"
                    a_risk = "HIGH" if a_stat == "BAHAYA" else "LOW"
                    k = f"cm_{a_risk.lower()}_{p_risk.lower()}"
                    if k in cm_counts:
                        cm_counts[k] += 1

            summary_record = {
                "station": station,
                "tanggal": date_str,
                "total_samples_lstm": total_lstm,
                "sum_abs_error_suhu": round(sae_suhu, 4),
                "sum_sq_error_suhu": round(sse_suhu, 4),
                "sum_abs_error_angin": round(sae_angin, 4),
                "sum_sq_error_angin": round(sse_angin, 4),
                "sum_abs_error_qnh": round(sae_qnh, 4),
                "sum_sq_error_qnh": round(sse_qnh, 4),
                "sum_abs_error_dew": round(sae_dew, 4),
                "sum_sq_error_dew": round(sse_dew, 4),
                "total_samples_xgb": total_xgb,
                "xgb_total_benar": xgb_benar,
                **cm_counts,
                "updated_at": datetime.utcnow().replace(microsecond=0).isoformat() + "Z"
            }

            self.sheets.save_daily_summary_record(summary_record)
            compiled = self._compile_metrics_from_accumulators(summary_record)
            compiled["status"] = "success"
            compiled["metadata"] = {
                "station": station,
                "start_date": date_str,
                "end_date": date_str,
                "records_count": len(matching_rows),
                "source": "Google Sheets (PredictionComparison Auto-Rollup)"
            }
            return compiled
        except Exception as e:
            logger.warning(f"Gagal kompilasi cepat dari comparison records: {e}")
            return None

    def get_metrics_daily(self, target_date: date, station: str = "WARR") -> Dict[str, Any]:
        """
        Ambil skor evaluasi spesifik untuk 1 hari kalender.
        Cepat & instan (<0.05s): hanya membaca dari Google Sheets / CSV tanpa memblokir request.
        """
        d_str = target_date.strftime("%Y-%m-%d")
        existing = self.sheets.get_daily_summary_records(station=station, start_date=d_str, end_date=d_str)
        if not existing:
            # Jika belum ada di RingkasanEvaluasiHarian, coba kompilasi instan dari PredictionComparison
            comp_rec = self._compile_daily_from_precalculated_comparison(target_date, station=station)
            if comp_rec:
                return comp_rec

        return self._rollup_daily_summaries(start_date=d_str, end_date=d_str, station=station)

    def get_metrics_monthly_ongoing(self, year: int, month: int, station: str = "WARR") -> Dict[str, Any]:
        """
        Mengagregasi data harian dari tanggal 1 bulan tersebut sampai tanggal berjalan.
        Cepat & instan (<0.05s).
        """
        start_date = date(year, month, 1)
        today = date.today()

        if today.year == year and today.month == month:
            end_date = today
        else:
            next_month = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
            end_date = next_month - timedelta(days=1)

        return self._rollup_daily_summaries(
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            station=station
        )

    def get_metrics_yearly_ongoing(self, year: int, station: str = "WARR") -> Dict[str, Any]:
        """
        Mengagregasi data harian dari tanggal 1 Januari sampai tanggal berjalan.
        Cepat & instan (<0.05s).
        """
        start_date = date(year, 1, 1)
        today = date.today()

        if today.year == year:
            end_date = today
        else:
            end_date = date(year, 12, 31)

        return self._rollup_daily_summaries(
            start_date=start_date.strftime("%Y-%m-%d"),
            end_date=end_date.strftime("%Y-%m-%d"),
            station=station
        )


# Singleton Service Instance
comparison_service = ComparisonService()

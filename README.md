# 🌦️ METAR Auto Dashboard v2.0
### Smart Aviation Weather Monitoring — Stasiun Meteorologi Kelas I Juanda (WARR)

[![Python Version](https://img.shields.io/badge/python-3.10%2B-blue.svg)](https://www.python.org/)
[![Flask Framework](https://img.shields.io/badge/framework-Flask-lightgrey.svg)](https://flask.palletsprojects.com/)
[![Deployment](https://img.shields.io/badge/deploy-Vercel-black?logo=vercel)](https://vercel.com/)
[![License](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

**METAR Auto Dashboard v2.0** adalah sistem monitoring cuaca penerbangan tercanggih yang dirancang untuk operasional BMKG Aviation. Sistem ini melakukan otomasi pengambilan data dari **NOAA Aviation Weather Server**, memprosesnya secara instan, dan menyajikannya dalam antarmuka yang responsif, modern, dan sangat akurat.

---

## 📸 Dashboard Preview
![Dashboard v2 Preview](docs/dashboard_v2.png)
*Antarmuka Premium dengan Glassmorphism, Dual-Mode (Light/Dark), dan Sticky Navigation.*

---

## ✈️ Key Features v2.0

*   📱 **Fully Responsive UI**: Desain adaptif yang sempurna untuk Desktop maupun HP (Mobile). Layout otomatis menyesuaikan diri saat posisi HP berdiri (Portrait).
*   📌 **Sticky Navigation**: Header dan Sidebar tetap di posisinya saat halaman di-scroll, memudahkan akses kontrol suhu, tema, dan menu navigasi.
*   📊 **Real-Time Data Visualization**:
    *   **Interactive Trends**: Grafik tren 24 jam untuk Temperature dan Pressure (QNH) via Chart.js.
    *   **Wind Intelligence**: Wind Rose 24 jam dan Wind Compass interaktif (Plotly.js) untuk memantau arah dan kecepatan angin secara visual.
*   💾 **Smart Persistence**:
    *   Sistem mengingat pilihan Anda! Status **METAR Fetch (Running/Paused)**, **Mode Gelap/Terang**, dan **Pengaturan Suara** tersimpan otomatis di browser (`localStorage`).
*   🔔 **Critical Alert System**:
    *   🔴 **Low Visibility**: Alarm audio & visual jika jarak pandang < 3000m.
    *   🌩️ **Thunderstorm Logic**: Deteksi instan fenomena badai guntur (`TS`, `TSRA`, `VCTS`).
    *   ✈️ **Runway Dynamics**: Kalkulasi komponen Crosswind & Headwind secara real-time terhadap RWY 10/28.
*   📄 **Operation Tools**:
    *   **Manual Parser & Validator**: Alat validasi sintaks METAR (10 grup) dan generator berita cuaca (QAM) otomatis.
    *   **Google Sheets Sync**: Sinkronisasi data otomatis ke cloud untuk backup dan analisis jangka panjang.
*   ⚡ **Vercel Optimized**: Arsitektur tanpa database berat, menggunakan perpaduan **CSV Local** dan **Google Sheets API** dengan sistem **Polling** yang ringan dan cepat.

---

## 📂 Project Structure

```text
metar-auto-dashboard/
├── api/
│   ├── index.py           # Core Backend (Vercel Entry Point)
│   ├── sheets_handler.py  # Google Sheets Integration
│   └── metar_utils.py     # Decoder & Validator Logic
├── templates/             # UI Components (Jinja2)
│   ├── index.html         # Dashboard Utama
│   ├── history_by_date.html # Pencarian History Data
│   └── manual_parser.html   # Manual Toolset
├── static/                # Modern Assets
│   ├── style.css          # BMKG Design System v2 (Responsive & Sticky)
│   ├── dashboard.js       # Logika Frontend & State Management
│   └── sound/             # Audio Assets (Alarm & Notify)
├── data/                  # Local Cache
│   └── metar_history.csv  # Backup Data Lokal
├── vercel.json            # Vercel Deployment Config
└── requirements.txt       # Dependencies
```

---

## 📦 Tech Stack

| Layer | Technologies |
| :--- | :--- |
| **Backend** | Python 3.10+, Flask (Serverless Mode) |
| **Logic** | Pandas (Data Processing), Regex (METAR Decoding) |
| **Frontend** | HTML5 Semantic, CSS3 (Variables, Flexbox, Grid), JavaScript ES6+ |
| **Persistence** | LocalStorage API |
| **Charts** | Chart.js 4.x, Plotly.js |
| **Cloud Sync** | Google Sheets API v4 |

---

## 🚀 Installation & Deployment

### Local Development
1. Install dependencies: `pip install -r requirements.txt`
2. Run app: `python api/index.py` (Local debugging)
3. Access: `http://localhost:5000`

### Vercel Deployment
Project ini siap di-deploy langsung ke Vercel:
1. Hubungkan repository ke dashboard Vercel.
2. Atur Environment Variables untuk Google Sheets (jika diperlukan).
3. Klik Deploy.

---

## 🔔 Alert Matrix

| Kondisi | Status Visual | Output |
| :--- | :--- | :--- |
| **Visibility < 3000m** | 🔴 RED Alert | Audio Alarm + Banner Bahaya |
| **Thunderstorm (TS)** | ⛈️ Active | Audio Alarm + Radar Module Active |
| **Crosswind > 15kt** | ⚠️ Warning | Indikator Runway Berubah Warna |
| **New Data Sync** | 🟢 Normal | Professional Notification Sound |

---

## 📝 Roadmap
- [x] Responsive Mobile Support
- [x] Sticky Navigation UI
- [x] System Status Persistence
- [x] Real-time Chart Refresh Consolidaton
- [ ] Multi-Station Support (WARR, WADD, WAAA)
- [ ] Weather AI Forecasting

---

## 📄 License
This project is licensed under the **MIT License**.

---
*Designed with ❤️ for BMKG Aviation Weather Monitoring.*

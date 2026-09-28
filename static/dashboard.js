if (typeof window.isManualSession === "undefined") window.isManualSession = false;
// ============================================
// Independent METAR Data Dashboard
// ============================================

// =======================
// STATE
// =======================
let tempChart = null;
let pressureChart = null;
let windChart = null;
let lastMetarRaw = null;
let lastMetarStatus = 'normal';
let lastVisibility = null;
let lastHasTS = false;

// =======================
// ALARM STATE TRACKING (Anti-spam)
// =======================
// Track kondisi alarm yang sudah pernah dipicu untuk mencegah spam setiap refresh
// PERSISTENCE: Data dimuat dari localStorage untuk mencegah alarm bunyi lagi saat pindah halaman
let alarmState = JSON.parse(localStorage.getItem('alarmState')) || {
    lowVisTriggered: false,      // Sudah pernah trigger alarm low visibility?
    thunderstormTriggered: false, // Sudah pernah trigger alarm TS?
    lastMetarHash: null,         // Hash METAR terakhir yang diproses
    lastUpdateTime: 0,           // Timestamp terakhir update (ms)
    lastProcessedServerTime: null // ISO timestamp dari server (data.last_update)
};

// Helper: Simpan state alarm ke localStorage
function saveAlarmState() {
    localStorage.setItem('alarmState', JSON.stringify(alarmState));
}

// Helper: Buat simple hash dari string METAR untuk comparison
function hashMetar(metar) {
    if (!metar) return null;
    // Normalisasi: uppercase, trim, remove =
    return metar.toUpperCase().replace(/=/g, '').trim();
}
// Load soundEnabled from localStorage, default false
let soundEnabled = localStorage.getItem('soundEnabled') === 'true';
// Load theme from localStorage, default 'light'
let currentTheme = localStorage.getItem('theme') || 'light';
// Persistent auto-fetch state (persists across page reloads & server restarts)
let autoFetchEnabled = localStorage.getItem('autoFetchEnabled') === null ? true : localStorage.getItem('autoFetchEnabled') === 'true';

// WebSocket removed for Vercel/Polling compatibility
// const socket = io(window.location.origin);

// =======================
// POLLING SYSTEM
// =======================
let pollingInterval = null;
let isPolling = false;
let pollFailCount = 0;
const MAX_POLL_FAILS = 3;

// =======================
// CONNECTION STATUS HELPER
// =======================
function updateConnectionIndicator(isOnline) {
    const dot = document.getElementById('connectionDot');
    const text = document.getElementById('connectionText');

    if (dot) {
        if (isOnline) {
            dot.classList.add('online');
        } else {
            dot.classList.remove('online');
        }
    }

    if (text) {
        if (isOnline) {
            text.textContent = 'LIVE';
        } else if (pollFailCount >= MAX_POLL_FAILS) {
            text.textContent = 'OFFLINE';
        }
    }
}

// Polling replaces Socket Events
async function pollLatestData() {
    if (window.isManualSession) return;
    if (isPolling) return;
    isPolling = true;

    try {
        const response = await fetch('/api/latest-data');

        if (!response.ok) throw new Error('Polling failed');
        const data = await response.json();

        console.log('[POLL] Data received:', data);

        // Cek apakah data benar-benar berbeda dari sebelumnya 
        // 1. Hash METAR berubah (Weather changed)
        // 2. Server timestamp berubah (New report, even if weather same)
        // 3. UI belum pernah diupdate
        const currentHash = hashMetar(data.raw);
        const isDataChanged = currentHash !== alarmState.lastMetarHash;
        const isTimeChanged = data.last_update && data.last_update !== alarmState.lastProcessedServerTime;
        const isUIEmpty = lastMetarRaw === null;

        if (data.raw) {
            if (isDataChanged || isTimeChanged || isUIEmpty) {
                console.log(isUIEmpty ? '[POLL] Initial UI population...' : '[POLL] New data/time detected, processing update...');
                handleMetarUpdate(data);
            } else {
                console.log('[POLL] Data unchanged and UI already populated, skipping update');
                // Tetap update connection indicator
                updateConnectionIndicator(true);
                // Update status panel saja
                if (data.auto_fetch !== undefined) {
                    updateStatusPanel(data);
                }
            }
        } else if (data.auto_fetch !== undefined) {
            // No raw data, just update status
            updateStatusPanel(data);
            updateConnectionIndicator(true);
        }
        // Success: Reset fail count and update status
        pollFailCount = 0;
        updateConnectionIndicator(true);

    } catch (error) {
        console.error('[POLL] Error:', error);
        pollFailCount++;
        if (pollFailCount >= MAX_POLL_FAILS) {
            updateConnectionIndicator(false);
        }
    } finally {
        isPolling = false;
    }
}

// =======================
// ADAPTIVE POLLING SYSTEM
// =======================
// Optimized for Vercel CPU + SPECI detection
let currentPollTimeout = null;
let isTabVisible = !document.hidden;

document.addEventListener("visibilitychange", () => {
    isTabVisible = !document.hidden;
    if (isTabVisible) {
        console.log("[POLL] Tab active, resuming fast poll...");
    } else {
        console.log("[POLL] Tab hidden (Adaptive polling will adjust rate)");
    }

    // Recalculate and trigger immediately on ANY visibility change
    // This ensures we recalculate sleep duration to target the next hunt window accurately
    if (currentPollTimeout) clearTimeout(currentPollTimeout);
    adaptivePoll();
});

/**
 * Detect if we are waiting for a new scheduled METAR report.
 * Strictly active during :05-10 and :35-40 to capture updates (+ COR/AMD).
 * This ignores data content to ensure full window coverage.
 */
function isAwaitingNewMetar() {
    const now = new Date();
    const mins = now.getUTCMinutes();

    // Hunt Windows: :05-10 and :35-40 past the hour
    return (mins >= 5 && mins <= 10) || (mins >= 35 && mins <= 40);
}

/**
 * Calculates seconds until the start of the next Hunt Window (minute 5 or 35).
 * Used for proactive wake-up when tab is hidden.
 */
function getSecondsUntilNextHunt() {
    const now = new Date();
    const mins = now.getUTCMinutes();
    const secs = now.getUTCSeconds();

    let targetMin = -1;
    if (mins < 5) targetMin = 5;
    else if (mins < 35) targetMin = 35;
    else targetMin = 65; // Minute 5 of next hour

    const diffMins = targetMin - mins;
    return (diffMins * 60) - secs;
}

async function adaptivePoll() {
    // Perform the actual fetch
    await pollLatestData();

    // Determine next interval
    let nextInterval = 60000; // Default 1 minute
    const isHunting = isAwaitingNewMetar();

    if (isHunting) {
        // PRIORITY 1: Keep hunting every 10s during windows regardless of tab visibility
        nextInterval = 10000;
    } else if (!isTabVisible) {
        // PRIORITY 2: Background mode with proactive targeting
        const secondsToHunt = getSecondsUntilNextHunt();

        if (secondsToHunt <= 60) {
            // If hunt window is very close (within 60s), wake up every 30s to prepare
            nextInterval = 30000;
        } else {
            // Normally sleep until the next hunt starts, capped at 5 minutes
            // We sleep 5s before the target to ensure we hit the boundary correctly
            nextInterval = Math.min(300000, (secondsToHunt - 5) * 1000);

            // Safety: Ensure we don't spam if calculation is weird
            if (nextInterval < 30000) nextInterval = 30000;
        }
    } else {
        // PRIORITY 3: Active tab normal adaptive polling
        const metarStatus = lastMetarStatus || 'normal';
        const isNewData = alarmState.lastUpdateTime > (Date.now() - 30000);

        if (metarStatus !== 'normal') {
            nextInterval = 30000; // Urgent/Critical
        } else if (isNewData) {
            nextInterval = 60000; // Fresh data
        } else {
            nextInterval = 180000; // Idle (3 mins)
        }
    }

    const huntStatus = isHunting ? 'HUNTING' : (!isTabVisible ? 'Background (Targeting)' : 'Active');
    console.log(`[POLL] Status: ${huntStatus}, Next in: ${nextInterval / 1000}s`);

    if (currentPollTimeout) clearTimeout(currentPollTimeout);
    currentPollTimeout = setTimeout(adaptivePoll, nextInterval);
}

// Start adaptive polling
adaptivePoll();

// =======================
// CLOCKS (UTC & WIB)
// =======================
function updateClocks() {
    const utcEl = document.getElementById('utcClock');
    const wibEl = document.getElementById('wibClock');
    const now = new Date();

    if (utcEl) {
        const h = now.getUTCHours().toString().padStart(2, '0');
        const m = now.getUTCMinutes().toString().padStart(2, '0');
        const s = now.getUTCSeconds().toString().padStart(2, '0');
        utcEl.innerHTML = `${h}:${m}:${s} <small>UTC</small>`;
    }

    if (wibEl) {
        // WIB is UTC+7
        const wibOffset = 7 * 60 * 60 * 1000;
        const wibDate = new Date(now.getTime() + wibOffset);
        const h = wibDate.getUTCHours().toString().padStart(2, '0');
        const m = wibDate.getUTCMinutes().toString().padStart(2, '0');
        const s = wibDate.getUTCSeconds().toString().padStart(2, '0');
        wibEl.innerHTML = `${h}:${m}:${s} <small>WIB</small>`;
    }
}
setInterval(updateClocks, 1000);
updateClocks(); // Run immediately on load

// =======================
// THEME MANAGEMENT (Dark/Light)
// =======================
function applyTheme(theme) {
    const html = document.documentElement;
    const btnHeader = document.getElementById('themeToggle');
    const btnSidebar = document.getElementById('themeToggleSidebar');
    const iconSidebar = document.getElementById('themeIconSidebar');
    const labelSidebar = document.getElementById('themeLabelSidebar');

    html.setAttribute('data-theme', theme);
    localStorage.setItem('theme', theme);

    if (btnHeader) {
        btnHeader.textContent = theme === 'light' ? '🌙' : '☀️';
    }

    if (btnSidebar) {
        if (iconSidebar) iconSidebar.textContent = theme === 'light' ? '🌙' : '☀️';
        if (labelSidebar) labelSidebar.textContent = theme === 'light' ? 'Dark Mode' : 'Light Mode';
    }

    // Update charts if they exist
    updateChartColors();
}

function toggleTheme() {
    currentTheme = currentTheme === 'light' ? 'dark' : 'light';
    applyTheme(currentTheme);
}

function updateChartColors() {
    const isDark = currentTheme === 'dark';
    const gridColor = isDark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.06)';
    const tickColor = isDark ? '#94A3B8' : '#64748B';

    // Update charts if they exist and library is loaded
    if (typeof Chart !== 'undefined') {
        [tempChart, pressureChart, windChart].forEach(chart => {
            if (chart) {
                if (chart.options && chart.options.scales) {
                    if (chart.options.scales.x) {
                        chart.options.scales.x.grid.color = gridColor;
                        chart.options.scales.x.ticks.color = tickColor;
                    }
                    if (chart.options.scales.y) {
                        chart.options.scales.y.grid.color = gridColor;
                        chart.options.scales.y.ticks.color = tickColor;
                    }
                }

                // Adjust legend color
                if (chart.options.plugins && chart.options.plugins.legend && chart.options.plugins.legend.labels) {
                    chart.options.plugins.legend.labels.color = isDark ? '#F1F5F9' : '#475569';
                }

                chart.update();
            }
        });
    }

}
window.toggleTheme = toggleTheme;

// =======================
// TOAST NOTIFICATIONS
// =======================
function showToast(title, body, type = 'success', duration = 5000, playSound = true) {
    const container = document.getElementById('toastContainer');
    if (!container) return;

    const toast = document.createElement('div');
    toast.className = `toast toast-${type}`;

    const icon = type === 'danger' ? '🔴' : type === 'warning' ? '🟡' : '🟢';

    toast.innerHTML = `
        <span>${icon}</span>
        <div>
            <div class="toast-title">${title}</div>
            <div class="toast-body">${body}</div>
        </div>
        <button class="toast-close" onclick="this.parentElement.remove()">✕</button>
    `;

    container.appendChild(toast);

    // Play notify sound
    if (type !== 'danger' && playSound) playNotify();

    // Auto dismiss
    setTimeout(() => {
        toast.classList.add('toast-exit');
        setTimeout(() => toast.remove(), 300);
    }, duration);
}

// =======================
// METAR UTILITIES
// =======================
function normalizeMetar(metar) {
    if (!metar) return "";
    // Remove trailing = (end of METAR marker)
    let clean = metar.replace(/=/g, "");
    // Normalize whitespace (multiple spaces -> single space)
    clean = clean.split(/\s+/).join(" ");
    // Uppercase and trim for consistency
    return clean.toUpperCase().trim();
}

// =======================
// METAR SYNTAX HIGHLIGHTING
// =======================
function highlightMetar(raw) {
    if (!raw) return '';

    const parts = raw.split(/\s+/);
    const highlighted = parts.map(part => {
        // Strip trailing '=' for matching, re-append after
        const hasEquals = part.endsWith('=');
        const cleanPart = hasEquals ? part.slice(0, -1) : part;
        const suffix = hasEquals ? '=' : '';

        // Station code (4 letter ICAO)
        if (/^[A-Z]{4}$/.test(cleanPart) && parts.indexOf(part) === 0) {
            return `<span class="metar-station">${cleanPart}</span>${suffix}`;
        }
        // Time (ddhhmmZ)
        if (/^\d{6}Z$/.test(cleanPart)) {
            return `<span class="metar-time">${cleanPart}</span>${suffix}`;
        }
        // Wind
        if (/KT$/.test(cleanPart)) {
            return `<span class="metar-wind">${cleanPart}</span>${suffix}`;
        }
        // Weather phenomena
        if (/^(\+|-|VC)?(TS|RA|SN|SH|FG|BR|HZ|DZ|GR|GS|SQ|FC|SA|DU|VA|FU|PO|SS|DS)/.test(cleanPart)) {
            return `<span class="metar-weather">${cleanPart}</span>${suffix}`;
        }
        // Clouds
        if (/^(FEW|SCT|BKN|OVC|NSC|NCD|CLR|SKC)/.test(cleanPart)) {
            return `<span class="metar-cloud">${cleanPart}</span>${suffix}`;
        }
        // Temperature / Dewpoint
        if (/^M?\d{2}\/M?\d{2}$/.test(cleanPart)) {
            return `<span class="metar-temp">${cleanPart}</span>${suffix}`;
        }
        // Pressure QNH
        if (/^Q\d{4}$/.test(cleanPart)) {
            return `<span class="metar-pressure">${cleanPart}</span>${suffix}`;
        }
        // Trend
        if (/^(NOSIG|TEMPO|BECMG)$/.test(cleanPart)) {
            return `<span class="metar-trend">${cleanPart}</span>${suffix}`;
        }
        return part;
    });

    return highlighted.join(' ');
}

/**
 * 🔥 METAR Validator Feature
 * Calls /api/validate to check the METAR string against 10 group rules
 */
async function runMetarValidation(raw, shouldPlayVoice = false) {
    const displayEl = document.getElementById('metarValidation');
    if (!displayEl || !raw) return;

    try {
        const response = await fetch('/api/validate', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ metar: raw })
        });
        const data = await response.json();
        const results = data.results || [];

        const isValid = results.length === 1 && results[0].startsWith('✅');
        const validationClass = isValid ? 'validation-success' : 'validation-error';

        // 🔥 VOICE ALERT FOR INVALID DATA (Only for new reports)
        if (!isValid && shouldPlayVoice) {
            console.warn('[VALIDATION] Invalid METAR detected on new update! Playing voice alert...');
            playValidationError();
        }

        let html = `
            <div class="validation-card ${validationClass}">
                <div style="font-weight: 700; font-size: 0.9rem; margin-bottom: 6px; display: flex; align-items: center; gap: 6px;">
                    <span>📋</span> Validation Results:
                </div>
                <ul style="list-style: none; padding: 0; margin: 0; font-family: 'JetBrains Mono', monospace; font-size: 0.8rem; line-height: 1.5;">
        `;

        results.forEach(err => {
            html += `<li style="margin-bottom: 2px;">${err}</li>`;
        });

        html += '</ul></div>';
        displayEl.innerHTML = html;
    } catch (err) {
        console.error('Validation API error:', err);
    }
}


// =======================
// DECODED PARAMETERS PANEL
// =======================
function updateDecodedPanel(raw) {
    if (!raw) return;

    // Extract temperature/dewpoint
    const tempMatch = raw.match(/\b(M?\d{2})\/(M?\d{2})\b/);
    if (tempMatch) {
        const temp = tempMatch[1].replace('M', '-');
        const dew = tempMatch[2].replace('M', '-');
        setParam('paramTemp', temp);
        setParam('paramDewpoint', dew);
    }

    // Extract wind
    const windMatch = raw.match(/\b(\d{3}|VRB)(\d{2,3})(G(\d{2,3}))?KT\b/);
    if (windMatch) {
        const windDirection = windMatch[1] === 'VRB' ? 'VRB' : parseInt(windMatch[1]);
        const windSpeed = parseInt(windMatch[2]);
        const windGust = windMatch[4] ? parseInt(windMatch[4]) : null;

        const windDisplay = windDirection === 'VRB' ? 'VRB' : `${windDirection}°`;
        setParam('paramWind', `${windDisplay}/${windSpeed}kt`);

        const gustText = windGust ? ` G${windGust}kt` : '';
        const detailEl = document.getElementById('paramWindDetail');
        if (detailEl) detailEl.textContent = gustText;
    }

    // Extract visibility
    const visParts = raw.replace('=', '').split(/\s+/);
    for (const p of visParts) {
        if (/^\d{4}$/.test(p) && !p.endsWith('Z')) {
            const vis = parseInt(p);
            setParam('paramVis', vis >= 9999 ? '10+' : (vis >= 1000 ? (vis / 1000).toFixed(vis % 1000 === 0 ? 0 : 1) : vis));
            const unitEl = document.getElementById('paramVisUnit');
            if (unitEl) unitEl.textContent = vis >= 1000 ? 'km' : 'm';

            // Update visibility bar
            const bar = document.getElementById('visBar');
            if (bar) {
                const pct = Math.min(100, (vis / 10000) * 100);
                bar.style.width = pct + '%';
                bar.className = 'vis-bar-fill ' + (vis >= 5000 ? 'vis-good' : vis >= 3000 ? 'vis-moderate' : 'vis-poor');
            }
            break;
        }
    }

    // Extract QNH
    const qnhMatch = raw.match(/Q(\d{4})/);
    if (qnhMatch) {
        setParam('paramQNH', qnhMatch[1]);
    }

    // Extract Cloud
    const cloudMatch = raw.match(/\b(FEW|SCT|BKN|OVC)(\d{3})(CB|TCU)?\b/);
    if (cloudMatch) {
        const height = parseInt(cloudMatch[2]) * 100;
        setParam('paramCloud', `${cloudMatch[1]} ${height}ft`);
        const extraEl = document.getElementById('paramCloudExtra');
        if (extraEl) extraEl.textContent = cloudMatch[3] ? cloudMatch[3] : '';
    }

    // Extract Weather
    const weatherCodes = ['\\+TSRA', '-TSRA', 'TSRA', '\\+TS', '-TS', 'TS', 'VCTS', '\\+RA', '-RA', 'RA', 'SH', 'FG', 'BR', 'HZ', 'DZ', 'SN', 'GR', 'SQ', 'DS', 'SS', 'FC'];
    let weatherFound = 'NIL';
    const weatherMap = {
        '+TSRA': '⛈️ Heavy Thunderstorm + Rain',
        '-TSRA': '🌩️ Light Thunderstorm + Rain',
        'TSRA': '⛈️ Thunderstorm + Rain',
        '+TS': '⛈️ Heavy Thunderstorm',
        '-TS': '🌩️ Light Thunderstorm',
        'TS': '⛈️ Thunderstorm',
        'VCTS': '🌩️ TS Vicinity',
        '+RA': '🌧️ Heavy Rain',
        '-RA': '🌦️ Light Rain',
        'RA': '🌧️ Rain',
        'SH': '🌦️ Showers',
        'FG': '🌫️ Fog',
        'BR': '🌫️ Mist',
        'HZ': '🌤️ Haze',
        'DZ': '🌧️ Drizzle',
        'SN': '❄️ Snow',
        'GR': '🧊 Hail',
        'SQ': '💨 Squall',
        'DS': '🌪️ Dust Storm',
        'SS': '🌪️ Sand Storm',
        'FC': '🌪️ Funnel Cloud'
    };

    for (const code of weatherCodes) {
        // Fix: Use a regex that allows space or start-of-line before codes that start with + or -
        const pattern = code.startsWith('\\+') || code.startsWith('-')
            ? '(?:^|\\s)' + code + '\\b'
            : '\\b' + code + '\\b';
        const regex = new RegExp(pattern, 'i');
        const cleanCode = code.replace(/\\/g, '');
        if (regex.test(raw)) {
            weatherFound = weatherMap[cleanCode] || cleanCode;
            break;
        }
    }
    setParam('paramWeather', weatherFound);

    // Styling khusus untuk kabut
    const weatherEl = document.getElementById('paramWeather');
    if (weatherEl) {
        if (/\b(FG|HZ|BR)\b/.test(raw)) {
            weatherEl.style.color = '#64748b'; // Abu-abu untuk kabut
            weatherEl.style.fontStyle = 'italic';
        } else {
            weatherEl.style.color = '';
            weatherEl.style.fontStyle = '';
        }
    }

    // Time
    const timeMatch = raw.match(/(\d{2})(\d{2})(\d{2})Z/);
    if (timeMatch) {
        setParam('paramTime', `${timeMatch[2]}:${timeMatch[3]} UTC`);
    }

    // Update Thunderstorm module
    updateThunderstormModule(raw);

    // Check for Rain effect
    checkRainStatus(raw);
}

// =======================
// RAIN EFFECT
// =======================
function checkRainStatus(raw) {
    if (!raw) return;

    console.log('[RAIN] Checking rain status for:', raw.substring(0, 60));

    // Fix: Updated regex to properly handle +/- prefixes by using (?:\s|^) instead of leading \b
    const rainRegex = /(?:^|\s)([\+\-]|VC)?(RA|DZ|SHRA|TSRA|SH|SN|SG|GR|GS|PL|IC|UP)\b/i;
    const match = raw.match(rainRegex);
    const hasRain = !!match;

    console.log('[RAIN] Regex match result:', hasRain, match ? match[0].trim() : 'no match');

    const rainIndicator = document.getElementById('rainIndicator');
    const rainIndicatorText = document.getElementById('rainIndicatorText');

    if (hasRain) {
        console.log('[RAIN] Rain detected! Activating effects...');
        document.body.classList.add('rain-active');
        makeItRain();

        if (rainIndicator) {
            rainIndicator.classList.add('active');
            if (rainIndicatorText) {
                const code = match[0].trim().toUpperCase();
                let statusText = 'RAIN DETECTED';
                if (code.includes('TS')) statusText = 'THUNDERSTORM';
                else if (code.includes('DZ')) statusText = 'DRIZZLE';
                else if (code.includes('SH')) statusText = 'SHOWERS';
                rainIndicatorText.textContent = statusText;
            }
        }
    } else {
        document.body.classList.remove('rain-active');
        stopRain();
        if (rainIndicator) rainIndicator.classList.remove('active');
    }
}

function makeItRain() {
    let frontRow = document.getElementById('rainFront');
    let backRow = document.getElementById('rainBack');

    // Auto-create containers if not found in DOM
    if (!frontRow) {
        console.log('[RAIN] Creating rainFront container dynamically');
        frontRow = document.createElement('div');
        frontRow.id = 'rainFront';
        frontRow.className = 'rain front-row';
        document.body.insertBefore(frontRow, document.body.firstChild);
    }
    if (!backRow) {
        console.log('[RAIN] Creating rainBack container dynamically');
        backRow = document.createElement('div');
        backRow.id = 'rainBack';
        backRow.className = 'rain back-row';
        document.body.insertBefore(backRow, document.body.firstChild);
    }

    // If rain is already falling, don't recreate it
    if (frontRow.children.length > 0) return;

    console.log('[RAIN] Generating realistic rain drops...');
    let increment = 0;
    let drops = "";
    let backDrops = "";

    // Realistic density loop as requested
    while (increment < 100) {
        // Menghasilkan angka acak untuk variasi
        const randoHundo = Math.floor(Math.random() * 98) + 1;
        const randoFiver = Math.floor(Math.random() * 4) + 2; // angka acak antara 2 dan 5
        increment += randoFiver;

        // Membuat tetesan untuk lapisan depan
        drops += `<div class="drop" style="left: ${increment}%; bottom: ${randoFiver + randoFiver - 1 + 100}%; animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;">
                    <div class="stem" style="animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;"></div>
                    <div class="splat" style="animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;"></div>
                  </div>`;

        // Membuat tetesan yang sesuai untuk lapisan belakang
        backDrops += `<div class="drop" style="right: ${increment}%; bottom: ${randoFiver + randoFiver - 1 + 100}%; animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;">
                        <div class="stem" style="animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;"></div>
                        <div class="splat" style="animation-delay: 0.${randoHundo}s; animation-duration: 0.5${randoHundo}s;"></div>
                      </div>`;
    }

    frontRow.innerHTML = drops;
    backRow.innerHTML = backDrops;
    console.log('[RAIN] Realistic rain effect activated! Drops:', frontRow.children.length);
}

function stopRain() {
    const frontRow = document.getElementById('rainFront');
    const backRow = document.getElementById('rainBack');
    if (frontRow) frontRow.innerHTML = '';
    if (backRow) backRow.innerHTML = '';
}

function setParam(id, value) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = value;
    el.classList.add('data-flash');
    setTimeout(() => el.classList.remove('data-flash'), 600);
}

// =======================
// THUNDERSTORM MODULE
// =======================
function updateThunderstormModule(raw) {
    const tsStatus = document.getElementById('tsStatus');
    const tsCB = document.getElementById('tsCB');
    const tsInMetar = document.getElementById('tsInMetar');
    const tsLightning = document.getElementById('tsLightning');
    const tsStatusText = document.getElementById('tsStatusText');
    const tsRadar = document.getElementById('tsRadar');

    if (!tsStatus) return;

    const tsCodes = ['TS', 'TSRA', 'VCTS', '+TS', 'TSGR', '-TS', '+TSRA', '-TSRA'];
    const hasTS = tsCodes.some(code => raw.includes(code));
    const hasCB = /CB\b/.test(raw);

    if (hasTS) {
        tsStatus.className = 'ts-status active';
        tsStatus.innerHTML = '<span class="lightning-icon">⚡</span> ACTIVE THUNDERSTORM DETECTED';

        // Add lightning icon to radar
        const existingIcon = tsRadar.querySelector('.ts-icon');
        if (!existingIcon) {
            const icon = document.createElement('span');
            icon.className = 'ts-icon';
            icon.textContent = '⚡';
            icon.style.top = '25%';
            icon.style.left = '60%';
            tsRadar.appendChild(icon);
        }
    } else {
        tsStatus.className = 'ts-status clear';
        tsStatus.innerHTML = '<span>✅</span> No Thunderstorm Detected';

        const existingIcon = tsRadar.querySelector('.ts-icon');
        if (existingIcon) existingIcon.remove();
    }

    if (tsCB) tsCB.textContent = hasCB ? 'Yes' : 'No';
    if (tsInMetar) tsInMetar.textContent = hasTS ? 'Yes' : 'No';
    if (tsLightning) tsLightning.textContent = hasTS ? 'Possible' : '--';
    if (tsStatusText) tsStatusText.textContent = hasTS ? 'ACTIVE' : 'Clear';

    const tsLastUpdate = document.getElementById('tsLastUpdate');
    if (tsLastUpdate) {
        const now = new Date();
        const utcStr = now.getUTCHours().toString().padStart(2, '0') + ':' +
            now.getUTCMinutes().toString().padStart(2, '0') + ' UTC';
        tsLastUpdate.textContent = `Last Update: ${utcStr}`;
    }
}

// =======================
// DATA HANDLER (Formerly Socket Handler) - FIXED ANTI-SPAM ALARM
// =======================
function handleMetarUpdate(data) {
    console.log('Processing data update:', data);

    // Buat hash dari METAR untuk cek duplikat
    const currentHash = hashMetar(data.raw);
    const isDuplicate = currentHash && currentHash === alarmState.lastMetarHash;
    const isFirstLoad = alarmState.lastMetarHash === null;

    // 🔥 DATA FLIP-FLOP GUARD: 
    // Jika data yang datang lebih lama (older) dari yang sudah kita display, abaikan.
    if (data.last_update && alarmState.lastProcessedServerTime) {
        const incomingTime = new Date(data.last_update).getTime();
        const existingTime = new Date(alarmState.lastProcessedServerTime).getTime();

        if (incomingTime < existingTime) {
            console.warn('[SYNC] 🛑 Stale data detected (incoming time earlier than existing). Ignoring update.');
            return;
        }
    }

    // Update global state
    lastMetarRaw = data.raw;
    if (data.metar_status) lastMetarStatus = data.metar_status;
    if (data.visibility_m !== undefined) lastVisibility = data.visibility_m;

    // Cek kondisi berbahaya
    const tsCodes = ['TS', 'TSRA', 'VCTS', '+TS', 'TSGR'];
    const hasTS = data.raw ? tsCodes.some(c => data.raw.includes(c)) : false;
    lastHasTS = hasTS;

    const vis = data.visibility_m !== undefined ? data.visibility_m : null;
    const isLowVis = vis !== null && vis < 3000;

    // ============================================================
    // LOGIKA ANTI-SPAM: Jika data sama persis, hanya update UI tanpa alarm
    // ============================================================
    if (isDuplicate && !isFirstLoad) {
        console.log('[SYNC] Data duplicate detected, skipping alarm but refreshing UI components');
        updateConnectionIndicator(true);

        // Tetap update status panel jika ada
        if (data.auto_fetch !== undefined) {
            updateStatusPanel(data);
        }

        // Update last update time tracking anyway
        if (data.last_update) alarmState.lastProcessedServerTime = data.last_update;
        saveAlarmState();

        // 🔥 SKIP ALARM LOGIC FOR DUPLICATES
        return;
    }

    // ============================================================
    // DATA BARU ATAU BERUBAH - Proses alarm dengan logika yang benar
    // ============================================================

    // Update hash untuk tracking berikutnya
    alarmState.lastMetarHash = currentHash;
    alarmState.lastUpdateTime = Date.now();
    if (data.last_update) {
        alarmState.lastProcessedServerTime = data.last_update;
    }
    saveAlarmState(); // 🔥 COMMIT TO LOCALSTORAGE

    // Cek apakah kondisi berbahaya BARU muncul (transisi dari aman ke berbahaya)
    const isNewLowVis = isLowVis && !alarmState.lowVisTriggered;
    const isNewThunderstorm = hasTS && !alarmState.thunderstormTriggered;

    // Variable to prevent overlapping sounds
    let alarmPlayedThisCycle = false;

    // 1. Alarm untuk Low Visibility yang BARU terdeteksi
    if (isNewLowVis) {
        console.log('[ALARM] New low visibility detected!');
        document.body.classList.add('low-visibility');

        if (!alarmPlayedThisCycle) {
            playAlarm();
            alarmPlayedThisCycle = true;
        }

        showToast('⚠️ LOW VISIBILITY', `Visibility reduced to ${vis}m`, 'danger', 10000);
        alarmState.lowVisTriggered = true;
        saveAlarmState(); // 🔥 PERSIST
    }
    // Jika visibility sudah normal, reset flag
    else if (!isLowVis && alarmState.lowVisTriggered) {
        alarmState.lowVisTriggered = false;
        document.body.classList.remove('low-visibility');
        saveAlarmState(); // 🔥 PERSIST
    }

    // 2. Alarm untuk Thunderstorm yang BARU terdeteksi
    if (isNewThunderstorm) {
        console.log('[ALARM] New thunderstorm detected!');
        showToast('⚡ THUNDERSTORM', 'Thunderstorm detected in METAR', 'danger', 10000);

        if (!alarmPlayedThisCycle) {
            playAlarm();
            alarmPlayedThisCycle = true;
        }

        alarmState.thunderstormTriggered = true;
        saveAlarmState(); // 🔥 PERSIST
    }
    // Jika TS sudah tidak ada, reset flag
    else if (!hasTS && alarmState.thunderstormTriggered) {
        alarmState.thunderstormTriggered = false;
        saveAlarmState(); // 🔥 PERSIST
    }

    // 3. Notifikasi suara untuk data baru (mencegah tabrakan alarm & notify)
    // Anti-Duplikasi: Hanya bunyikan alarm/notif jika isi METAR benar-benar berubah
    const isMetarChanged = data.raw && (!window.lastProcessedMetar || normalizeMetar(data.raw) !== normalizeMetar(window.lastProcessedMetar));

    let allowToastSound = true;
    if (!isFirstLoad && soundEnabled && data.status === 'new' && isMetarChanged) {
        if (hasTS) {
            if (!alarmPlayedThisCycle) {
                playAlarm();
                alarmPlayedThisCycle = true;
            }
            allowToastSound = false;
        } else if (alarmPlayedThisCycle) {
            allowToastSound = false;
        }
    } else if (data.status !== 'new' || !isMetarChanged) {
        // Jika data duplikat atau tidak berubah, jangan bunyikan notif apa pun
        allowToastSound = false;
    }

    // ============================================================
    // UI UPDATES (hanya jika data berbeda atau first load)
    // ============================================================

    // Show toast untuk data baru (hanya jika konten METAR berubah)
    if (!isFirstLoad && data.raw && data.status === 'new' && isMetarChanged) {
        showToast('New METAR Received', `${STATION} — ${new Date().toISOString().slice(11, 19)} UTC`, 'success', 5000, allowToastSound);

        // 🔔 TRIGGER NOTIFIKASI SUARA UNTUK DATA BARU
        if (typeof playNotifySound === 'function') {
            playNotifySound();
        }
    }

    // Simpan data terakhir untuk perbandingan di polling berikutnya
    if (data.raw) {
        window.lastProcessedMetar = data.raw;
    }

    // Update METAR raw display
    if (data.raw) {
        const rawEl = document.getElementById('metarRawCode');
        if (rawEl) {
            // Trim the data.raw before checking/appending to prevent trailing spaces before '='
            const trimmedRaw = data.raw.trim();
            const displayRaw = trimmedRaw.endsWith('=') ? trimmedRaw : trimmedRaw + '=';
            let htmlStr = highlightMetar(displayRaw);
            // Badge untuk COR/AMD/SPECI
            if (data.raw.includes(' COR ') || data.raw.includes('METAR COR') || data.raw.includes('CCA')) {
                htmlStr += ' <span class="badge" style="background-color: #f59e0b; color: #1e293b; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ CORRECTION</span>';
            } else if (data.raw.includes(' AMD ') || data.raw.includes('METAR AMD')) {
                htmlStr += ' <span class="badge" style="background-color: #3b82f6; color: #ffffff; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ AMD</span>';
            } else if (data.raw.includes(' SPECI ') || data.raw.startsWith('SPECI ')) {
                htmlStr += ' <span class="badge" style="background-color: #ef4444; color: #ffffff; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ SPECI</span>';
            }
            rawEl.innerHTML = htmlStr;
        }

        // Update panel status color
        const panel = document.getElementById('metarRawPanel');
        if (panel) {
            panel.classList.remove('status-danger', 'status-warning');
            if (hasTS || isLowVis) {
                panel.classList.add('status-danger');
            } else if (vis !== null && vis <= 5000) {
                panel.classList.add('status-warning');
            }
        }

        updateDecodedPanel(data.raw);

        // 🔔 Update Stale Data Monitor with fresh METAR
        if (typeof metarStaleMonitor !== 'undefined') {
            metarStaleMonitor.updateFromMetar(data.raw);
        }

        // 🔥 AKTIFKAN EFEK KABUT JIKA ADA FG/HZ/BR
        if (typeof updateFogEffect === 'function') {
            if (!window.isManualSession) updateFogEffect(data);
        }
    }

    // Update QAM dan Narrative
    if (data.qam) {
        const qamEl = document.getElementById('qamDisplay');
        if (qamEl) qamEl.textContent = data.qam;
    }

    if (data.narrative) {
        const narEl = document.getElementById('narrativeDisplay');
        if (narEl) narEl.textContent = data.narrative;
    }

    // Update last saved time
    const now = new Date();
    const formatted = now.toLocaleDateString('id-ID') + ' ' +
        now.getHours().toString().padStart(2, '0') + ':' +
        now.getMinutes().toString().padStart(2, '0') + ':' +
        now.getSeconds().toString().padStart(2, '0');

    const lastSaved = document.getElementById('lastSaved');
    if (lastSaved) lastSaved.textContent = formatted;

    // Refresh ALL UI Components in sync
    if (typeof loadHistory === 'function') loadHistory(); // Updates Charts
    if (typeof updateHistoryTable === 'function') updateHistoryTable(); // Updates Table

    // updateMiniTimeline(); // 🔥 DISABLED

    // Run validation (trigger voice alert only if it's a new unique record)
    const shouldVoiceOnFailure = !isFirstLoad && data.status === 'new' && isMetarChanged;
    runMetarValidation(data.raw, shouldVoiceOnFailure);

    // Update status panel
    if (data.auto_fetch !== undefined) {
        updateStatusPanel(data);
    }

    // Update connection indicator
    updateConnectionIndicator(true);
}

// =======================
// SYSTEM CONTROL
// =======================
function updateStatusPanel(data) {
    const serverEl = document.getElementById('server-status');
    const metarEl = document.getElementById('metar-status');
    const lastEl = document.getElementById('last-update-status');

    if (serverEl) serverEl.textContent = "ONLINE 🟢";
    if (metarEl) {
        metarEl.textContent = data.auto_fetch ? "RUNNING 🟢" : "PAUSED 🟡";
        metarEl.className = 'status-value ' + (data.auto_fetch ? 'status-running' : 'status-paused');
    }

    if (lastEl) {
        if (data.last_update) {
            const date = new Date(data.last_update);
            // Strictly UTC for time display
            const timeStr = date.getUTCHours().toString().padStart(2, '0') + ':' +
                date.getUTCMinutes().toString().padStart(2, '0') + ' UTC';

            // 🔥 DATA FRESHNESS INDICATOR
            const ageMs = Date.now() - date.getTime();
            const ageMins = Math.floor(ageMs / 60000);

            let freshnessIndicator = '';
            if (ageMins < 5) {
                freshnessIndicator = ' <span style="color:#10b981;font-weight:bold;">( LIVE )</span>';
            } else if (ageMins < 60) {
                freshnessIndicator = ` <span style="color:#f59e0b;font-weight:bold;">( ${ageMins}m ago )</span>`;
            } else {
                freshnessIndicator = ` <span style="color:#ef4444;font-weight:bold;">( ${ageMins}m ago )</span>`;
            }

            lastEl.innerHTML = `${timeStr}${freshnessIndicator}`;
        } else {
            lastEl.textContent = "WAITING...";
        }
    }
}

async function toggleSystem() {
    try {
        const res = await fetch("/api/toggle_fetch", { method: "POST" });
        const data = await res.json();

        // Update local state and persistence
        autoFetchEnabled = data.auto_fetch;
        localStorage.setItem('autoFetchEnabled', autoFetchEnabled);

        updateStatusPanel(data);
        showToast(
            'System Control',
            `Auto fetch: ${data.auto_fetch ? 'ENABLED ✅' : 'DISABLED ⏸️'}`,
            data.auto_fetch ? 'success' : 'warning'
        );
    } catch (err) {
        console.error("Toggle system failed:", err);
        showToast('Error', 'Failed to toggle system', 'danger');
    }
}

// Global expose
window.toggleSystem = toggleSystem;

// Initial health check
fetch("/health")
    .then(r => r.json())
    .then(updateStatusPanel)
    .catch(() => console.warn("Initial health check failed"));

// =======================
// ALERT BANNER
// =======================
function showAlert(title, message, level = 'critical') {
    const banner = document.getElementById('alertBanner');
    if (!banner) return;
    banner.className = `alert-banner visible alert-${level}`;
    document.getElementById('alertTitle').textContent = title;
    document.getElementById('alertMessage').textContent = message;
}

function dismissAlert() {
    const banner = document.getElementById('alertBanner');
    if (banner) banner.classList.remove('visible');
}

// =======================
// COPY FUNCTIONS
// =======================
function copyMetar() {
    const el = document.getElementById('metarRawCode');
    if (!el) return;
    navigator.clipboard.writeText(el.textContent).then(() => {
        const fb = document.getElementById('metarCopyFeedback');
        if (fb) { fb.classList.add('visible'); setTimeout(() => fb.classList.remove('visible'), 2000); }
    });
}

function copyQam() {
    const el = document.getElementById('qamDisplay');
    if (!el) return;
    // Wrap in triple backticks so WhatsApp renders it in monospace font
    // This ensures the ":" alignment is preserved in proportional-font apps
    const rawText = el.textContent.trim();
    const wrappedText = '```\n' + rawText + '\n```';
    navigator.clipboard.writeText(wrappedText).then(() => {
        const fb = document.getElementById('qamCopyFeedback');
        if (fb) { fb.classList.add('visible'); setTimeout(() => fb.classList.remove('visible'), 2000); }
    });
}

window.copyMetar = copyMetar;
window.copyQam = copyQam;

// =======================
// SOUND FUNCTIONS
// =======================
function enableSound() {
    soundEnabled = !soundEnabled;
    const btnHeader = document.getElementById('soundToggle');
    const btnSidebar = document.getElementById('soundToggleSidebar');
    const iconSidebar = document.getElementById('soundIconSidebar');
    const labelSidebar = document.getElementById('soundLabelSidebar');

    // Save state to localStorage
    localStorage.setItem('soundEnabled', soundEnabled);

    // Update Header
    if (btnHeader) {
        btnHeader.textContent = soundEnabled ? '🔊 Sound ON' : '🔇 Sound OFF';
        if (soundEnabled) btnHeader.classList.add('active');
        else btnHeader.classList.remove('active');
    }

    // Update Sidebar
    if (btnSidebar) {
        if (iconSidebar) iconSidebar.textContent = soundEnabled ? '🔊' : '🔇';
        if (labelSidebar) labelSidebar.textContent = soundEnabled ? 'Sound ON' : 'Sound OFF';
        if (soundEnabled) btnSidebar.classList.add('active');
        else btnSidebar.classList.remove('active');
    }

    if (soundEnabled) {
        console.log('Sound ENABLED');

        // Immediate check: If there's an active critical condition, play alarm now
        if (lastVisibility !== null && lastVisibility < 3000 || lastHasTS || lastMetarStatus === 'danger') {
            console.log('Immediate alert: critical condition detected on activation');
            playAlarm();
        }
    } else {
        console.log('Sound DISABLED');
    }
}
window.enableSound = enableSound;

function playAlarm() {
    if (!soundEnabled) {
        console.log('Sound disabled, skipping alarm');
        return;
    }
    const a = document.getElementById('lowVisSound');
    if (a) {
        a.currentTime = 0;
        a.play()
            .then(() => console.log('Alarm played'))
            .catch(error => {
                console.error('Failed to play alarm:', error);
                // Try to load and play again
                a.load();
                a.play().catch(e => console.error('Retry also failed:', e));
            });
    } else {
        console.error('Alarm audio element not found');
    }
}

function playNotify() {
    if (!soundEnabled) {
        console.log('Sound disabled, skipping notify');
        return;
    }
    const a = document.getElementById('newDataSound');
    if (a) {
        a.currentTime = 0;
        a.play()
            .then(() => console.log('Notify sound played'))
            .catch(error => {
                console.error('Failed to play notify sound:', error);
                // Try loading the audio again
                a.load();
                a.play().catch(e => console.warn('Retry also failed:', e));
            });
    } else {
        console.error('Notify audio element not found');
    }
}

function playValidationError() {
    if (!soundEnabled) {
        console.log('Sound disabled, skipping validation alert');
        return;
    }
    const a = document.getElementById('validationErrorSound');
    if (a) {
        a.currentTime = 0;
        a.play()
            .then(() => console.log('Validation error sound played'))
            .catch(error => {
                console.error('Failed to play validation sound:', error);
                a.load();
                a.play().catch(e => console.warn('Retry failed:', e));
            });
    } else {
        console.error('Validation audio element not found');
    }
}

function playStaleAlarm() {
    if (!soundEnabled) {
        console.log('Sound disabled, skipping stale alarm');
        return;
    }
    const a = document.getElementById('staleDataSound');
    if (a) {
        a.currentTime = 0;
        a.play()
            .then(() => console.log('Stale METAR alarm played'))
            .catch(error => {
                console.error('Failed to play stale alarm:', error);
                a.load();
                a.play().catch(e => console.warn('Retry failed:', e));
            });
    } else {
        console.error('Stale alarm audio element not found');
    }
}

// =======================
// CHARTS (Light Theme)
// =======================
const chartColors = {
    tempLine: '#DC2626',
    tempFill: 'rgba(220, 38, 38, 0.08)',
    pressureLine: '#2E5C8A',
    pressureFill: 'rgba(46, 92, 138, 0.08)',
    windLine: '#E8B339',
    windFill: 'rgba(232, 179, 57, 0.1)',
    gustLine: '#059669',
    gustFill: 'rgba(5, 150, 105, 0.08)',
    tickColor: '#64748B',
    refLine: 'rgba(30, 58, 95, 0.3)',
    legendColor: '#475569'
};

function getDynamicChartColors() {
    const isDark = currentTheme === 'dark';
    return {
        grid: isDark ? 'rgba(255, 255, 255, 0.1)' : 'rgba(0, 0, 0, 0.06)',
        ticks: isDark ? '#94A3B8' : '#64748B',
        legend: isDark ? '#F1F5F9' : '#475569'
    };
}

function chartDefaults() {
    return {
        responsive: true,
        maintainAspectRatio: false,
        animation: { duration: 800, easing: 'easeOutQuart' },
        interaction: { mode: 'index', intersect: false },
        plugins: {
            legend: {
                labels: {
                    font: { family: 'Inter', weight: '600', size: 12 },
                    color: getDynamicChartColors().legend,
                    usePointStyle: true,
                    padding: 28 // Increased from 16 for better margin
                }
            },
            tooltip: {
                backgroundColor: '#1E3A5F',
                titleFont: { family: 'Inter', size: 13, weight: '700' },
                bodyFont: { family: 'JetBrains Mono', size: 12 },
                padding: 12,
                cornerRadius: 8,
                borderColor: 'rgba(232,179,57,0.3)',
                borderWidth: 1,
                callbacks: {
                    title: function(context) {
                        let title = context[0].label || '';
                        if (title && !title.includes('UTC')) {
                            return title + ' UTC';
                        }
                        return title;
                    }
                }
            }
        },
        layout: {
            padding: {
                top: 10 // Extra room above the legend
            }
        },
        scales: {
            y: {
                beginAtZero: false,
                grace: '10%', // Prevents chart data from touching the top edge
                grid: { color: getDynamicChartColors().grid, drawBorder: false },
                ticks: { color: getDynamicChartColors().ticks, font: { family: 'Inter', weight: '600', size: 11 } }
            },
            x: {
                grid: { display: false },
                ticks: {
                    color: getDynamicChartColors().ticks,
                    font: { family: 'Inter', size: 10 },
                    maxTicksLimit: 10,
                    maxRotation: 45,
                    callback: function(value, index, values) {
                        let label = this.getLabelForValue(value);
                        if (label && !label.includes('UTC')) {
                            return label + ' UTC';
                        }
                        return label;
                    }
                }
            }
        }
    };
}

function createCharts() {
    const tempCanvas = document.getElementById('tempChart');
    const pressureCanvas = document.getElementById('pressureChart');
    if (!tempCanvas || !pressureCanvas) return;

    if (tempChart) tempChart.destroy();
    if (pressureChart) pressureChart.destroy();

    tempChart = new Chart(tempCanvas.getContext('2d'), {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'Temperature (°C)',
                data: [],
                borderColor: chartColors.tempLine,
                backgroundColor: chartColors.tempFill,
                borderWidth: 2.5,
                tension: 0.4,
                fill: true,
                pointRadius: 3,
                pointHoverRadius: 6,
                pointBackgroundColor: chartColors.tempLine
            }]
        },
        options: chartDefaults()
    });

    pressureChart = new Chart(pressureCanvas.getContext('2d'), {
        type: 'line',
        data: {
            labels: [],
            datasets: [{
                label: 'QNH (hPa)',
                data: [],
                borderColor: chartColors.pressureLine,
                backgroundColor: chartColors.pressureFill,
                borderWidth: 2.5,
                tension: 0.4,
                fill: true,
                pointRadius: 3,
                pointHoverRadius: 6,
                pointBackgroundColor: chartColors.pressureLine
            }]
        },
        options: chartDefaults()
    });
}

function createWindChart() {
    const canvas = document.getElementById('windChart');
    if (!canvas) return;
    if (windChart) windChart.destroy();

    windChart = new Chart(canvas.getContext('2d'), {
        type: 'line',
        data: {
            labels: [],
            datasets: [
                {
                    label: 'Wind Speed (KT)',
                    data: [],
                    borderColor: chartColors.windLine,
                    backgroundColor: chartColors.windFill,
                    borderWidth: 2.5,
                    tension: 0.4,
                    fill: true,
                    pointRadius: 3,
                    pointHoverRadius: 6,
                    pointBackgroundColor: chartColors.windLine
                },
                {
                    label: 'Wind Gust (KT)',
                    data: [],
                    borderColor: chartColors.gustLine,
                    backgroundColor: chartColors.gustFill,
                    borderWidth: 2,
                    borderDash: [5, 5],
                    tension: 0.4,
                    fill: true,
                    spanGaps: true,
                    pointRadius: 3,
                    pointHoverRadius: 5,
                    pointBackgroundColor: chartColors.gustLine,
                    hidden: true
                }
            ]
        },
        options: {
            ...chartDefaults(),
            scales: {
                ...chartDefaults().scales,
                y: {
                    ...chartDefaults().scales.y,
                    beginAtZero: true,
                    grace: '10%', // Provides headroom above top line
                    title: {
                        display: true,
                        text: 'Speed (KT)',
                        color: '#475569',
                        font: { family: 'Inter', weight: '700' }
                    }
                }
            }
        }
    });
}

// =======================
// FETCH & UPDATE CHARTS
// =======================
async function loadHistory() {
    // Only run on main dashboard
    if (window.location.pathname !== '/' && window.location.pathname !== '/metar') return;

    // 🔥 Jika sedang melihat data 'yesterday', jangan timpa grafik dengan data polling terbaru
    if (typeof currentView !== 'undefined' && currentView !== 'today') {
        console.log('[CHART] Skipping history update - current view is not "today"');
        return;
    }

    console.log('[CHART] Requesting history update...');
    try {
        const res = await fetch('/api/metar/history');
        const result = await res.json();

        if (!result.data || result.data.length === 0) {
            console.warn('[CHART] No history data received');
            return;
        }

        console.log(`[CHART] History loaded: ${result.data.length} records`);

        const labels = result.labels || result.data.map(r => r.time);
        const temps = result.temps || result.data.map(r => r.temp);
        const pressures = result.pressures || result.data.map(r => r.pressure);
        const winds = result.data.map(r => r.wind);
        const gusts = result.data.map(r => r.gust);

        if (!tempChart || !pressureChart) {
            console.log('[CHART] Initializing charts...');
            createCharts();
        }
        if (!windChart) createWindChart();

        // Update datasets
        if (tempChart && tempChart.data) {
            tempChart.data.labels = labels;
            tempChart.data.datasets[0].data = temps;
            tempChart.update('active');
        }
        if (pressureChart && pressureChart.data) {
            pressureChart.data.labels = labels;
            pressureChart.data.datasets[0].data = pressures;
            pressureChart.update('active');
        }
        if (windChart && windChart.data) {
            windChart.data.labels = labels;
            windChart.data.datasets[0].data = winds;
            if (windChart.data.datasets[1]) {
                windChart.data.datasets[1].data = gusts;
            }
            windChart.update('active');
        }

        // Update data summary indicators (footers)
        const infoText = `${result.range.start} to ${result.range.end} • ${result.count} records (from ${result.source})`;
        ['tempChart-info', 'pressureChart-info', 'windChart-info'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.textContent = infoText;
        });

        // Trigger min/max indicators update
        updateChartMinMax(labels, temps, pressures, winds, gusts);

        console.log('[CHART] Charts successfully updated with summary info');
    } catch (e) {
        console.error('[CHART] Error updating charts:', e);
    }
}

// Alias for compatibility
function updateCharts(labels, temps, pressures, winds, gusts) {
    if (labels && temps) {
        // Initial manual load from template data
        if (!tempChart && document.getElementById('tempChart')) createCharts();
        if (!windChart && document.getElementById('windChart')) createWindChart();

        if (tempChart) {
            tempChart.data.labels = labels;
            tempChart.data.datasets[0].data = temps;
            tempChart.update();
        }
        if (pressureChart) {
            pressureChart.data.labels = labels;
            pressureChart.data.datasets[0].data = pressures;
            pressureChart.update();
        }
        if (windChart) {
            windChart.data.labels = labels;
            windChart.data.datasets[0].data = winds;
            if (windChart.data.datasets[1] && gusts) {
                windChart.data.datasets[1].data = gusts;
            }
            windChart.update();
        }
        // Trigger min/max indicators update
        updateChartMinMax(labels, temps, pressures, winds, gusts);
    } else {
        loadHistory();
    }
}
window.updateCharts = updateCharts;

let currentChartPeriod = 'today';
let currentChartYear = new Date().getUTCFullYear();

async function loadChartPeriod(period = 'today', year = currentChartYear) {
    const yearSelect = document.getElementById('chart-year');
    if (!yearSelect) return;

    currentChartPeriod = period;
    if (period === 'year') {
        currentChartYear = Number(year || (yearSelect && yearSelect.value));
    }

    document.querySelectorAll('.chart-period-button').forEach(button => button.classList.remove('active'));
    const activeButton = document.getElementById(`btn-${period}`);
    if (activeButton) activeButton.classList.add('active');

    const query = new URLSearchParams({ period });
    if (period === 'year') query.set('year', String(currentChartYear));

    try {
        const response = await fetch(`/api/charts/data?${query.toString()}`);
        const data = await response.json();
        if (!response.ok) throw new Error(data.error || 'Failed to load chart data');

        const infoText = `${data.range.start} to ${data.range.end} • ${data.count} records (from ${data.source})`;

        const displayDate = document.getElementById('display-date');
        if (displayDate) displayDate.textContent = data.title;

        updateCharts(data.labels, data.temps, data.pressures, data.winds, data.gusts);
        ['tempChart-info', 'pressureChart-info', 'windChart-info'].forEach(id => {
            const element = document.getElementById(id);
            if (element) element.textContent = infoText;
        });

    } catch (error) {
        console.error('[CHART] Period data error:', error);
        ['tempChart-info', 'pressureChart-info', 'windChart-info'].forEach(id => {
            const element = document.getElementById(id);
            if (element) element.textContent = 'Gagal memuat data untuk periode ini.';
        });
    }
}
window.loadChartPeriod = loadChartPeriod;

function initializeChartPeriodControls() {
    const yearSelect = document.getElementById('chart-year');
    if (!yearSelect) return;

    const currentYear = new Date().getUTCFullYear();
    yearSelect.replaceChildren();
    for (let year = 2011; year <= currentYear; year += 1) {
        const option = document.createElement('option');
        option.value = String(year);
        option.textContent = String(year);
        yearSelect.appendChild(option);
    }
    yearSelect.value = String(currentYear);
    currentChartYear = currentYear;
    loadChartPeriod('today');
}

// =======================
// CHART MIN/MAX INDICATORS
// =======================
/**
 * Calculate and display min/max indicators for all charts.
 * Called after chart data is updated.
 * @param {string[]} labels - Time labels
 * @param {number[]} temps - Temperature values
 * @param {number[]} pressures - Pressure values
 * @param {number[]} winds - Wind speed values
 * @param {number[]} gusts - Wind gust values (optional)
 */
function updateChartMinMax(labels, temps, pressures, winds, gusts) {
    // Helper: find min/max with index from numeric array
    function findMinMax(dataArr, labelArr) {
        const validPairs = [];
        for (let i = 0; i < dataArr.length; i++) {
            const val = parseFloat(dataArr[i]);
            if (!isNaN(val) && dataArr[i] !== null && dataArr[i] !== undefined) {
                validPairs.push({ value: val, label: labelArr[i] || '-', index: i });
            }
        }
        if (validPairs.length === 0) return null;

        let minItem = validPairs[0], maxItem = validPairs[0];
        for (const item of validPairs) {
            if (item.value < minItem.value) minItem = item;
            if (item.value > maxItem.value) maxItem = item;
        }
        return { min: minItem, max: maxItem };
    }

    // Helper: render a min/max row into a container
    function renderMinMaxRow(containerId, result, unit, lowIcon, highIcon, invertColors = false) {
        const container = document.getElementById(containerId);
        if (!container) return;

        if (!result) {
            container.innerHTML = '<div style="padding: 8px 14px; color: var(--text-muted); font-size: 0.85rem;">No data available</div>';
            container.classList.remove('loading');
            return;
        }

        const minTimeLabel = result.min.label ? `at ${result.min.label}` : '';
        const maxTimeLabel = result.max.label ? `at ${result.max.label}` : '';

        const lowClass = invertColors ? 'minmax-high' : 'minmax-low';
        const highClass = invertColors ? 'minmax-low' : 'minmax-high';

        container.innerHTML = `
            <div class="chart-minmax-item ${lowClass}">
                <div class="minmax-icon">${lowIcon}</div>
                <div class="minmax-content">
                    <span class="minmax-label">Terendah</span>
                    <span class="minmax-value">${result.min.value} ${unit}</span>
                    <span class="minmax-time">${minTimeLabel}</span>
                </div>
            </div>
            <div class="chart-minmax-item ${highClass}">
                <div class="minmax-icon">${highIcon}</div>
                <div class="minmax-content">
                    <span class="minmax-label">Tertinggi</span>
                    <span class="minmax-value">${result.max.value} ${unit}</span>
                    <span class="minmax-time">${maxTimeLabel}</span>
                </div>
            </div>
        `;
        container.classList.remove('loading');
    }

    // Temperature
    if (temps && temps.length > 0) {
        const tempResult = findMinMax(temps, labels);
        renderMinMaxRow('tempChart-minmax', tempResult, '°C', '❄️', '🔥', true);
    }

    // Pressure
    if (pressures && pressures.length > 0) {
        const pressResult = findMinMax(pressures, labels);
        renderMinMaxRow('pressureChart-minmax', pressResult, 'hPa', '⬇️', '⬆️', false);
    }

    // Wind Speed
    if (winds && winds.length > 0) {
        const windResult = findMinMax(winds, labels);
        renderMinMaxRow('windChart-minmax', windResult, 'KT', '🍃', '🌪️', false);
    }
}
window.updateChartMinMax = updateChartMinMax;

// =======================
// FETCH METAR DATA
// =======================
// 🗑️ fetchMetar() removed - replaced by pollLatestData() for consistency

// =======================
// METAR STATUS INDICATOR SYSTEM
// =======================

// 🔥 RE SEQUENCE VALIDATION HELPERS
function getMainMetarPart(metar) {
    if (!metar) return "";
    // Find the first occurrence of trend/supplementary keywords
    const match = metar.match(/\b(TEMPO|BECMG|NOSIG|PROB|TREND|RMK)\b/i);
    if (match) {
        // Cut the string before the keyword
        return metar.substring(0, match.index).trim();
    }
    return metar.trim();
}

function metarHasTS(metar) {
    // Detects TS, TSRA, +TSRA, -TSRA, VCTS, TSGR etc.
    return /(?:^|\s)(?:[+-])?(?:VC)?TS(?:RA|GR|SN|PE|PL)?\b/i.test(metar);
}

function metarHasRA(metar) {
    // Detects RA, +RA, -RA, SHRA, TSRA, DZ, +DZ, -DZ etc.
    return /(?:^|\s)(?:[+-])?(?:TS|SH|VC)?(?:RA|DZ)\b/i.test(metar);
}

function metarHasRETSRA(metar) {
    return /\bRETSRA\b/i.test(metar);
}

function metarHasRETS(metar) {
    // RETS but not RETSRA
    return /\bRETS\b/i.test(metar) && !metarHasRETSRA(metar);
}

function metarHasRERA(metar) {
    return /\bRERA\b/i.test(metar);
}

/**
 * 🔥 Check RE (Recent Weather) sequence between consecutive METAR observations.
 * Returns an array of error messages if transition rules are violated.
 * @param {string} currentRaw - Current METAR string
 * @param {string} previousRaw - Previous METAR string (the one before current in time)
 * @returns {string[]} Array of error strings, empty if all OK
 */
function checkRESequence(currentRaw, previousRaw) {
    if (!currentRaw || !previousRaw) return [];
    const errors = [];

    const currentMain = getMainMetarPart(currentRaw);
    const previousMain = getMainMetarPart(previousRaw);

    const prevHasTS = metarHasTS(previousMain);
    const prevHasRA = metarHasRA(previousMain);
    const currHasTS = metarHasTS(currentMain);
    const currHasRA = metarHasRA(currentMain);

    // Case 1: Previous had TSRA, current has neither TS nor RA → needs RETSRA
    if (prevHasTS && prevHasRA && !currHasTS && !currHasRA) {
        if (!metarHasRETSRA(currentRaw)) {
            errors.push('❌ Cuaca TSRA berhenti tapi tidak ada RETSRA');
        }
    }
    // Case 2: Previous had TSRA, current has only TS (rain stopped) → needs RERA
    else if (prevHasTS && prevHasRA && currHasTS && !currHasRA) {
        if (!metarHasRERA(currentRaw)) {
            errors.push('❌ Hujan (RA) berhenti tapi tidak ada RERA');
        }
    }
    // Case 3: Previous had TSRA, current has only RA (TS stopped) → needs RETS
    else if (prevHasTS && prevHasRA && !currHasTS && currHasRA) {
        if (!metarHasRETS(currentRaw) && !metarHasRETSRA(currentRaw)) {
            errors.push('❌ Thunderstorm (TS) berhenti tapi tidak ada RETS');
        }
    }
    // Case 4: Previous had TS only (no RA), current has no TS → needs RETS
    else if (prevHasTS && !prevHasRA && !currHasTS) {
        if (!metarHasRETS(currentRaw) && !metarHasRETSRA(currentRaw)) {
            errors.push('❌ Cuaca TS berhenti tapi tidak ada RETS');
        }
    }
    // Case 5: Previous had RA only (no TS), current has no RA → needs RERA
    else if (!prevHasTS && prevHasRA && !currHasRA) {
        if (!metarHasRERA(currentRaw) && !metarHasRETSRA(currentRaw)) {
            errors.push('❌ Cuaca RA berhenti tapi tidak ada RERA');
        }
    }

    return errors;
}

/**
 * Determine METAR status based on data characteristics.
 * Logic:
 *   - COR/CCA in raw string → Correction
 *   - AMD in raw string → Amendment
 *   - Contains comma → Invalid/Anomaly
 *   - Minute not on :00 or :30 → SPECI (intermediate)
 *   - Otherwise → Normal METAR
 */
function getMetarStatus(rawMetar, fullTime, validationResults, sequenceErrors) {
    const raw = rawMetar || '';
    const hasComma = raw.includes(',');
    const hasValidationError = validationResults && validationResults.length > 0 && !validationResults[0].startsWith('✅');
    const hasSequenceError = sequenceErrors && sequenceErrors.length > 0;

    // 1. Check for anomaly (contains comma, validation errors, or RE sequence errors)
    if (hasComma || hasValidationError || hasSequenceError) {
        let errorDetail = hasComma ? 'Anomali ditemukan dalam string data (koma)' : '';
        if (hasValidationError) {
            const errorList = validationResults.filter(err => !err.startsWith('✅')).join('; ');
            errorDetail = errorDetail ? `${errorDetail}. ${errorList}` : errorList;
        }
        if (hasSequenceError) {
            const seqList = sequenceErrors.join('; ');
            errorDetail = errorDetail ? `${errorDetail}. ${seqList}` : seqList;
        }

        return {
            type: 'invalid',
            label: 'INVALID',
            pillIcon: '🔴',
            icon: '⚠️',
            title: 'Data Invalid',
            description: hasSequenceError ? 'Data METAR tidak memenuhi aturan Recent Weather (RE).' : (hasComma ? 'Data METAR mengandung karakter tidak valid (koma).' : 'Data METAR gagal divalidasi.'),
            errorDetail: errorDetail
        };
    }

    // 2. Check for COR (Correction)
    if (raw.includes(' COR ') || raw.startsWith('METAR COR') || raw.includes('CCA') || raw.includes(' CCA ')) {
        return {
            type: 'cor',
            label: 'COR',
            pillIcon: '🟠',
            icon: '🔄',
            title: 'Koreksi (COR)',
            description: 'Laporan koreksi terhadap METAR sebelumnya.',
            errorDetail: null
        };
    }

    // 3. Check for AMD (Amendment)
    if (raw.includes(' AMD ') || raw.startsWith('METAR AMD')) {
        return {
            type: 'amd',
            label: 'AMD',
            pillIcon: '🔵',
            icon: '📝',
            title: 'Amandemen (AMD)',
            description: 'Perubahan/amandemen terhadap laporan sebelumnya.',
            errorDetail: null
        };
    }

    // 4. Extract minute from the METAR time group (DDHHMMz)
    let minute = -1;
    const timeGroupMatch = raw.match(/\b\d{6}Z\b/);
    if (timeGroupMatch) {
        minute = parseInt(timeGroupMatch[0].substring(4, 6), 10);
    } else if (fullTime) {
        const timeParts = fullTime.match(/:(\d{2})/);
        if (timeParts) {
            minute = parseInt(timeParts[1], 10);
        }
    }

    // 5. Check for SPECI keyword in raw string
    if (raw.includes('SPECI') || raw.startsWith('SPECI ')) {
        const minuteStr = minute >= 0 ? minute.toString().padStart(2, '0') : '??';
        return {
            type: 'speci',
            label: 'SPECI',
            pillIcon: '🟡',
            icon: '📡',
            title: 'Laporan Khusus (SPECI)',
            description: `Laporan cuaca khusus di luar jadwal rutin (menit :${minuteStr}).`,
            errorDetail: null
        };
    }

    // 6. Determine based on minute: 0 or 30 = normal, else = SPECI
    if (minute >= 0 && minute !== 0 && minute !== 30) {
        const minuteStr = minute.toString().padStart(2, '0');
        return {
            type: 'speci',
            label: 'SPECI',
            pillIcon: '🟡',
            icon: '📡',
            title: 'Laporan Khusus (SPECI)',
            description: `Laporan cuaca khusus di luar jadwal rutin (menit :${minuteStr}).`,
            errorDetail: null
        };
    }

    // 7. Normal METAR
    return {
        type: 'normal',
        label: 'METAR',
        pillIcon: '🟢',
        icon: '✓',
        title: 'Data Normal',
        description: 'Laporan cuaca rutin sesuai jadwal standar (kelipatan 30 menit).',
        errorDetail: null
    };
}

/**
 * Create HTML for status indicator with tooltip
 */
function createStatusIndicatorHTML(status) {
    const tooltipError = status.errorDetail
        ? `<div class="tooltip-error">${status.errorDetail}</div>`
        : '';

    return `
        <div class="metar-status-indicator metar-status-${status.type}">
            <span class="metar-status-icon">${status.pillIcon}</span>
            <span>${status.label}</span>
            <div class="metar-status-tooltip">
                <div class="tooltip-title">
                    <span>${status.icon}</span>
                    <span>${status.title}</span>
                </div>
                <div class="tooltip-desc">${status.description}</div>
                ${tooltipError}
            </div>
        </div>
    `;
}

// =======================
// HISTORY TABLE UPDATE
// =======================
async function updateHistoryTable() {
    try {
        const tbody = document.getElementById('historyTableBody');
        if (!tbody) return;

        const res = await fetch('/api/history');
        const result = await res.json();

        tbody.innerHTML = '';
        if (result.data && result.data.length > 0) {
            result.data.forEach((item, i) => {
                const row = document.createElement('tr');
                row.style.animation = `fadeIn 0.3s ease-out ${i * 0.03}s both`;

                const status = getMetarStatus(item.metar, item.full_time, item.validation_results);
                const statusHtml = createStatusIndicatorHTML(status);

                if (status.type === 'speci') {
                    row.classList.add('metar-row-speci');
                } else if (status.type === 'invalid') {
                    row.classList.add('metar-row-invalid');
                }

                const metarWithEqual = item.metar.trim().endsWith('=') ? item.metar : item.metar + '=';
                row.innerHTML = `
                    <td class="col-time">${item.full_time}</td>
                    <td class="col-station">${item.station}</td>
                    <td class="metar-cell col-metar">${metarWithEqual}</td>
                    <td class="col-status">${statusHtml}</td>
                `;
                tbody.appendChild(row);
            });
        } else {
            tbody.innerHTML = '<tr><td colspan="4" class="text-center text-muted">No records available for today</td></tr>';
        }
    } catch (e) {
        console.error('History table error:', e);
    }
}

// =======================
// MINI TIMELINE RENDERER
// =======================
async function updateMiniTimeline() {
    try {
        const res = await fetch('/api/history');
        const result = await res.json();
        const tlContainer = document.getElementById('metarTimeline');

        // This element only exists on index.html
        if (!tlContainer) return;

        tlContainer.innerHTML = '';

        if (result.data && result.data.length > 0) {
            // We want chronological order for timeline (oldest to newest left to right)
            // The API returns newest first (descending), so we reverse it or just slice
            // Let's show the last 5 reports for the timeline
            const timelineData = result.data.slice(0, 5).reverse();

            timelineData.forEach((item) => {
                // Extract just the HH:MM
                const timeMatch = item.time.match(/\d{4}-\d{2}-\d{2}\s(\d{2}:\d{2})/);
                const shortTime = timeMatch ? timeMatch[1] : item.time;

                let badgeText = 'METAR';
                let badgeColor = '#059669'; // Emerald

                if (item.metar.includes(' COR ') || item.metar.startsWith('METAR COR') || item.metar.includes('CCA') || item.metar.includes(' CCA ')) {
                    badgeText = 'COR';
                    badgeColor = '#f59e0b'; // Amber
                } else if (item.metar.includes(' AMD ') || item.metar.startsWith('METAR AMD')) {
                    badgeText = 'AMD';
                    badgeColor = '#3b82f6'; // Blue
                } else if (item.metar.includes(' SPECI ') || item.metar.startsWith('SPECI ')) {
                    badgeText = 'SPECI';
                    badgeColor = '#ef4444'; // Red
                }

                const node = document.createElement('div');
                node.style.cssText = 'display: flex; flex-direction: column; align-items: center; min-width: 60px;';
                node.innerHTML = `
                    <span style="font-weight: bold; margin-bottom: 4px;">${shortTime}</span>
                    <span style="background-color: ${badgeColor}; color: ${badgeText === 'COR' ? '#1e293b' : '#fff'}; padding: 2px 8px; border-radius: 4px; font-size: 0.70rem; font-weight: bold;">
                        ${badgeText}
                    </span>
                `;

                tlContainer.appendChild(node);

                // Add a connector line if it's not the last element (handled implicitly via gap in CSS)
            });
        } else {
            tlContainer.innerHTML = '<span style="color: #64748b;">No recent reports</span>';
        }
    } catch (e) {
        console.error('Mini timeline error:', e);
    }
}

/**
 * Global Chart Download Handler
 * Supports both Chart.js and Plotly.js charts
 */
function downloadChart(chartId) {
    console.log(`[DOWNLOAD] Initiating export for: ${chartId}`);

    // Handle Chart.js plots (trends).
    let chartInstance = null;
    let fallbackFilename = 'Chart';

    if (chartId === 'tempChart') { chartInstance = tempChart; fallbackFilename = 'Temperature_Trend'; }
    else if (chartId === 'pressureChart') { chartInstance = pressureChart; fallbackFilename = 'Pressure_Trend'; }
    else if (chartId === 'windChart') { chartInstance = windChart; fallbackFilename = 'Wind_Speed_Trend'; }

    if (chartInstance) {
        const link = document.createElement('a');
        link.download = `${fallbackFilename}_${STATION}_${new Date().toISOString().slice(0, 10)}.png`;
        link.href = chartInstance.toBase64Image();
        link.click();
    } else {
        // Fallback for canvas elements not using the global instances
        const canvas = document.getElementById(chartId);
        if (canvas && canvas.toDataURL) {
            const link = document.createElement('a');
            link.download = `${chartId}_Export.png`;
            link.href = canvas.toDataURL('image/png');
            link.click();
        } else {
            console.error(`[DOWNLOAD] Chart instance or canvas not found for ID: ${chartId}`);
        }
    }
}

// =======================
// INITIALIZATION
// =======================
document.addEventListener('DOMContentLoaded', function () {
    // 1. Sync Sound Button UI FIRST (Most critical for UX persistence)
    const soundToggleHeader = document.getElementById('soundToggle');
    const soundToggleSidebar = document.getElementById('soundToggleSidebar');
    const soundIconSidebar = document.getElementById('soundIconSidebar');
    const soundLabelSidebar = document.getElementById('soundLabelSidebar');

    if (soundEnabled) {
        if (soundToggleHeader) {
            soundToggleHeader.textContent = '🔊 Sound ON';
            soundToggleHeader.classList.add('active');
        }
        if (soundToggleSidebar) {
            if (soundIconSidebar) soundIconSidebar.textContent = '🔊';
            if (soundLabelSidebar) soundLabelSidebar.textContent = 'Sound ON';
            soundToggleSidebar.classList.add('active');
        }
    } else {
        if (soundToggleHeader) {
            soundToggleHeader.textContent = '🔇 Sound OFF';
            soundToggleHeader.classList.remove('active');
        }
        if (soundToggleSidebar) {
            if (soundIconSidebar) soundIconSidebar.textContent = '🔇';
            if (soundLabelSidebar) soundLabelSidebar.textContent = 'Sound OFF';
            soundToggleSidebar.classList.remove('active');
        }
    }

    // 2. Clocks
    updateClocks();

    // 4. Theme (Apply saved preference)
    applyTheme(currentTheme);

    // 5. Staggered card fade-in
    document.querySelectorAll('.card, .metar-raw-panel').forEach((el, i) => {
        if (el) el.style.animationDelay = `${0.05 + i * 0.06}s`;
    });

    // Create charts
    createCharts();
    createWindChart();
    updateCharts();
    if (window.location.pathname === '/charts') {
        initializeChartPeriodControls();
    } else {
        loadHistory();
    }
    // updateMiniTimeline(); // 🔥 DISABLED (As requested)

    // 6. Instant UI Population from SSR content (Eliminates loading delay)
    const initialRaw = document.getElementById('metarRawCode');
    if (initialRaw) {
        // Clean text content (remove any existing badge text that might be inside)
        // We clone it to get clean text without the badge span contents if they were rendered
        const cleanText = initialRaw.innerText.split('⚠️')[0].trim().split('🟢')[0].trim();

        if (cleanText && cleanText !== '{{ latest.metar }}' && cleanText !== '--') {
            console.log('[INIT] Pre-populating UI from clean text:', cleanText);

            initialRaw.innerHTML = highlightMetar(cleanText);

            // Re-append badges based on text content
            if (cleanText.includes(' COR ') || cleanText.includes('CCA')) {
                initialRaw.innerHTML += ' <span class="badge" style="background-color: #f59e0b; color: #1e293b; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ COR</span>';
            } else if (cleanText.includes(' AMD ')) {
                initialRaw.innerHTML += ' <span class="badge" style="background-color: #3b82f6; color: #ffffff; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ AMD</span>';
            } else if (cleanText.includes(' SPECI ')) {
                initialRaw.innerHTML += ' <span class="badge" style="background-color: #ef4444; color: #ffffff; margin-left: 10px; font-size: 0.75rem; padding: 4px 8px; vertical-align: middle;">⚠️ SPECI</span>';
            }

            updateDOM(cleanText, STATION);
            runMetarValidation(cleanText);
            lastMetarRaw = cleanText;
            alarmState.lastMetarHash = hashMetar(cleanText);
            saveAlarmState();
        }
    }

    // For pages that don't have the initial raw text but still need to render widgets based on last state
    if (!initialRaw && window.lastKnownMetar) {
        updateDOM(window.lastKnownMetar, STATION);
    }

    // Skip background polling and heavy UI initialization if this is a manual session
    if (window.isManualSession) {
        console.log('[INIT] Manual session detected. Skipping background polling.');
        return;
    }

    // 7. Sync System Fetch Status and trigger first poll
    console.log('[INIT] Syncing system fetch status with server...');

    fetch("/api/set_fetch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: autoFetchEnabled })
    })
        .then(response => {
            if (!response.ok) throw new Error('Fetch status sync failed');
            return response.json();
        })
        .then(data => {
            updateStatusPanel(data);
            console.log('[INIT] Server fetch status synced:', data.auto_fetch);
            if (typeof adaptivePoll === 'function') setTimeout(adaptivePoll, 1000);
        })
        .catch(error => {
            console.error('[INIT] Failed to sync fetch status:', error);
            if (typeof adaptivePoll === 'function') setTimeout(adaptivePoll, 1000);
        });

    // Initial poll will handle first load
    // setInterval(fetchMetar, 60000); // 🗑️ REMOVED REDUNDANT LOOP
});

// =======================
// HELP MODAL FUNCTIONS
// =======================
function toggleHelpModal() {
    const modal = document.getElementById('helpModal');
    const overlay = document.getElementById('helpModalOverlay');

    if (modal.classList.contains('active')) {
        closeHelpModal();
    } else {
        modal.classList.add('active');
        overlay.classList.add('active');
        document.body.style.overflow = 'hidden'; // Prevent background scroll
    }
}

function closeHelpModal() {
    const modal = document.getElementById('helpModal');
    const overlay = document.getElementById('helpModalOverlay');

    modal.classList.remove('active');
    overlay.classList.remove('active');
    document.body.style.overflow = ''; // Restore scroll
}

// Close modal dengan tombol Escape
document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
        closeHelpModal();
    }
});

// Global expose
window.toggleHelpModal = toggleHelpModal;
window.closeHelpModal = closeHelpModal;

// =======================
// FOG / MIST / HAZE EFFECT CONTROLLER (VANTA.JS)
// =======================

let vantaFogInstance = null;
const fogState = {
    isActive: false,
    currentType: null, // 'FG', 'HZ', 'BR', atau null
    lastWeather: null
};

/**
 * Watch for dark/light mode toggle to update Vanta colors
 */
const toggleObserver = new MutationObserver((mutations) => {
    mutations.forEach((mutation) => {
        if (mutation.attributeName === 'data-theme' && fogState.isActive) {
            console.log('[FOG] Theme changed, updating Vanta colors...');
            applyVantaFog(fogState.currentType);
        }
    });
});
if (document.documentElement) {
    toggleObserver.observe(document.documentElement, { attributes: true });
}

function applyVantaFog(fogType) {
    const isDark = document.documentElement.getAttribute('data-theme') === 'dark';

    // Default FOG (FG) - Thinner and softer
    let vantaConfig = {
        highlightColor: isDark ? 0xbbbbbb : 0x888888,
        midtoneColor: isDark ? 0x888888 : 0xaaaaaa,
        lowlightColor: isDark ? 0x222222 : 0xdddddd,
        baseColor: isDark ? 0x000000 : 0xffffff,
        blurFactor: 0.5,    // Diperkecil agar lebih tipis
        speed: 0.2,         // Mengembalikan kecepatan asli FG (0.6)
        zoom: 1.2           // Pola dan arah kabut (style FG)
    };

    if (fogType === 'HZ') {
        // Haze - Thinner in light mode (colors closer to white), brighter in dark mode
        vantaConfig.highlightColor = 0xd1c7b5;
        vantaConfig.midtoneColor = 0xece5d8;
        vantaConfig.lowlightColor = 0xf5f0e6;
        vantaConfig.blurFactor = 0.5; // Thinner look
        vantaConfig.speed = 0.2;
        vantaConfig.zoom = 0.3;

    } else if (fogType === 'BR') {
        // Mist - Sangat tipis, arah/pola (zoom & speed) disamakan dengan efek FG aslinya!
        vantaConfig.highlightColor = isDark ? 0xaaaaaa : 0x94a3b8;
        vantaConfig.midtoneColor = isDark ? 0x666666 : 0xcbd5e1;
        vantaConfig.lowlightColor = isDark ? 0x222222 : 0xe2e8f0;
        vantaConfig.blurFactor = 0.5;  // Paling tipis
        vantaConfig.speed = 0.2;        // NGAMBIL DARI EFEK FG SEBELUMNYA
        vantaConfig.zoom = 1.2;         // NGAMBIL DARI EFEK FG SEBELUMNYA
    }

    if (vantaFogInstance) {
        console.log(`[FOG] Updating Vanta instance for ${fogType} (Softened)...`);
        vantaFogInstance.setOptions(vantaConfig);
    } else {
        if (typeof VANTA === 'undefined') {
            console.warn('[FOG] Vanta.js not loaded yet, retrying in 500ms...');
            setTimeout(() => applyVantaFog(fogType), 500);
            return;
        }
        console.log(`[FOG] Initializing Vanta instance for ${fogType} (Softened)...`);
        vantaFogInstance = VANTA.FOG(Object.assign({
            el: "#fogContainer",
            mouseControls: true,
            touchControls: true,
            gyroControls: false,
            minHeight: 200.00,
            minWidth: 200.00
        }, vantaConfig));
    }
}

function checkAndActivateFog(rawMetar) {
    if (!rawMetar) return;
    const metar = rawMetar.toUpperCase();
    console.log(`[FOG] Checking weather for: ${metar.substring(0, 50)}...`);
    const hasFG = /\bFG\b/i.test(metar);
    const hasHZ = /\bHZ\b/i.test(metar);
    const hasBR = /\bBR\b/i.test(metar);
    console.log(`[FOG] Regex results -> FG: ${hasFG}, HZ: ${hasHZ}, BR: ${hasBR}`);
    if (hasFG) console.log("[FOG] Match Found: FG (Fog)");
    if (hasHZ) console.log("[FOG] Match Found: HZ (Haze)");
    if (hasBR) console.log("[FOG] Match Found: BR (Mist)");

    const fogContainer = document.getElementById('fogContainer');
    const fogIndicator = document.getElementById('fogIndicator');
    const fogIndicatorText = document.getElementById('fogIndicatorText');

    if (!fogContainer || !fogIndicator) {
        // Only warn if we're on a real dashboard page, not a manual parser or history page without these elements
        if (!window.isManualSession) {
            console.warn('[FOG] Container or indicator element not found.');
        }
        return;
    }

    let fogType = null;
    let indicatorClass = '';
    let icon = '';
    let text = '';

    if (hasFG) {
        fogType = 'FG';
        indicatorClass = 'fog-fg';
        icon = '🌫️';
        text = 'FOG - VISIBILITY REDUCED';
    } else if (hasHZ) {
        fogType = 'HZ';
        indicatorClass = 'fog-hz';
        icon = '😶‍🌫️';
        text = 'HAZE - SMOKE/DUST';
    } else if (hasBR) {
        fogType = 'BR';
        indicatorClass = 'fog-br';
        icon = '💨';
        text = 'MIST - LIGHT FOG';
    }

    // Jika tidak ada kabut, matikan efek
    if (!fogType) {
        if (fogState.isActive) {
            console.log('[FOG] Weather clear, deactivating...');
            deactivateFog();
        }
        return;
    }

    // Jika tipe sama dan sudah aktif, tidak perlu update berat
    if (fogState.isActive && fogState.currentType === fogType) {
        return;
    }

    console.log(`[FOG] Detected ${fogType}. Activating Vanta...`);
    fogState.isActive = true;
    fogState.currentType = fogType;
    fogState.lastWeather = metar;

    // Reset and show elements
    fogContainer.className = 'fog-container active';
    fogIndicator.className = 'fog-indicator active';
    document.body.classList.add('fog-active');

    // Switch Vanta Fog Config
    applyVantaFog(fogType);

    // Badge update
    fogIndicator.classList.add(indicatorClass);
    const iconEl = fogIndicator.querySelector('.fog-indicator-icon');
    if (iconEl) iconEl.textContent = icon;
    if (fogIndicatorText) fogIndicatorText.textContent = text;
}

/**
 * Matikan efek kabut dengan transisi halus
 */
function deactivateFog() {
    if (!fogState.isActive) return;

    const fogContainer = document.getElementById('fogContainer');
    const fogIndicator = document.getElementById('fogIndicator');

    if (fogContainer) fogContainer.classList.remove('active');
    if (fogIndicator) fogIndicator.classList.remove('active');

    // Destroy Vanta instance after fade-out transition (2s)
    setTimeout(() => {
        if (fogContainer) {
            fogContainer.className = 'fog-container';
            if (vantaFogInstance) {
                console.log('[FOG] Destroying Vanta instance to save resources.');
                vantaFogInstance.destroy();
                vantaFogInstance = null;
            }
        }
        if (fogIndicator) fogIndicator.className = 'fog-indicator';
    }, 2000);

    fogState.isActive = false;
    fogState.currentType = null;
    document.body.classList.remove('fog-active');
}

// Update fog effect saat data METAR berubah
function updateFogEffect(data) {
    const rawMetar = data.raw || data.metar || '';
    checkAndActivateFog(rawMetar);
}

// Global helper to trigger DOM updates on auxiliary pages
function updateDOM(raw, station) {
    if (!raw) return;
    window.lastKnownMetar = raw;

    // Try updating decoded panels (home) and crosswind (operational tools)
    if (typeof updateDecodedPanel === 'function') {
        updateDecodedPanel(raw);
    }

    // Try updating thunderstorm panel (weather analysis)
    if (typeof updateThunderstormModule === 'function') {
        updateThunderstormModule(raw);
    }

    // Refresh Historical Charts and Wind Rose
    if (typeof loadHistory === 'function') {
        loadHistory();
    }
}

// Global expose
window.updateFogEffect = updateFogEffect;
window.checkAndActivateFog = checkAndActivateFog;
window.checkRainStatus = checkRainStatus;
window.makeItRain = makeItRain;
window.stopRain = stopRain;
window.updateDOM = updateDOM;

// ============================================
// STALE DATA DETECTOR - 15 MINUTE THRESHOLD
// ============================================

class MetarStaleMonitor {
    constructor(options = {}) {
        this.graceMinutes = options.graceMinutes || 15;
        this.checkInterval = options.checkInterval || 30 * 1000;
        this.lastMetarTimestamp = null;
        this.lastMetarRaw = null;

        // State management
        this.isAlerting = false;     // Is the popup currently showing?
        this.isDismissed = false;    // Has the user clicked "Dismiss" for the current period?
        this.dismissedObsKey = null; // Which period was dismissed?

        this.alarmTimer = null;
        this.timer = null;
    }

    /**
     * Parse METAR timestamp (e.g., "010600Z" -> Date object in UTC)
     */
    parseMetarTimestamp(metarString) {
        if (!metarString) return null;

        const match = metarString.match(/\b(\d{2})(\d{2})(\d{2})Z\b/);
        if (!match) return null;

        const day = parseInt(match[1]);
        const hour = parseInt(match[2]);
        const minute = parseInt(match[3]);

        const now = new Date();
        const year = now.getUTCFullYear();
        const month = now.getUTCMonth();

        let metarDate = new Date(Date.UTC(year, month, day, hour, minute, 0));

        // If parsed date is in the future by more than 1 day, it's likely from previous month
        if (metarDate.getTime() - now.getTime() > 24 * 60 * 60 * 1000) {
            metarDate = new Date(Date.UTC(year, month - 1, day, hour, minute, 0));
        }

        return metarDate;
    }

    /**
     * Get the most recent scheduled observation time (:00 or :30) that has passed
     */
    getExpectedObservationTime() {
        const now = new Date();
        const minute = now.getUTCMinutes();
        const expectedMinute = minute >= 30 ? 30 : 0;

        return new Date(Date.UTC(
            now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate(),
            now.getUTCHours(), expectedMinute, 0
        ));
    }

    /**
     * Called when new METAR data arrives from polling
     */
    updateFromMetar(metarString) {
        if (!metarString) return;

        const timestamp = this.parseMetarTimestamp(metarString);
        if (!timestamp) return;

        const newTs = timestamp.getTime();

        // Only process if the METAR observation time changed
        if (this.lastMetarTimestamp === null || newTs !== this.lastMetarTimestamp) {
            console.log(`[STALE] New METAR timestamp: ${timestamp.toUTCString()}`);
            this.lastMetarTimestamp = newTs;
            this.lastMetarRaw = metarString;

            // Immediately check if this new data clears a currently active or dismissed alert
            const expected = this.getExpectedObservationTime();
            if (newTs >= expected.getTime()) {
                if (this.isAlerting || this.isDismissed) {
                    console.log("[STALE] Fresh data arrived. Clearing all alerts.");
                }
                this.resetState();
            }
        }
    }

    resetState() {
        this.isAlerting = false;
        this.isDismissed = false;
        this.dismissedObsKey = null;
        this.hideAlert();
        this.stopAlarm();
    }

    /**
     * Periodic check: has METAR data failed to update after a new observation period?
     * 
     * Logic:
     *   1. Determine the latest scheduled observation time (:00 or :30 UTC)
     *   2. If we are more than `graceMinutes` past that time...
     *   3. AND the METAR timestamp is still from BEFORE that observation time...
     *   4. THEN the data is stale → show popup
     */
    checkStale() {
        if (!this.lastMetarTimestamp) return;

        const now = new Date();
        const expectedObs = this.getExpectedObservationTime();
        const expectedObsMs = expectedObs.getTime();
        const obsKey = expectedObs.toISOString();

        // 1. If data is NOT stale (it's current for this period), ensure state is reset
        if (this.lastMetarTimestamp >= expectedObsMs) {
            if (this.isAlerting || this.isDismissed) {
                this.resetState();
            }
            return;
        }

        // 2. Data IS old relative to schedule. Now check if we are past grace period.
        const msPastExpected = now.getTime() - expectedObsMs;
        const gracePeriodMs = this.graceMinutes * 60 * 1000;

        if (msPastExpected < gracePeriodMs) {
            // Still in grace period → do nothing
            return;
        }

        // 3. We are past grace period and data is still old.
        // Check if we already handled this period
        if (this.isDismissed && this.dismissedObsKey === obsKey) {
            return; // User already acknowledged this specific stale period
        }

        if (this.isAlerting) {
            // Already showing the popup. No need to re-trigger, 
            // but we could update the "Minutes Late" text if we wanted.
            return;
        }

        // 4. Trigger the Alert
        const metarDate = new Date(this.lastMetarTimestamp);
        const metarTimeStr = metarDate.getUTCHours().toString().padStart(2, '0') + ':' +
            metarDate.getUTCMinutes().toString().padStart(2, '0') + ':' +
            metarDate.getUTCSeconds().toString().padStart(2, '0');

        const expectedTimeStr = expectedObs.getUTCHours().toString().padStart(2, '0') + ':' +
            expectedObs.getUTCMinutes().toString().padStart(2, '0') + ':' +
            expectedObs.getUTCSeconds().toString().padStart(2, '0');

        const nowTimeStr = now.getUTCHours().toString().padStart(2, '0') + ':' +
            now.getUTCMinutes().toString().padStart(2, '0') + ':' +
            now.getUTCSeconds().toString().padStart(2, '0');

        const totalDelay = now.getTime() - expectedObsMs;
        const delayMinutes = Math.floor(totalDelay / 60000);
        const delaySeconds = Math.floor((totalDelay % 60000) / 1000);

        console.warn(`[STALE] ⚠️ ALERT: Expected ${expectedTimeStr}Z, current is ${metarTimeStr}Z. Delay: ${delayMinutes}m`);

        this.showAlert(metarTimeStr, nowTimeStr, delayMinutes, delaySeconds, expectedTimeStr);
        this.startAlarmLoop();
        this.isAlerting = true;
        this.isDismissed = false;
        this.dismissedObsKey = null;
    }

    /**
     * Play alarm using existing system, repeat every 10 seconds
     */
    startAlarmLoop() {
        // Use specifically playStaleAlarm for metartelat.mp3
        if (typeof playStaleAlarm === 'function') {
            playStaleAlarm();
        }
        this.alarmTimer = setTimeout(() => this.startAlarmLoop(), 10000);
    }

    stopAlarm() {
        if (this.alarmTimer) {
            clearTimeout(this.alarmTimer);
            this.alarmTimer = null;
        }
    }

    showAlert(metarTime, currentTime, minutes, seconds, expectedTime) {
        let popup = document.getElementById('stale-metar-alert');
        if (!popup) {
            popup = document.createElement('div');
            popup.id = 'stale-metar-alert';
            popup.className = 'stale-alert-overlay';
            document.body.appendChild(popup);
        }

        popup.innerHTML = `
            <div class="stale-alert-box">
                <div class="stale-alert-header">
                    <span class="stale-alert-bell">🔔</span>
                    <h3>DATA METAR TIDAK UPDATE</h3>
                </div>
                <div class="stale-alert-body">
                    <div class="stale-expected-obs">
                        <span class="expected-obs-icon">📡</span>
                        <span>Observasi <strong>${expectedTime} UTC</strong> belum diterima</span>
                    </div>
                    <div class="time-comparison">
                        <div class="time-box time-metar">
                            <label>Data Terakhir</label>
                            <span class="time-value">${metarTime} UTC</span>
                        </div>
                        <div class="time-arrow">➔</div>
                        <div class="time-box time-expected">
                            <label>Seharusnya</label>
                            <span class="time-value">${expectedTime} UTC</span>
                        </div>
                        <div class="time-arrow">➔</div>
                        <div class="time-box time-now">
                            <label>Sekarang</label>
                            <span class="time-value">${currentTime} UTC</span>
                        </div>
                    </div>
                    <div class="stale-duration">
                        <span class="duration-label">Terlambat dari jadwal:</span>
                        <span class="duration-value">${minutes} menit ${seconds} detik</span>
                    </div>
                    <div class="stale-warning-box">
                        <strong>⚠️ PERINGATAN</strong>
                        <p>Data METAR seharusnya sudah update pada <strong>${expectedTime} UTC</strong> namun masih menampilkan data <strong>${metarTime} UTC</strong> setelah lebih dari ${this.graceMinutes} menit.</p>
                        <p class="stale-cause">Kemungkinan: Sumber data cuaca bermasalah atau koneksi terputus.</p>
                    </div>
                </div>
                <button class="stale-alert-dismiss" onclick="window.metarStaleMonitor.dismiss()">
                    ✓ Saya Mengerti (Matikan Alarm)
                </button>
                <div class="stale-alert-footer">
                    Popup akan muncul lagi pada periode observasi berikutnya jika data masih tidak update
                </div>
            </div>
        `;

        popup.classList.add('visible');

        // Browser notification
        if ('Notification' in window && Notification.permission === 'granted') {
            try {
                new Notification('🔔 METAR TELAT - Data Tidak Update', {
                    body: `Observasi ${expectedTime} UTC belum diterima! Data masih ${metarTime} UTC (terlambat ${minutes}m ${seconds}s).`,
                    requireInteraction: true
                });
            } catch (e) { /* ignore notification errors */ }
        }

        // Also show a toast for consistency
        if (typeof showToast === 'function') {
            showToast('METAR TELAT', `Observasi ${expectedTime} UTC belum diterima!`, 'danger', 10000);
        }
    }

    hideAlert() {
        const popup = document.getElementById('stale-metar-alert');
        if (popup) popup.classList.remove('visible');
    }

    dismiss() {
        const expectedObs = this.getExpectedObservationTime();
        this.isAlerting = false;
        this.isDismissed = true;
        this.dismissedObsKey = expectedObs.toISOString();

        this.hideAlert();
        this.stopAlarm();
        console.log(`[STALE] Alert dismissed for period ${this.dismissedObsKey}.`);
    }

    start() {
        console.log(`[STALE] Monitor started (grace: ${this.graceMinutes}min past obs time, check every ${this.checkInterval / 1000}s)`);
        this.checkStale();
        this.timer = setInterval(() => this.checkStale(), this.checkInterval);
    }

    stop() {
        if (this.timer) clearInterval(this.timer);
        this.stopAlarm();
    }
}

// Initialize and expose globally (Deactivated as requested)
/*
const metarStaleMonitor = new MetarStaleMonitor({
    graceMinutes: 15,          // Alert if data not updated 15 min past observation time
    checkInterval: 30 * 1000  // Check every 30 seconds
});
metarStaleMonitor.start();
window.metarStaleMonitor = metarStaleMonitor;
*/

// Request notification permission on first interaction
document.addEventListener('click', () => {
    if ('Notification' in window && Notification.permission === 'default') {
        Notification.requestPermission();
    }
}, { once: true });

// Seed stale monitor from server-rendered METAR on page load (Deactivated as requested)
(function initStaleMonitorFromDOM() {
    const rawEl = document.getElementById('metarRawCode');
    if (rawEl && rawEl.textContent.trim()) {
        const rawText = rawEl.textContent.trim().replace(/=/g, '');
        if (typeof metarStaleMonitor !== 'undefined') {
            metarStaleMonitor.updateFromMetar(rawText);
            console.log('[STALE] Seeded from server-rendered METAR');
        }
    }
})();

// =======================
// DAILY METAR RECORD MANAGER
// =======================
let currentView = 'today';

async function loadView(viewType) {
    if (window.location.pathname !== '/' && window.location.pathname !== '/metar' && window.location.pathname !== '/charts') return;
    currentView = viewType;

    // Update Button UI
    document.querySelectorAll('.toggle-btn').forEach(btn => btn.classList.remove('active'));
    const btn = document.getElementById(`btn-${viewType}`);
    if (btn) btn.classList.add('active');

    try {
        const response = await fetch(`/api/records/${viewType}`);
        const data = await response.json();

        const displayDate = document.getElementById('display-date');
        if (displayDate) displayDate.textContent = data.date;

        // 🔥 UPDATE CHARTS SYNC WITH VIEW
        if (data.chart_data && typeof updateCharts === 'function') {
            console.log(`[CHART] Syncing charts with ${viewType} data...`);
            updateCharts(
                data.chart_data.labels,
                data.chart_data.temps,
                data.chart_data.pressures,
                data.chart_data.winds,
                data.chart_data.gusts
            );

            // Update info text for charts
            const infoText = `${data.date} • ${data.count} records (View: ${viewType})`;
            ['tempChart-info', 'pressureChart-info', 'windChart-info'].forEach(id => {
                const el = document.getElementById(id);
                if (el) el.textContent = infoText;
            });
        }

        const tbody = document.getElementById('table-body');
        if (!tbody) return;
        tbody.innerHTML = '';

        if (!data.records || data.records.length === 0) {
            tbody.innerHTML = `<tr><td colspan="4" class="empty" style="text-align: center; padding: 20px;">Tidak ada records tersedia</td></tr>`;
        } else {
            // 🔥 RE Sequence Validation: records are newest-first
            // records[i] is newer, records[i+1] is the previous observation
            data.records.forEach((record, i) => {
                let statusHtml = '';
                let rowClass = "";
                let metarDisplay = "";

                if (record.is_missing) {
                    rowClass = "metar-row-missing";
                    metarDisplay = `<span class="missing-placeholder-text" style="color: #ef4444; font-weight: bold; display: flex; align-items: center; gap: 6px;">⚠️ DATA METAR TIDAK MASUK</span>`;
                    statusHtml = `
                        <div class="metar-status-indicator metar-status-missing">
                            <span class="metar-status-icon">⚠️</span>
                            <span>MISSING</span>
                            <div class="metar-status-tooltip">
                                <div class="tooltip-title">
                                    <span>⚠️</span>
                                    <span>Data Missing</span>
                                </div>
                                <div class="tooltip-desc">Data METAR pada jam ini tidak masuk/diterima oleh sistem.</div>
                            </div>
                        </div>
                    `;
                } else {
                    // Get the previous (older) record for RE comparison
                    const prevRecord = (i + 1 < data.records.length) ? data.records[i + 1] : null;
                    const seqErrors = (prevRecord && !prevRecord.is_missing) ? checkRESequence(record.metar, prevRecord.metar) : [];

                    const status = getMetarStatus(record.metar, record.time, record.validation_results, seqErrors);
                    statusHtml = createStatusIndicatorHTML(status);
                    
                    if (status.type === "speci") {
                        rowClass = "metar-row-speci";
                    } else if (status.type === "invalid") {
                        rowClass = "metar-row-invalid";
                    }

                    metarDisplay = record.metar;
                    if (!metarDisplay.endsWith('=')) metarDisplay += '=';
                }

                const row = `
                    <tr class="history-row ${rowClass}" ${record.is_missing ? 'data-missing="true"' : ''}>
                        <td class="col-time">${record.time}</td>
                        <td class="col-station">${record.station}</td>
                        <td class="metar-cell col-metar">${metarDisplay}</td>
                        <td class="col-status">${statusHtml}</td>
                    </tr>
                `;
                tbody.insertAdjacentHTML('beforeend', row);
            });
        }

        // Update count badge
        const countBadge = document.getElementById(`count-${viewType}`);
        if (countBadge) countBadge.textContent = data.count || 0;
    } catch (e) {
        console.error('Failed to load view:', e);
    }
}

// Auto-refresh setiap 1 menit hanya jika di view 'today'
setInterval(() => {
    if (window.location.pathname === '/charts') {
        if (currentChartPeriod === 'today') loadChartPeriod('today');
        return;
    }
    if (currentView === 'today') loadView('today');
}, 60000);

// Load data awal
document.addEventListener('DOMContentLoaded', () => {
    if (window.location.pathname !== '/charts') loadView('today');
});
// =======================
// MODAL HANDLERS (CITATION)
// =======================
function openCitationModal() {
    console.log('[UI] Opening Citation Modal');
    const overlay = document.getElementById('citationModalOverlay');
    const modal = document.getElementById('citationModal');
    if (overlay) overlay.classList.add('active');
    if (modal) modal.classList.add('active');
    document.body.style.overflow = 'hidden'; // Prevent scroll
}

function closeCitationModal() {
    const overlay = document.getElementById('citationModalOverlay');
    const modal = document.getElementById('citationModal');
    if (overlay) overlay.classList.remove('active');
    if (modal) modal.classList.remove('active');
    document.body.style.overflow = 'auto'; // Restore scroll
}

// Close modal when clicking outside content (handled by onclick on overlay div)

// ============================================
// INITIALIZATION ON PAGE LOAD
// ============================================
document.addEventListener('DOMContentLoaded', () => {
    // Check if we are on a page with METAR data already loaded (like the dashboard)
    const rawEl = document.getElementById('metarRawCode');
    if (rawEl && rawEl.textContent) {
        // Remove any DOM text content that might include badges or extra spaces
        const rawMetar = rawEl.textContent.replace(/⚠️.*/g, '').trim();
        if (rawMetar && !window.isManualSession) {
            console.log('[INIT] Checking initial weather effects for:', rawMetar.substring(0, 50));
            // Set lastMetarRaw so other functions have access to it
            lastMetarRaw = rawMetar;
            window.lastProcessedMetar = rawMetar;

            // Run fog check
            if (typeof checkAndActivateFog === 'function') {
                checkAndActivateFog(rawMetar);
            }
            // Run rain check
            if (typeof checkRainStatus === 'function') {
                checkRainStatus(rawMetar);
            }
            // Thunderstorm module
            if (typeof updateThunderstormModule === 'function') {
                updateThunderstormModule(rawMetar);
            }
        }
    }
});

/**
 * Filter baris tabel berdasarkan tipe laporan (METAR/SPECI).
 * Menggunakan row class dan status indicator untuk deteksi yang akurat.
 * Bekerja pada tabel #metar-daily-table (index/metar) dan #history-results-table (history_by_date).
 */
function filterReportType() {
    const filterEl = document.getElementById('reportTypeFilter');
    if (!filterEl) return;
    
    const filter = filterEl.value;
    
    // Support kedua tabel: metar-daily-table (dashboard/metar) dan history-results-table (history_by_date)
    const tables = ['metar-daily-table', 'history-results-table'];
    let totalVisibleCount = 0;

    tables.forEach(function(tableId) {
        const rows = document.querySelectorAll('#' + tableId + ' tbody tr');
        
        rows.forEach(function(row) {
            // Skip empty/placeholder rows
            if (row.querySelector('.empty') || row.querySelector('td[colspan]')) {
                return;
            }

            let isSpeci = false;

            // Method 1: Check row class (paling reliable untuk tabel dinamis dari loadView)
            if (row.classList.contains('metar-row-speci')) {
                isSpeci = true;
            }

            // Method 2: Check status indicator label di kolom STATUS
            if (!isSpeci) {
                const statusCell = row.querySelector('.col-status');
                if (statusCell) {
                    const statusText = statusCell.textContent.toUpperCase().trim();
                    if (statusText.includes('SPECI')) {
                        isSpeci = true;
                    }
                }
            }

            // Method 3: Check METAR data content for "SPECI" keyword
            if (!isSpeci) {
                const metarCell = row.querySelector('.col-metar');
                if (metarCell) {
                    const metarText = metarCell.textContent.toUpperCase().trim();
                    if (metarText.includes('SPECI')) {
                        isSpeci = true;
                    }
                }
            }

            // Method 4: Check data-metar attribute (for history_by_date server-rendered rows)
            if (!isSpeci && row.hasAttribute('data-metar')) {
                const dataMetar = row.getAttribute('data-metar').toUpperCase();
                if (dataMetar.includes('SPECI')) {
                    isSpeci = true;
                }
            }

            let show = true;
            if (filter === 'METAR') {
                show = !isSpeci; // Tampilkan hanya yang BUKAN SPECI
            } else if (filter === 'SPECI') {
                show = isSpeci;  // Tampilkan hanya SPECI
            }
            // filter === 'all' -> tampilkan semua

            row.style.display = show ? '' : 'none';
            if (show) totalVisibleCount++;
        });
    });

    // Update badge count pada tombol aktif (Today/Yesterday) jika ada
    const activeBtn = document.querySelector('.manager-controls .toggle-btn.active');
    if (activeBtn) {
        const badge = activeBtn.querySelector('.badge');
        if (badge) badge.textContent = totalVisibleCount;
    }
    
    // Update count info untuk history_by_date jika ada
    const historyCountEl = document.getElementById('filtered-count-display');
    if (historyCountEl) {
        historyCountEl.textContent = totalVisibleCount;
    }
}

/**
 * Pantau perubahan pada tbody agar filter tetap diterapkan
 * setelah loadView('today') / loadView('yesterday') mengganti data tabel.
 */
document.addEventListener('DOMContentLoaded', function() {
    // Observer untuk tabel dinamis (index.html / metar.html)
    const tbody = document.getElementById('table-body');
    if (tbody) {
        const observer = new MutationObserver(function() {
            // Delay sedikit agar row class sudah di-assign oleh loadView
            setTimeout(filterReportType, 50);
        });
        observer.observe(tbody, { childList: true, subtree: true });
    }
    
    // Untuk history_by_date: terapkan filter langsung setelah DOM ready
    const historyTable = document.getElementById('history-results-table');
    if (historyTable) {
        // Delay agar status indicators sudah di-inject oleh script di history_by_date.html
        setTimeout(filterReportType, 500);
    }
});

// Expose globally
window.filterReportType = filterReportType;

// Early Warning System summary and short-term trend charts.
(() => {
    const charts = {};

    function regressionLine(values) {
        const valid = values.map((value, index) => ({ value: Number(value), index }))
            .filter(point => Number.isFinite(point.value));
        if (valid.length < 2) return values.map(() => null);

        const meanX = valid.reduce((sum, point) => sum + point.index, 0) / valid.length;
        const meanY = valid.reduce((sum, point) => sum + point.value, 0) / valid.length;
        const denominator = valid.reduce((sum, point) => sum + (point.index - meanX) ** 2, 0);
        const slope = denominator ? valid.reduce((sum, point) =>
            sum + (point.index - meanX) * (point.value - meanY), 0) / denominator : 0;
        return values.map((_, index) => meanY + slope * (index - meanX));
    }

    function drawEwsChart(canvasId, rows, key, label, color) {
        const canvas = document.getElementById(canvasId);
        if (!canvas || typeof Chart === 'undefined') return;

        const values = rows.map(row => row[key] === null ? null : Number(row[key]));
        const labels = rows.map(row => {
            const timestamp = String(row.time || '');
            const match = timestamp.match(/(?:T|\s)(\d{1,2}:\d{2})/);
            return match ? match[1] : timestamp;
        });
        if (charts[canvasId]) charts[canvasId].destroy();
        charts[canvasId] = new Chart(canvas, {
            type: 'line',
            data: { labels, datasets: [
                { label, data: values, borderColor: color, backgroundColor: color + '22',
                    pointRadius: 3, pointHoverRadius: 5, tension: 0.25, spanGaps: true },
                { label: 'Regresi linear', data: regressionLine(values), borderColor: '#d97706',
                    borderDash: [5, 4], pointRadius: 0, borderWidth: 2, spanGaps: true }
            ] },
            options: {
                responsive: true, maintainAspectRatio: false,
                interaction: { mode: 'index', intersect: false },
                plugins: { legend: { position: 'bottom' } },
                scales: { y: { beginAtZero: false } }
            }
        });
    }

    function drawEwsExplanationChart(explanation) {
        const canvasId = 'ewsContributionChart';
        const canvas = document.getElementById(canvasId);
        if (!canvas || typeof Chart === 'undefined' || !explanation?.features?.length) return;

        const features = explanation.features.slice(0, 10);
        if (charts[canvasId]) charts[canvasId].destroy();
        charts[canvasId] = new Chart(canvas, {
            type: 'bar',
            data: {
                labels: features.map(feature => feature.label),
                datasets: [{
                    label: 'Kontribusi TreeSHAP',
                    data: features.map(feature => Number(feature.shap_value)),
                    backgroundColor: features.map(feature =>
                        Number(feature.shap_value) >= 0 ? 'rgba(220, 38, 38, 0.78)' : 'rgba(5, 150, 105, 0.78)'
                    ),
                    borderColor: features.map(feature =>
                        Number(feature.shap_value) >= 0 ? '#b91c1c' : '#047857'
                    ),
                    borderWidth: 1,
                    borderRadius: 3,
                    barThickness: 18
                }]
            },
            options: {
                indexAxis: 'y',
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: { display: false },
                    tooltip: {
                        callbacks: {
                            label: context => `Kontribusi: ${Number(context.raw).toFixed(4)} log-odds`,
                            afterLabel: context => {
                                const observed = features[context.dataIndex].value;
                                return `Nilai fitur: ${observed ?? 'tidak tersedia'}`;
                            }
                        }
                    }
                },
                scales: {
                    x: {
                        title: { display: true, text: 'Dampak terhadap skor BAHAYA (log-odds)' },
                        grid: { color: context => context.tick.value === 0 ? '#64748b' : 'rgba(148, 163, 184, 0.18)' }
                    },
                    y: { reverse: true, grid: { display: false } }
                }
            }
        });
    }

    async function loadEwsStatus() {
        const panel = document.getElementById('ewsPanel');
        if (!panel) return;
        const updated = document.getElementById('ewsUpdated');
        try {
            const response = await fetch('/api/ews-status', { headers: { Accept: 'application/json' } });
            const data = await response.json();
            if (!response.ok) {
                const diagnostic = [data.stage, data.error_code, data.missing_module]
                    .filter(Boolean).join(' / ');
                const message = data.error || 'Data EWS belum tersedia.';
                throw new Error(diagnostic ? `${message} (${diagnostic})` : message);
            }

            const danger = data.status === 'BAHAYA';
            const indicator = document.getElementById('ewsIndicator');
            indicator.dataset.state = danger ? 'danger' : 'safe';
            document.getElementById('ewsStatus').textContent = data.status;
            document.getElementById('ewsConfidence').textContent = `${data.confidence_percent}%`;
            document.getElementById('ewsDescription').textContent = data.deskripsi;
            document.getElementById('ewsMetar').textContent = `METAR terbaru: ${data.metar_terbaru}`;
            drawEwsChart('ewsWindChart', data.history, 'wind_speed_kt', 'Kecepatan angin', '#0f766e');
            drawEwsChart('ewsQnhChart', data.history, 'qnh_hpa', 'QNH', '#2563a6');
            drawEwsExplanationChart(data.explanation);
            updated.textContent = `Diperbarui ${new Date().toLocaleTimeString('id-ID')}`;
        } catch (error) {
            document.getElementById('ewsIndicator').dataset.state = 'error';
            document.getElementById('ewsStatus').textContent = 'TIDAK TERSEDIA';
            document.getElementById('ewsConfidence').textContent = '--%';
            document.getElementById('ewsDescription').textContent = error.message;
            updated.textContent = 'Gagal memperbarui';
        }
    }

    document.addEventListener('DOMContentLoaded', loadEwsStatus);
})();


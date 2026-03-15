#!/usr/bin/env python3
"""
plex_health_monitor.py — Plex Media Server health & playback watchdog
Project: plex-health-monitor  v2.0.0
Machine: Mac Pro (mMacPro) at /Users/mProAdmin

Monitors:
  - Plex process alive
  - NFS music/media mounts responsive
  - Active sessions stuck (no progress for N seconds)
  - Boot NVMe cache dir not growing beyond threshold
  - Session metadata: media type, client, platform, resolution, transcode decision

Prometheus metrics exposed on :9101/metrics:
  plex_up, plex_active_sessions, plex_active_streams (by media_type/platform/user/decision)
  plex_direct_streams, plex_transcoding_streams, plex_stream_info
  plex_stream_bitrate_kbps, plex_library_section
  plex_stuck_sessions_total, plex_restart_count_total, plex_nfs_mounts_ok
  plex_session_type_total (cumulative counter: music vs video)
  plex_hour_activity (gauge: sessions active in last poll, labelled by hour bucket)

Usage:
  python3 plex_health_monitor.py [--dry-run] [--token TOKEN] [--interval 60]

Run as daemon via launchd: com.capes.plex-health-monitor
"""

import argparse
import json
import logging
import os
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ── Config ────────────────────────────────────────────────────────────────────
PLEX_URL        = "http://localhost:32400"
PLEX_DATA_DIR   = Path("/Volumes/6tb-R1/Plex Media Server")
PLEX_CACHE_DIR  = Path("/Volumes/6tb-R1/PlexCache")
PLEX_APP        = Path("/Applications/Plex Media Server.app")
BOOT_CACHE_PATH = Path.home() / "Library/Caches/PlexMediaServer"

NFS_MUSIC_PATHS = [
    Path("/Volumes/music/managed"),
    Path("/Volumes/music/singles"),
    Path("/Volumes/music"),
]
NFS_MEDIA_PATHS = [
    Path("/Volumes/tv"),
    Path("/Volumes/movies"),
]

# Thresholds
STUCK_SESSION_SECS   = 120    # Session with no progress = hung
MAX_BOOT_CACHE_GB    = 1.0    # Alert if boot-volume Plex cache exceeds 1 GB
RESTART_COOLDOWN_SECS = 300   # Min seconds between auto-restarts
CHECK_INTERVAL_SECS  = 60     # How often to poll (override with --interval)
MAX_RESTART_ATTEMPTS = 3      # Give up after this many restarts per hour
METRICS_PORT         = 9101   # Prometheus scrape port

# Media type classification — titles matching these are video, else music/audio
VIDEO_CLIENTS = {"plex-web", "plexmediaplayer", "plexforandroidtv", "plexforios",
                 "appletv", "roku", "chromecast", "shieldandroidtv"}
MUSIC_CLIENTS = {"plexamp"}

# Known video titles from log history (seed set; grows via type field at runtime)
_VIDEO_TITLE_CACHE: set = set()

# ── Prometheus metrics registry ───────────────────────────────────────────────
_metrics: Dict[str, str] = {}
_metrics_lock = threading.Lock()

def set_metric(name: str, value, labels: Dict[str, str] = None) -> None:
    """Set a Prometheus gauge/counter metric."""
    if labels:
        label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
        key = f"{name}{{{label_str}}}"
    else:
        key = name
    with _metrics_lock:
        _metrics[key] = str(value)

def inc_metric(name: str, labels: Dict[str, str] = None) -> None:
    """Increment a counter metric by 1."""
    if labels:
        label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
        key = f"{name}{{{label_str}}}"
    else:
        key = name
    with _metrics_lock:
        prev = int(_metrics.get(key, 0))
        _metrics[key] = str(prev + 1)

def clear_prefix(prefix: str) -> None:
    """Remove all metrics starting with prefix."""
    with _metrics_lock:
        for k in [k for k in _metrics if k.startswith(prefix)]:
            del _metrics[k]

class MetricsHandler(BaseHTTPRequestHandler):
    """Minimal Prometheus /metrics HTTP handler."""
    def do_GET(self):
        if self.path != "/metrics":
            self.send_response(404); self.end_headers(); return
        with _metrics_lock:
            lines = [f"{k} {v}" for k, v in _metrics.items()]
        body = "\n".join(lines) + "\n"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body.encode())
    def log_message(self, *args):
        pass  # suppress access logs

def start_metrics_server(port: int) -> None:
    """Start Prometheus metrics HTTP server in a daemon thread."""
    server = HTTPServer(("0.0.0.0", port), MetricsHandler)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()

# ── Logging setup ─────────────────────────────────────────────────────────────
LOG_DIR = PLEX_DATA_DIR / "Logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

def setup_logging(log_dir: Path) -> logging.Logger:
    """Configure logging to stdout and daily timestamped file."""
    ts = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"plex-health-{ts}.log"
    logger = logging.getLogger("plex-health")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    if not logger.handlers:
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        logger.addHandler(sh)
        fh = logging.FileHandler(log_file)
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    return logger

# ── Helpers ───────────────────────────────────────────────────────────────────
def notify(title: str, msg: str) -> None:
    """Send macOS notification."""
    subprocess.run(["osascript", "-e",
                    f'display notification "{msg}" with title "{title}"'],
                   capture_output=True)

def plex_running() -> Optional[int]:
    """Return PID of Plex Media Server or None."""
    r = subprocess.run(["pgrep", "-x", "Plex Media Server"],
                       capture_output=True, text=True)
    pids = r.stdout.strip().split()
    return int(pids[0]) if pids else None

def plex_api(endpoint: str, token: str, timeout: int = 10) -> Optional[ET.Element]:
    """Make a Plex API call, return parsed XML root or None."""
    import urllib.request, urllib.error
    url = f"{PLEX_URL}{endpoint}?X-Plex-Token={token}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return ET.fromstring(r.read())
    except Exception:
        return None

def classify_media_type(item_type: str, client_name: str, title: str) -> str:
    """Return 'music', 'video', or 'other' for a session item."""
    t = item_type.lower() if item_type else ""
    c = (client_name or "").lower().replace(" ", "")
    if t in ("track", "album", "artist") or c in MUSIC_CLIENTS:
        return "music"
    if t in ("movie", "episode", "clip") or c in VIDEO_CLIENTS:
        return "video"
    # Fallback: check our growing cache of known video titles
    if title in _VIDEO_TITLE_CACHE:
        return "video"
    return "other"

def get_sessions(token: str) -> List[Dict]:
    """Return list of enriched active playback session dicts."""
    root = plex_api("/status/sessions", token)
    if root is None:
        return []
    sessions = []
    for item in list(root):
        player   = item.find("Player")
        user_el  = item.find("User")
        ts_el    = item.find("TranscodeSession")
        media_el = item.find("Media")

        item_type   = item.get("type", "")
        title       = item.get("grandparentTitle", item.get("title", "?"))
        client_name = player.get("product", "") if player is not None else ""
        platform    = player.get("platform", "unknown") if player is not None else "unknown"
        device      = player.get("title", "unknown") if player is not None else "unknown"
        state       = player.get("state", "?") if player is not None else "?"
        username    = user_el.get("title", "?") if user_el is not None else "?"
        view_offset = int(item.get("viewOffset", 0))

        # Transcode decision
        if ts_el is not None:
            decision    = ts_el.get("videoDecision", ts_el.get("audioDecision", "transcode"))
            bitrate     = int(ts_el.get("speed", 0))
            resolution  = ts_el.get("videoResolution", "unknown")
        elif media_el is not None:
            decision    = "direct"
            bitrate     = int(media_el.get("bitrate", 0))
            resolution  = media_el.get("videoResolution", "unknown")
        else:
            decision, bitrate, resolution = "direct", 0, "unknown"

        media_type = classify_media_type(item_type, client_name, title)

        sessions.append({
            "key":        item.get("key", ""),
            "title":      title,
            "state":      state,
            "user":       username,
            "viewOffset": view_offset,
            "type":       item_type,
            "media_type": media_type,
            "client":     client_name or "unknown",
            "platform":   platform,
            "device":     device,
            "decision":   decision,
            "bitrate":    bitrate,
            "resolution": resolution,
        })
    return sessions

def get_library_sections(token: str) -> List[Dict]:
    """Return library section metadata for plex_library_section metric."""
    root = plex_api("/library/sections", token)
    if root is None:
        return []
    sections = []
    for d in root.findall("Directory"):
        sections.append({
            "key":   d.get("key", ""),
            "title": d.get("title", "?"),
            "type":  d.get("type", "?"),
        })
    return sections

def check_nfs_mounts(logger: logging.Logger) -> bool:
    """Verify NFS mounts are alive. Return True if all OK."""
    all_ok = True
    for p in NFS_MUSIC_PATHS + NFS_MEDIA_PATHS:
        try:
            r = subprocess.run(["ls", str(p)], capture_output=True, timeout=5)
            if r.returncode != 0:
                logger.warning("NFS mount unresponsive: %s", p)
                all_ok = False
        except subprocess.TimeoutExpired:
            logger.error("NFS mount TIMED OUT (hung): %s", p)
            all_ok = False
    return all_ok

def check_boot_cache(logger: logging.Logger) -> None:
    """Alert if Plex cache has leaked back onto the boot NVMe."""
    if BOOT_CACHE_PATH.is_symlink():
        return
    if BOOT_CACHE_PATH.exists():
        r = subprocess.run(["du", "-sk", str(BOOT_CACHE_PATH)],
                           capture_output=True, text=True)
        kb = int(r.stdout.split()[0]) if r.stdout else 0
        gb = kb / 1024 / 1024
        if gb > MAX_BOOT_CACHE_GB:
            logger.warning(
                "Boot NVMe Plex cache is %.1f GB (limit %.1f GB) — "
                "run fix_plex_storage.sh to relocate", gb, MAX_BOOT_CACHE_GB)
            notify("Plex Cache Warning",
                   f"Plex cache on boot NVMe is {gb:.1f} GB. Run fix_plex_storage.sh.")

def restart_plex(logger: logging.Logger, dry_run: bool) -> bool:
    """Kill and relaunch Plex. Return True on success."""
    logger.warning("⚡ Restarting Plex Media Server...")
    notify("Plex Health Monitor", "Restarting Plex due to playback hang.")
    if dry_run:
        logger.info("[DRY-RUN] Would restart Plex here")
        return True
    subprocess.run(["pkill", "-x", "Plex Media Server"], capture_output=True)
    time.sleep(4)
    subprocess.run(["open", "-a", str(PLEX_APP)], capture_output=True)
    time.sleep(6)
    pid = plex_running()
    if pid:
        logger.info("✅ Plex restarted successfully (PID %d)", pid)
        return True
    else:
        logger.error("❌ Plex failed to restart — manual intervention required")
        notify("Plex Health Monitor", "⚠️ Plex failed to restart! Check Mac Pro.")
        return False

def update_session_metrics(sessions: List[Dict]) -> None:
    """Push rich per-session and aggregate metrics to Prometheus."""
    # Clear per-session labels before rewriting
    clear_prefix("plex_stream_info")
    clear_prefix("plex_stream_bitrate_kbps")

    direct = sum(1 for s in sessions if s["decision"] == "direct")
    transcode = sum(1 for s in sessions if s["decision"] != "direct")

    set_metric("plex_active_sessions",   len(sessions), {"host": "macpro"})
    set_metric("plex_active_streams",    len(sessions), {"host": "macpro"})
    set_metric("plex_direct_streams",    direct,        {"host": "macpro"})
    set_metric("plex_transcoding_streams", transcode,   {"host": "macpro"})

    # Per-stream info gauge (presence = 1)
    for s in sessions:
        labels = {
            "host":       "macpro",
            "user":       s["user"],
            "media_type": s["media_type"],
            "platform":   s["platform"],
            "device":     s["device"][:30],
            "decision":   s["decision"],
            "video_resolution": s["resolution"],
        }
        set_metric("plex_stream_info", 1, labels)
        if s["bitrate"] > 0:
            set_metric("plex_stream_bitrate_kbps", s["bitrate"],
                       {"host": "macpro", "user": s["user"],
                        "media_type": s["media_type"]})

def update_library_metrics(token: str) -> None:
    """Refresh plex_library_section gauges (called every 10 min)."""
    sections = get_library_sections(token)
    clear_prefix("plex_library_section")
    for sec in sections:
        set_metric("plex_library_section", 1,
                   {"host": "macpro", "key": sec["key"],
                    "title": sec["title"], "type": sec["type"]})

# ── Main watchdog loop ────────────────────────────────────────────────────────
def run_monitor(token: str, interval: int, dry_run: bool,
                metrics_port: int = METRICS_PORT) -> None:
    """Main monitoring loop."""
    logger = setup_logging(LOG_DIR)
    logger.info("🟢 Plex Health Monitor v2.0.0 starting "
                "(interval=%ds, dry_run=%s, metrics_port=%d)",
                interval, dry_run, metrics_port)

    start_metrics_server(metrics_port)
    logger.info("📊 Prometheus metrics endpoint: http://0.0.0.0:%d/metrics", metrics_port)

    # Initialise static metrics
    set_metric("plex_health_monitor_up",    1, {"host": "macpro"})
    set_metric("plex_up",                   0, {"host": "macpro"})
    set_metric("plex_active_sessions",      0, {"host": "macpro"})
    set_metric("plex_active_streams",       0, {"host": "macpro"})
    set_metric("plex_direct_streams",       0, {"host": "macpro"})
    set_metric("plex_transcoding_streams",  0, {"host": "macpro"})
    set_metric("plex_stuck_sessions_total", 0, {"host": "macpro"})
    set_metric("plex_restart_count_total",  0, {"host": "macpro"})
    set_metric("plex_nfs_mounts_ok",        1, {"host": "macpro"})
    # Cumulative session type counters
    set_metric("plex_session_type_total", 0, {"host": "macpro", "media_type": "music"})
    set_metric("plex_session_type_total", 0, {"host": "macpro", "media_type": "video"})
    set_metric("plex_session_type_total", 0, {"host": "macpro", "media_type": "other"})

    session_tracker: Dict[str, Tuple[int, float]] = {}
    last_restart_time: float = 0.0
    restart_count_hour: int = 0
    restart_window_start: float = time.time()
    library_refresh_at: float = 0.0  # force refresh on first loop
    poll_count: int = 0

    while True:
        now = time.time()
        poll_count += 1

        # Reset hourly restart counter
        if now - restart_window_start > 3600:
            restart_count_hour = 0
            restart_window_start = now

        # ── Check 1: Plex process ─────────────────────────────────────────────
        pid = plex_running()
        if not pid:
            logger.error("❌ Plex Media Server is NOT running!")
            set_metric("plex_up", 0, {"host": "macpro"})
            set_metric("plex_active_sessions", 0, {"host": "macpro"})
            set_metric("plex_active_streams", 0, {"host": "macpro"})
            notify("Plex Health Monitor", "Plex is down — restarting...")
            if not dry_run and restart_count_hour < MAX_RESTART_ATTEMPTS:
                if restart_plex(logger, dry_run):
                    restart_count_hour += 1
                    last_restart_time = now
                    inc_metric("plex_restart_count_total", {"host": "macpro"})
            time.sleep(interval)
            continue
        else:
            logger.debug("✅ Plex running (PID %d)", pid)
            set_metric("plex_up", 1, {"host": "macpro"})

        # ── Check 2: NFS mounts ───────────────────────────────────────────────
        nfs_ok = check_nfs_mounts(logger)
        set_metric("plex_nfs_mounts_ok", 1 if nfs_ok else 0, {"host": "macpro"})
        if not nfs_ok:
            notify("Plex Health Monitor", "NFS mount unresponsive. Check mbuntu.")

        # ── Check 3: Boot NVMe cache bleed ───────────────────────────────────
        check_boot_cache(logger)

        # ── Check 4: Library sections (every 10 min) ──────────────────────────
        if now >= library_refresh_at:
            update_library_metrics(token)
            library_refresh_at = now + 600

        # ── Check 5: Active sessions & rich metrics ───────────────────────────
        sessions = get_sessions(token)
        active_keys = set()
        update_session_metrics(sessions)

        # Hour-of-day activity gauge (1 = something playing right now)
        hour_label = datetime.now().strftime("%H")
        set_metric("plex_hour_activity", 1 if sessions else 0,
                   {"host": "macpro", "hour": hour_label})

        for s in sessions:
            key    = s["key"]
            offset = s["viewOffset"]
            state  = s["state"]
            active_keys.add(key)

            if key not in session_tracker:
                session_tracker[key] = (offset, now)
                logger.info(
                    "▶ New session: %s — %s (%s) [%s | %s | %s]",
                    s["user"], s["title"], state,
                    s["media_type"], s["platform"], s["decision"]
                )
                inc_metric("plex_session_type_total",
                           {"host": "macpro", "media_type": s["media_type"]})
                # Track known video titles
                if s["media_type"] == "video":
                    _VIDEO_TITLE_CACHE.add(s["title"])
                continue

            prev_offset, last_changed = session_tracker[key]

            if state == "playing":
                if offset != prev_offset:
                    session_tracker[key] = (offset, now)
                else:
                    stuck_secs = now - last_changed
                    if stuck_secs > STUCK_SESSION_SECS:
                        logger.warning(
                            "⚠️  STUCK SESSION: %s playing '%s' for %.0fs with no progress",
                            s["user"], s["title"], stuck_secs)
                        inc_metric("plex_stuck_sessions_total", {"host": "macpro"})
                        notify("Plex Playback Hung",
                               f"{s['user']} stuck on '{s['title']}' — restarting Plex")
                        if now - last_restart_time > RESTART_COOLDOWN_SECS:
                            if restart_count_hour < MAX_RESTART_ATTEMPTS:
                                if restart_plex(logger, dry_run):
                                    restart_count_hour += 1
                                    last_restart_time = now
                                    inc_metric("plex_restart_count_total",
                                               {"host": "macpro"})
            elif state == "paused":
                session_tracker[key] = (offset, now)  # reset on pause

        # Clean up ended sessions
        for old_key in list(session_tracker.keys()):
            if old_key not in active_keys:
                logger.info("⏹ Session ended: %s", old_key)
                del session_tracker[old_key]

        if not sessions:
            logger.debug("No active sessions")

        time.sleep(interval)


# ── Entry point ───────────────────────────────────────────────────────────────
def main() -> None:
    """Parse args and start monitor."""
    parser = argparse.ArgumentParser(
        description="Plex Health Monitor v2.0.0 — watchdog + rich Prometheus metrics"
    )
    parser.add_argument("--token", default=os.environ.get("PLEX_TOKEN", ""),
                        help="Plex auth token (or set PLEX_TOKEN env var)")
    parser.add_argument("--interval", type=int, default=CHECK_INTERVAL_SECS,
                        help=f"Check interval in seconds (default: {CHECK_INTERVAL_SECS})")
    parser.add_argument("--metrics-port", type=int, default=METRICS_PORT,
                        help=f"Prometheus metrics port (default: {METRICS_PORT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="Log actions but don't restart Plex")
    args = parser.parse_args()

    if not args.token:
        token_file = PLEX_DATA_DIR / ".LocalAdminToken"
        if token_file.exists():
            args.token = token_file.read_text().strip()
        else:
            print("ERROR: No Plex token. Use --token or set PLEX_TOKEN env var.")
            sys.exit(1)

    run_monitor(args.token, args.interval, args.dry_run, args.metrics_port)


if __name__ == "__main__":
    main()

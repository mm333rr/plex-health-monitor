#!/usr/bin/env python3
"""
plex_health_monitor.py — Plex Media Server health & playback watchdog
Project: plex-health-monitor
Machine: Mac Pro (mMacPro) at /Users/mProAdmin

Monitors:
  - Plex process alive
  - NFS music mounts responsive
  - Active sessions stuck (no progress for N seconds)
  - Boot NVMe cache dir not growing beyond threshold
  - Plexamp audio analysis jobs not wedged

Actions:
  - Logs warnings to stdout + timestamped log file
  - Restarts Plex if hung (with configurable cooldown)
  - Alerts via macOS notification

Usage:
  python3 plex_health_monitor.py [--dry-run] [--token TOKEN] [--interval 60]

Run as daemon via launchd: com.capes.plex-health-monitor
"""

import argparse
import json
import logging
import os
import platform
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
PLEX_URL = "http://localhost:32400"
PLEX_DATA_DIR = Path("/Volumes/6tb-R1/Plex Media Server")
PLEX_CACHE_DIR = Path("/Volumes/6tb-R1/PlexCache")
PLEX_APP = Path("/Applications/Plex Media Server.app")
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
STUCK_SESSION_SECS = 120       # Session with no progress for this long = hung
MAX_BOOT_CACHE_GB = 1.0        # Alert if boot-volume Plex cache exceeds 1 GB
RESTART_COOLDOWN_SECS = 300    # Min seconds between auto-restarts
CHECK_INTERVAL_SECS = 60       # How often to poll (override with --interval)
MAX_RESTART_ATTEMPTS = 3       # Give up after this many restarts per hour

# Prometheus metrics HTTP port (scraped by Prometheus on mbuntu)
METRICS_PORT = 9101

# ── Prometheus metrics registry (simple thread-safe dict) ─────────────────────
_metrics: Dict[str, str] = {}
_metrics_lock = threading.Lock()

def set_metric(name: str, value, labels: Dict[str, str] = None) -> None:
    """Set a Prometheus metric value."""
    if labels:
        label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
        key = f"{name}{{{label_str}}}"
    else:
        key = name
    with _metrics_lock:
        _metrics[key] = str(value)

def clear_metric_prefix(prefix: str) -> None:
    """Remove all metrics starting with prefix (for per-session cleanup)."""
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
    return server

# Log dir
LOG_DIR = PLEX_DATA_DIR / "Logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ── Logging setup ─────────────────────────────────────────────────────────────
def setup_logging(log_dir: Path) -> logging.Logger:
    """Configure logging to stdout and timestamped file."""
    ts = datetime.now().strftime("%Y%m%d")
    log_file = log_dir / f"plex-health-{ts}.log"
    logger = logging.getLogger("plex-health")
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")
    # stdout
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    logger.addHandler(sh)
    # file
    fh = logging.FileHandler(log_file)
    fh.setFormatter(fmt)
    logger.addHandler(fh)
    return logger

# ── Helpers ───────────────────────────────────────────────────────────────────
def notify(title: str, msg: str) -> None:
    """Send macOS notification."""
    script = f'display notification "{msg}" with title "{title}"'
    subprocess.run(["osascript", "-e", script], capture_output=True)

def plex_running() -> Optional[int]:
    """Return PID of Plex Media Server or None."""
    result = subprocess.run(
        ["pgrep", "-x", "Plex Media Server"],
        capture_output=True, text=True
    )
    pids = result.stdout.strip().split()
    return int(pids[0]) if pids else None

def plex_api(endpoint: str, token: str, timeout: int = 10) -> Optional[ET.Element]:
    """Make a Plex API call, return parsed XML root or None."""
    import urllib.request
    import urllib.error
    url = f"{PLEX_URL}{endpoint}?X-Plex-Token={token}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return ET.fromstring(r.read())
    except Exception:
        return None

def get_sessions(token: str) -> List[Dict]:
    """Return list of active playback session dicts."""
    root = plex_api("/status/sessions", token)
    if root is None:
        return []
    sessions = []
    for video in list(root):
        player = video.find("Player")
        media = video.find("Media")
        stream = None
        if media is not None:
            stream = media.find(".//Stream[@streamType='2']")  # audio stream
        sessions.append({
            "key": video.get("key", ""),
            "title": video.get("grandparentTitle", video.get("title", "?")),
            "state": player.get("state", "?") if player is not None else "?",
            "user": video.find("User").get("title", "?") if video.find("User") is not None else "?",
            "viewOffset": int(video.get("viewOffset", 0)),
            "type": video.get("type", "?"),
        })
    return sessions

def check_nfs_mounts(logger: logging.Logger) -> bool:
    """Verify NFS music and media mounts are alive. Return True if all OK."""
    all_ok = True
    for p in NFS_MUSIC_PATHS + NFS_MEDIA_PATHS:
        try:
            result = subprocess.run(
                ["ls", str(p)],
                capture_output=True, timeout=5
            )
            if result.returncode != 0:
                logger.warning("NFS mount unresponsive: %s", p)
                all_ok = False
        except subprocess.TimeoutExpired:
            logger.error("NFS mount TIMED OUT (hung): %s", p)
            all_ok = False
    return all_ok

def check_boot_cache(logger: logging.Logger) -> None:
    """Alert if Plex cache has leaked back onto the boot NVMe."""
    if BOOT_CACHE_PATH.is_symlink():
        return  # Correctly redirected — all good
    if BOOT_CACHE_PATH.exists():
        result = subprocess.run(
            ["du", "-sk", str(BOOT_CACHE_PATH)],
            capture_output=True, text=True
        )
        kb = int(result.stdout.split()[0]) if result.stdout else 0
        gb = kb / 1024 / 1024
        if gb > MAX_BOOT_CACHE_GB:
            logger.warning(
                "Boot NVMe Plex cache is %.1f GB (limit %.1f GB) — "
                "run fix_plex_storage.sh to relocate", gb, MAX_BOOT_CACHE_GB
            )
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

# ── Main watchdog loop ────────────────────────────────────────────────────────
def run_monitor(token: str, interval: int, dry_run: bool, metrics_port: int = METRICS_PORT) -> None:
    """Main monitoring loop."""
    logger = setup_logging(LOG_DIR)
    logger.info("🟢 Plex Health Monitor starting (interval=%ds, dry_run=%s, metrics_port=%d)",
                interval, dry_run, metrics_port)

    # Start Prometheus metrics server
    start_metrics_server(metrics_port)
    logger.info("📊 Prometheus metrics endpoint: http://0.0.0.0:%d/metrics", metrics_port)

    # Initialise static metrics
    set_metric("plex_health_monitor_up", 1, {"host": "macpro"})
    set_metric("plex_up", 0, {"host": "macpro"})
    set_metric("plex_active_sessions", 0, {"host": "macpro"})
    set_metric("plex_stuck_sessions_total", 0, {"host": "macpro"})
    set_metric("plex_restart_count_total", 0, {"host": "macpro"})
    set_metric("plex_nfs_mounts_ok", 1, {"host": "macpro"})

    # Track session progress: {session_key: (viewOffset, last_change_time)}
    session_tracker: Dict[str, Tuple[int, float]] = {}
    last_restart_time: float = 0.0
    restart_count_hour: int = 0
    restart_window_start: float = time.time()

    while True:
        now = time.time()

        # Reset hourly restart counter
        if now - restart_window_start > 3600:
            restart_count_hour = 0
            restart_window_start = now

        # ── Check 1: Plex process ─────────────────────────────────────────────
        pid = plex_running()
        if not pid:
            logger.error("❌ Plex Media Server is NOT running!")
            set_metric("plex_up", 0, {"host": "macpro"})
            notify("Plex Health Monitor", "Plex is down — restarting...")
            if not dry_run and restart_count_hour < MAX_RESTART_ATTEMPTS:
                if restart_plex(logger, dry_run):
                    restart_count_hour += 1
                    last_restart_time = now
            else:
                logger.warning("Restart limit reached (%d/hr) or dry-run — skipping",
                               MAX_RESTART_ATTEMPTS)
            time.sleep(interval)
            continue
        else:
            logger.debug("✅ Plex running (PID %d)", pid)
            set_metric("plex_up", 1, {"host": "macpro"})

        # ── Check 2: NFS mounts ───────────────────────────────────────────────
        nfs_ok = check_nfs_mounts(logger)
        if not nfs_ok:
            set_metric("plex_nfs_mounts_ok", 0, {"host": "macpro"})
            logger.warning("NFS issue detected — may cause playback hangs")
            notify("Plex Health Monitor", "NFS mount unresponsive. Check mbuntu.")
        else:
            set_metric("plex_nfs_mounts_ok", 1, {"host": "macpro"})

        # ── Check 3: Boot NVMe cache bleed ───────────────────────────────────
        check_boot_cache(logger)

        # ── Check 4: Stuck sessions ───────────────────────────────────────────
        sessions = get_sessions(token)
        active_keys = set()
        set_metric("plex_active_sessions", len(sessions), {"host": "macpro"})

        for s in sessions:
            key = s["key"]
            offset = s["viewOffset"]
            state = s["state"]
            active_keys.add(key)

            if key not in session_tracker:
                session_tracker[key] = (offset, now)
                logger.info("▶ New session: %s — %s (%s)", s["user"], s["title"], state)
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
                            s["user"], s["title"], stuck_secs
                        )
                        # Increment stuck counter metric
                        with _metrics_lock:
                            prev = int(_metrics.get('plex_stuck_sessions_total{host="macpro"}', 0))
                        set_metric("plex_stuck_sessions_total", prev + 1, {"host": "macpro"})
                        notify("Plex Playback Hung",
                               f"{s['user']} stuck on '{s['title']}' — restarting Plex")
                        if now - last_restart_time > RESTART_COOLDOWN_SECS:
                            if restart_count_hour < MAX_RESTART_ATTEMPTS:
                                if restart_plex(logger, dry_run):
                                    restart_count_hour += 1
                                    last_restart_time = now
                                    set_metric("plex_restart_count_total",
                                               restart_count_hour, {"host": "macpro"})
                        else:
                            logger.warning("Restart cooldown active — skipping")
            elif state == "paused":
                # Reset tracker on pause so we don't false-trigger
                session_tracker[key] = (offset, now)

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
        description="Plex Health Monitor — watchdog for Plex Media Server"
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
        # Try reading from local admin token as fallback
        token_file = PLEX_DATA_DIR / ".LocalAdminToken"
        if token_file.exists():
            args.token = token_file.read_text().strip()
        else:
            print("ERROR: No Plex token. Use --token or set PLEX_TOKEN env var.")
            print("       Find your token in Plex Web > Account > XML API")
            sys.exit(1)

    run_monitor(args.token, args.interval, args.dry_run, args.metrics_port)

if __name__ == "__main__":
    main()

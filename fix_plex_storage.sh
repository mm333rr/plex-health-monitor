#!/usr/bin/env bash
# fix_plex_storage.sh — Relocate Plex cache/logs off the NVMe boot volume
# Run ONCE manually with Plex stopped. Safe to re-run (idempotent).
# Usage: bash fix_plex_storage.sh [--dry-run]
#
# What it does:
#   1. Stops Plex Media Server
#   2. Moves ~/Library/Caches/PlexMediaServer → /Volumes/6tb-R1/PlexCache
#      and replaces with a symlink
#   3. Creates /Volumes/6tb-R1/PlexTemp/Logs and symlinks into data dir
#   4. Redirects TMPDIR for Plex via launchd env plist
#   5. Restarts Plex

set -euo pipefail

DRY_RUN=false
[[ "${1:-}" == "--dry-run" ]] && DRY_RUN=true

PLEX_CACHE_SRC="$HOME/Library/Caches/PlexMediaServer"
PLEX_CACHE_DST="/Volumes/6tb-R1/PlexCache"
PLEX_DATA="/Volumes/6tb-R1/Plex Media Server"
PLEX_LOGS_DST="/Volumes/6tb-R1/PlexLogs"
PLEX_APP="/Applications/Plex Media Server.app"

log() { echo "[$(date '+%H:%M:%S')] $*"; }
run() {
  if $DRY_RUN; then echo "  [DRY-RUN] $*"; else "$@"; fi
}

log "=== Plex Storage Fix Script ==="
$DRY_RUN && log "DRY RUN MODE — no changes will be made"

# Verify external volumes are mounted
for vol in "/Volumes/6tb-R1" "/Volumes/music"; do
  if ! mountpoint -q "$vol" 2>/dev/null && [[ ! -d "$vol" ]]; then
    log "ERROR: $vol not mounted. Aborting."
    exit 1
  fi
done
log "✅ External volumes present"

# ── STEP 1: Stop Plex ────────────────────────────────────────────────────────
log "Stopping Plex Media Server..."
run osascript -e 'quit app "Plex Media Server"' 2>/dev/null || true
run sleep 3
run pkill -x "Plex Media Server" 2>/dev/null || true
run sleep 2
log "✅ Plex stopped"

# ── STEP 2: Move cache off NVMe ──────────────────────────────────────────────
log "Moving Plex cache: $PLEX_CACHE_SRC → $PLEX_CACHE_DST"
if [[ -L "$PLEX_CACHE_SRC" ]]; then
  log "  Cache already a symlink — skipping move"
elif [[ -d "$PLEX_CACHE_SRC" ]]; then
  run rsync -a --info=progress2 "$PLEX_CACHE_SRC/" "$PLEX_CACHE_DST/"
  run rm -rf "$PLEX_CACHE_SRC"
  run ln -s "$PLEX_CACHE_DST" "$PLEX_CACHE_SRC"
  log "✅ Cache moved and symlinked"
else
  run mkdir -p "$PLEX_CACHE_DST"
  run ln -s "$PLEX_CACHE_DST" "$PLEX_CACHE_SRC"
  log "✅ Cache dir created and symlinked"
fi

# ── STEP 3: Move ~/Library/Logs/Plex off NVMe ────────────────────────────────
PLEX_LOGS_SRC="$HOME/Library/Logs/Plex Media Server"
PLEX_LOGS_DST="/Volumes/6tb-R1/PlexLogs"
log "Moving Plex logs: $PLEX_LOGS_SRC → $PLEX_LOGS_DST"
if [[ -L "$PLEX_LOGS_SRC" ]]; then
  log "  Logs already a symlink — skipping"
elif [[ -d "$PLEX_LOGS_SRC" ]]; then
  run rsync -a "$PLEX_LOGS_SRC/" "$PLEX_LOGS_DST/"
  run rm -rf "$PLEX_LOGS_SRC"
  run ln -s "$PLEX_LOGS_DST" "$PLEX_LOGS_SRC"
  log "✅ Logs moved and symlinked"
else
  run mkdir -p "$PLEX_LOGS_DST"
  run ln -s "$PLEX_LOGS_DST" "$PLEX_LOGS_SRC"
  log "✅ Logs dir created and symlinked"
fi

# Also ensure the in-data-dir Logs folder exists for the watchdog
log "Ensuring Plex data Logs dir exists..."
LOGS_DIR="$PLEX_DATA/Logs"
run mkdir -p "$LOGS_DIR"
run chmod 755 "$LOGS_DIR"
log "✅ Logs dir ready at $LOGS_DIR"

# ── STEP 3b: Fix Butler DB backup path ───────────────────────────────────────
BUTLER_DIR="/Volumes/500g-R1/Plex Data/Databases"
log "Creating Butler DB backup dir: $BUTLER_DIR"
run mkdir -p "$BUTLER_DIR"
log "✅ Butler backup dir ready"

# ── STEP 4: Create dedicated Plex TMPDIR on 6tb ──────────────────────────────
PLEX_TMP="/Volumes/6tb-R1/PlexTmp"
log "Creating Plex TMPDIR at $PLEX_TMP..."
run mkdir -p "$PLEX_TMP"
run chmod 700 "$PLEX_TMP"
log "✅ PlexTmp dir ready"

# Set via launchd plist (the right way on macOS)
PLIST="$HOME/Library/LaunchAgents/com.plexapp.plexmediaserver.plist"
if [[ -f "$PLIST" ]]; then
  log "Found Plex launchd plist — will add TMPDIR env var"
  # Check if already set
  if grep -q "TMPDIR" "$PLIST" 2>/dev/null; then
    log "  TMPDIR already in plist — skipping"
  else
    log "  NOTE: Add manually to plist EnvironmentVariables:"
    log "    <key>TMPDIR</key><string>/Volumes/6tb-R1/PlexTmp</string>"
  fi
else
  log "  No launchd plist found at $PLIST (Plex may be login-item managed)"
  log "  To redirect TMPDIR: set in Plex Server Settings → Transcoder → Temporary directory"
fi

# ── STEP 5: Verify music read access ─────────────────────────────────────────
log "Checking music library access..."
for path in "/Volumes/music/managed" "/Volumes/music/singles"; do
  if [[ -r "$path" ]]; then
    COUNT=$(ls "$path" 2>/dev/null | wc -l | tr -d ' ')
    log "  ✅ $path readable ($COUNT items)"
  else
    log "  ❌ $path NOT readable — check NFS export permissions on mbuntu"
  fi
done

# ── STEP 5b: Install watchdog launchd agent ──────────────────────────────────
WATCHDOG_PLIST_SRC="$(dirname "$0")/com.capes.plex-health-monitor.plist"
WATCHDOG_PLIST_DST="$HOME/Library/LaunchAgents/com.capes.plex-health-monitor.plist"
if [[ -f "$WATCHDOG_PLIST_SRC" ]]; then
  PLEX_TOKEN=$(defaults read com.plexapp.plexmediaserver PlexOnlineToken 2>/dev/null || echo "")
  if [[ -n "$PLEX_TOKEN" ]]; then
    sed "s/YOUR_PLEX_TOKEN_HERE/$PLEX_TOKEN/" "$WATCHDOG_PLIST_SRC" > "$WATCHDOG_PLIST_DST" 2>/dev/null || \
      run cp "$WATCHDOG_PLIST_SRC" "$WATCHDOG_PLIST_DST"
    log "✅ Watchdog plist installed with token"
  else
    run cp "$WATCHDOG_PLIST_SRC" "$WATCHDOG_PLIST_DST"
    log "⚠️  Watchdog plist installed — edit $WATCHDOG_PLIST_DST to add your Plex token"
  fi
  $DRY_RUN || launchctl unload "$WATCHDOG_PLIST_DST" 2>/dev/null || true
  run launchctl load "$WATCHDOG_PLIST_DST"
  log "✅ Watchdog launchd agent loaded"
else
  log "  Watchdog plist not found at $WATCHDOG_PLIST_SRC — skipping"
fi

# ── STEP 6: Restart Plex ─────────────────────────────────────────────────────
log "Restarting Plex Media Server..."
run open -a "$PLEX_APP"
run sleep 5

# Verify it came back
if pgrep -x "Plex Media Server" > /dev/null; then
  log "✅ Plex running (PID: $(pgrep -x 'Plex Media Server'))"
else
  log "⚠️  Plex did not start — check manually"
fi

# ── SUMMARY ───────────────────────────────────────────────────────────────────
log ""
log "=== Summary ==="
log "Plex data dir:     $PLEX_DATA (on 6tb-R1)"
log "Plex cache dir:    $PLEX_CACHE_SRC → $PLEX_CACHE_DST (on 6tb-R1)"
log "Plex logs dir:     $PLEX_LOGS_SRC → $PLEX_LOGS_DST (on 6tb-R1)"
log "Plex temp dir:     $PLEX_TMP (on 6tb-R1)"
log "Butler DB backup:  $BUTLER_DIR (on 500g-R1)"
log ""
log "Boot NVMe should now only hold the Plex app (~500MB) + OS"
log "Run 'plex-health-monitor.py' to watch for playback hangs"

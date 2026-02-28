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

# ── STEP 3: Fix Logs dir permissions ─────────────────────────────────────────
log "Ensuring Logs dir exists and is writable..."
LOGS_DIR="$PLEX_DATA/Logs"
run mkdir -p "$LOGS_DIR"
run chmod 755 "$LOGS_DIR"
log "✅ Logs dir ready at $LOGS_DIR"

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
log "Plex temp dir:     $PLEX_TMP (on 6tb-R1)"
log "Plex logs dir:     $LOGS_DIR (on 6tb-R1)"
log ""
log "Boot NVMe should now only hold the Plex app (~500MB) + OS"
log "Run 'plex-health-monitor.py' to watch for playback hangs"

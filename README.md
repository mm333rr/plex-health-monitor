# plex-health-monitor

Plex Media Server watchdog for The Capes homelab (Mac Pro, Ventura CA).

Monitors Plex for playback hangs, NFS mount failures, process death, and Plex
cache bleeding onto the boot NVMe. Auto-restarts Plex when stuck sessions are
detected and sends macOS notifications.

## Architecture

```
plex_health_monitor.py   — Main watchdog daemon
fix_plex_storage.sh      — One-time storage relocation fix (run first!)
com.capes.plex-health-monitor.plist  — launchd agent
```

Plex data layout (all on external volumes, NOT boot NVMe):

| Path | Volume | Purpose |
|---|---|---|
| `/Volumes/6tb-R1/Plex Media Server/` | 6tb-R1 | Plex data dir (symlinked from ~/Library/...) |
| `/Volumes/6tb-R1/PlexCache/` | 6tb-R1 | Plex cache (symlinked from ~/Library/Caches/...) |
| `/Volumes/6tb-R1/PlexTemp/` | 6tb-R1 | Transcoder + download temp |
| `/Volumes/6tb-R1/PlexTmp/` | 6tb-R1 | TMPDIR for Plex process |
| `/Volumes/music/managed/` | NFS tank | Lidarr-managed music library |
| `/Volumes/music/singles/` | NFS tank | Singles / misc music |

## Quick Start

### Step 1 — Fix storage (run once with Plex stopped)
```bash
bash "/Users/mProAdmin/Claude Scripts and Venvs/plex-health-monitor/fix_plex_storage.sh"
# or dry-run first:
bash "/Users/mProAdmin/Claude Scripts and Venvs/plex-health-monitor/fix_plex_storage.sh" --dry-run
```

### Step 2 — Get your Plex token
Open in browser: `http://localhost:32400/web` → Account → XML API → copy token
Or read from: `/Volumes/6tb-R1/Plex Media Server/Plug-in Support/Databases/` via Preferences.xml

### Step 3 — Test run (foreground, no restart)
```bash
python3 "/Users/mProAdmin/Claude Scripts and Venvs/plex-health-monitor/plex_health_monitor.py" \
  --token YOUR_TOKEN --interval 30 --dry-run
```

### Step 4 — Install launchd agent
```bash
# Edit plist first — add your real token:
nano "/Users/mProAdmin/Claude Scripts and Venvs/plex-health-monitor/com.capes.plex-health-monitor.plist"

# Copy to LaunchAgents and load:
cp "/Users/mProAdmin/Claude Scripts and Venvs/plex-health-monitor/com.capes.plex-health-monitor.plist" \
   ~/Library/LaunchAgents/

launchctl load ~/Library/LaunchAgents/com.capes.plex-health-monitor.plist
launchctl start com.capes.plex-health-monitor
```

### Watch / Stop / Restart
```bash
# Watch logs live
tail -f "/Volumes/6tb-R1/Plex Media Server/Logs/plex-health-monitor.log"

# Stop daemon
launchctl stop com.capes.plex-health-monitor
launchctl unload ~/Library/LaunchAgents/com.capes.plex-health-monitor.plist

# Restart daemon
launchctl stop com.capes.plex-health-monitor
launchctl start com.capes.plex-health-monitor

# Check status
launchctl list | grep plex
```

## CLI Flags

| Flag | Default | Description |
|---|---|---|
| `--token TOKEN` | `$PLEX_TOKEN` env | Plex auth token |
| `--interval N` | 60 | Poll interval in seconds |
| `--dry-run` | false | Log without restarting Plex |

## What It Checks

1. **Plex process** — restarts if not running
2. **NFS mounts** — warns if music/tv/movies mounts are hung (5s timeout)
3. **Boot NVMe cache bleed** — alerts if `~/Library/Caches/PlexMediaServer` exists (not symlink) and exceeds 1 GB
4. **Stuck sessions** — any "playing" session with no viewOffset progress for 120s triggers restart
5. **Restart limits** — max 3 restarts/hour, 5-minute cooldown between restarts

## Known Issues / Quirks

- Plex `TMPDIR` defaults to `/var/folders/...` on boot NVMe. Set in Plex Server Settings → Transcoder Temporary Directory to `/Volumes/6tb-R1/PlexTemp`
- FSEventStreamCreate errors in system log are benign on NFS mounts — Plex can't watch NFS dirs for changes, so set library scan to Scheduled (not Automatic) in Plex settings
- Music files owned by uid 112 (mbuntu's plex user) — mProAdmin can read but not write. Plexamp lyric/analysis writes go through Plex server so this is fine as long as Plex server runs as mProAdmin and has write to its own data dir
- Plexamp requires a Plex Pass subscription for full offline/download features

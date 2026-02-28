# CHANGELOG — plex-health-monitor

## [1.0.0] — 2026-02-28

### Added
- `plex_health_monitor.py` — watchdog daemon with session hang detection,
  NFS mount checks, process monitoring, boot cache bleed detection
- `fix_plex_storage.sh` — one-time migration: moves Plex cache off boot NVMe
  to /Volumes/6tb-R1/PlexCache, fixes Logs dir permissions, creates PlexTmp
- `com.capes.plex-health-monitor.plist` — launchd agent (KeepAlive, RunAtLoad)
- README.md with full setup, CLI flags, watch/stop/restart commands

### Findings from initial audit
- Plex data dir correctly on 6tb-R1 via symlink ✅
- TranscoderTemp/DownloadsTemp correctly on 6tb-R1 ✅
- **BUG: 7.7 GB Plex cache on boot NVMe** (PhotoTranscoder 7.4 GB) — fixed by fix_plex_storage.sh
- **BUG: Logs dir empty** — Plex not writing logs, fixed by ensuring dir permissions
- FSEventStreamCreate error in system log — NFS watch limitation (benign, configure scheduled scans)
- Music NFS readable by mProAdmin (uid 501) — world-readable NFS dirs, OK for playback

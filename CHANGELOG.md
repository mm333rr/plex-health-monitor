# CHANGELOG — plex-health-monitor

## [2.0.0] — 2026-03-14

### Added
- `classify_media_type()`: classifies every session as `music`, `video`, or `other`
  using Plex item type, client product name, and a growing runtime title cache
- `plex_stream_info` gauge with labels: `user`, `media_type`, `platform`, `device`,
  `decision`, `video_resolution` — enables all Client Breakdown bargauge panels
- `plex_stream_bitrate_kbps` per user/media_type
- `plex_session_type_total` cumulative counter split by `music` / `video` / `other`
- `plex_hour_activity` gauge (1 if streaming in current hour) — powers usage heatmap
- `plex_library_section` gauges refreshed every 10 min — powers Library table panel
- `plex_direct_streams` and `plex_transcoding_streams` split counters
- `get_library_sections()`: polls `/library/sections` for section metadata
- `update_session_metrics()`: atomic clear-and-rewrite of per-session label sets
- `inc_metric()`: safe incrementing counter helper (replaces read-modify-write)
- New session log line now includes `[media_type | platform | decision]`
- Grafana dashboard `plex.json` v2 (31 panels) deployed to mbuntu — replaces stub

### Changed
- Metrics server thread: suppress access logs, clean handler shutdown
- `setup_logging()`: guard against duplicate handlers on reload
- Session tracker cleanup: ended sessions log at INFO not DEBUG

### Analysis basis
- 744 Plex session events parsed across Feb 28–Mar 14 2026
- Peak Plexamp usage: **12:00–18:00 daily**, peak hour **17:00**
- Video sessions: rare, cluster at **23:00** (movies) and **10:00–11:00** (catch-up TV)
- Quiet window for background jobs: **02:00–07:00** (99–100% idle)
- Butler maintenance window reset to **02:00–05:00** to protect daytime streams

## [1.2.0] — 2026-02-28

### Added
- `plex_health_monitor.py`: Prometheus `/metrics` endpoint on port 9101 — exports
  `plex_up`, `plex_active_sessions`, `plex_stuck_sessions_total`, `plex_restart_count_total`,
  `plex_nfs_mounts_ok`, `plex_health_monitor_up`
- `promtail-config.yml`: Mac Pro Promtail agent shipping 4 Plex log streams to Loki:
  `plex` (main server), `plex-scanner`, `plex-transcoder`, `plex-health-monitor`
- `com.capes.promtail.plist`: launchd agent for Promtail on Mac Pro (port 9081)
- Prometheus scrape job `plex-health-macpro` added to mbuntu observability stack

### Fixed
- `com.capes.plex-health-monitor.plist`: removed `StartInterval` (was killing daemon every 60s),
  added `--metrics-port 9101` arg
- `plex_health_monitor.py`: fixed `global METRICS_PORT` SyntaxError — now passes port as arg
- `promtail-config.yml`: removed unsupported `label_keep` stage (not in this Promtail version)

### Verified
- `plex-health-macpro` Prometheus target: **up** ✅
- Loki label/job values: `plex`, `plex-health-monitor`, `plex-scanner`, `plex-transcoder` ✅
- Promtail on Mac Pro port 9081: **Ready** ✅
- `plex_active_sessions=1` live during runningrock session ✅

## [1.1.0] — 2026-02-28

### Fixed
- `fix_plex_storage.sh`: Also migrates `~/Library/Logs/Plex Media Server` → `/Volumes/6tb-R1/PlexLogs` (was 72 MB on NVMe)
- `fix_plex_storage.sh`: Creates Butler DB backup dir on 500g-R1 (`/Volumes/500g-R1/Plex Data/Databases`)
- `fix_plex_storage.sh`: Auto-installs and loads the watchdog launchd agent with real Plex token

### Executed & Verified
- Cache (7.7 GB) migrated from NVMe → 6tb-R1, symlink in place ✅
- Logs (72 MB) migrated from NVMe → 6tb-R1, symlink in place ✅
- Watchdog running as launchd agent (PID 47920) ✅
- Plex API responding HTTP 200 ✅
- Boot NVMe Plex footprint now: only 4 KB plist (Preferences) + app binary ✅
- All `buildIndexFile: part has no video stream` errors confirmed benign (music analysis) ✅

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

# omarchy-iptv

Lightweight single-provider IPTV browser for Omarchy Quattro.

* **One provider**: m3u playlist **or** Xtream Codes (Live + VOD + Series)
* **Series** (Xtream): browse or search shows by category, open one for its seasons and episodes, play an episode in mpv. The catalog syncs with everything else; a show's episodes are fetched on demand (one `get_series_info` call, cached 6 h under `~/.cache/omarchy-iptv/series/`). Favorite a show with ★. A catalog over 2000 shows asks for a group or a query first, like VOD.
* **Stream to TV**: send any row to a Samsung Smart TV (or any DLNA/UPnP renderer) on the same network instead of mpv. See below.
* **EPG**: XMLTV (`epg` url for m3u, `xmltv.php` for Xtream), now/next only — no week-long grids in memory
* **Themed**: zero hardcoded colors; every surface uses `Color` / `Style` / `Border` shell tokens, so theme switches apply live
* **Light**: QML is browser-only; playback is an external `mpv` window. Stream URLs stay out of the shell. VOD is searched on demand rather than loaded in full.

## Authorized use

This plugin is a player. It does not provide, sell, or host any streams. You must only connect playlists and Xtream accounts you are authorized to use (your own provider subscription, officially redistributable public lists, etc.).

Unauthorized access to copyrighted broadcasts is your responsibility, not the authors'. The software is provided as-is; see `LICENSE`.

## Install

```sh
omarchy plugin add https://github.com/sam-blakeman/OmarchyIPTV.git --enable
omarchy pkg add mpv
```

Requires `mpv` for playback. `python` (stdlib only) and `secret-tool` (libsecret) are used for sync and the Xtream password.

## Configure

Open the IPTV panel → **Setup**. Pick Xtream or M3U, fill the fields, **Save & sync**.

The Xtream password is stored in the keyring (`secret-tool`), not in `provider.json`. Leave the password blank on later saves to keep the current one.

Advanced: `~/.config/omarchy-iptv/provider.json` (mode 600) still works if you prefer files.

```json
{"type": "xtream", "host": "http://host:8080", "username": "myuser"}
```

```json
{"type": "m3u", "url": "https://example.com/playlist.m3u", "epg": "https://example.com/xmltv.xml"}
```

Optional `"user_agent"` key if the provider whitelists a player UA.

### Limits

Sync caps what a provider may send, so a broken or hostile server (or a gzip
bomb) cannot exhaust memory. Larger responses abort with
`provider response too large` in Setup.

| input | on the wire | after gzip |
|---|---|---|
| m3u playlist | 32 MiB | 128 MiB |
| Xtream JSON (per call) | 32 MiB | 128 MiB |
| XMLTV guide | 256 MiB | 2 GiB (streamed into sqlite, never held whole) |

Then press 󰑓, or:

```sh
~/.config/omarchy/plugins/io.github.sam-blakeman.iptv/bin/iptv-sync --sync
```

## Use

* Left click bar icon: quick-browse panel (Live / VOD / Guide / Setup)
* Middle click bar icon: stop mpv / the TV
* Right click bar icon: re-sync provider + EPG
* ⛶ in the panel, or `omarchy-shell shell summon io.github.sam-blakeman.iptv`: fullscreen TV mode
* Click / Enter on a row: play in external mpv
* Guide rows play the live channel
* VOD: pick a group or type to search (the full VOD catalogue is not loaded into the shell)
* Group picker is searchable
* Sync errors show in Setup (secrets redacted)
* A stream mpv cannot open reports "Stream failed" in the bar and the mpv error in Setup
* Setup shows cache age and EPG coverage; the provider re-syncs automatically once a day

### Stream to TV

Setup → **Stream to TV** → **Find TVs** lists the DLNA/UPnP renderers on your
network (Samsung Smart TVs, most LG/Sony sets, receivers, Kodi…). Pick one;
from then on 󰍹 in the panel header (or `Ctrl+T` in fullscreen) switches
between playing in mpv and playing on the TV. Middle click on the bar icon
stops the TV too. The choice survives restarts (`~/.config/omarchy-iptv/tv.json`).

How it works: `bin/iptv-cast` talks UPnP AVTransport (`SetAVTransportURI` +
`Play`) straight to the TV, which then fetches and decodes the stream by
itself — nothing is transcoded or proxied through this machine, and the shell
still never sees a stream URL. Live Xtream channels are handed over as MPEG-TS
(`.ts`) rather than HLS because TV DLNA players cope with that far better;
set `"live_format": "m3u8"` in `tv.json` if your provider only serves HLS.

Things to know:

* The first time, the TV asks whether to allow this computer — accept with the
  remote (Samsung: Settings → General → External Device Manager → Device
  Connection Manager lists it afterwards).
* Discovery works even when a host firewall (ufw) drops SSDP replies: Samsung
  sets are also found by probing their DLNA port (9197). Other brands behind
  such a firewall need `bin/iptv-cast --select --location http://<tv>:<port>/<desc>.xml`
  (or opening UDP 1900 for the LAN).
* The TV uses its own User-Agent, so a provider that only accepts a
  whitelisted player UA will not play on the TV.
* `bin/iptv-cast --status` shows the TV's transport state; `--stop`,
  `--pause`, `--resume` control it from a script.

### Favorites

☆/★ per row (or `Ctrl+F` in fullscreen). Stored as `{kind, id, name}` refs in
`~/.config/omarchy-iptv/favorites.json` — no stream URLs. Pinned under
★ Favorites.

### Fullscreen TV mode

Keyboard-first: ↑↓ move · ←→ group · Enter play · Ctrl+F favorite ·
Ctrl+T mpv/TV · Tab Live/VOD · type to filter · Esc back out. Now/next refreshes from the
cache every 5 minutes. Themed with the `[menu]` surface tokens.

## Layout

```
manifest.json   # kinds: [bar-widget, overlay, service], keepLoaded
BarWidget.qml   # bar button + quick panel loader
Panel.qml       # Live/VOD/Guide/Setup
IptvOverlay.qml # fullscreen 10-foot browser
IptvService.qml # shared singleton: data, mpv playback, favorites
IptvModel.js    # filter/favorites helpers
bin/iptv-sync   # python3 stdlib: m3u/Xtream fetch, XMLTV → sqlite, JSON dumps
bin/iptv-play   # mpv wrapper (looks up stream URL by id)
bin/iptv-cast   # python3 stdlib: DLNA discovery + AVTransport control (stream to TV)
```

Cache: `~/.cache/omarchy-iptv/` (`channels.json`, `vod.json`, `epg.db`, `renderers.json`, mode 600).
Stream URLs never leave that cache into QML.

## Remove

```sh
omarchy plugin remove io.github.sam-blakeman.iptv
rm -rf ~/.config/omarchy-iptv ~/.cache/omarchy-iptv
```

## Future (out of scope)

Series (`get_series`/`get_series_info`), full EPG grid, multi-provider.

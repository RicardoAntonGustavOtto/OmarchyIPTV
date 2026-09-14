import QtQuick
import Quickshell
import Quickshell.Io
import "IptvModel.js" as Model

Item {
  id: root

  property var shell: null

  readonly property string pluginId: "io.github.sam-blakeman.iptv"
  readonly property string home: Quickshell.env("HOME")
  readonly property string configDir: home + "/.config/omarchy-iptv"
  readonly property string favPath: configDir + "/favorites.json"

  property var channels: []
  property var vod: []
  property var liveGroups: ["All"]
  property var vodGroups: ["All"]
  property var favRows: []
  property var epgNow: []
  property var epgMap: ({})
  property bool epgLoaded: false
  property string statusLine: "IPTV"
  property string lastError: ""
  property bool syncing: false
  property bool savingProvider: false
  property string vodGroup: "All"
  property string vodQuery: ""
  property int vodLimit: 400

  // Series: catalog rows come from the sync cache like VOD; a show's
  // episodes are fetched on demand (one provider call, cached 6h).
  property var series: []
  property var seriesGroups: ["All"]
  property string seriesGroup: "All"
  property string seriesQuery: ""
  property int seriesLimit: 400
  property var episodes: []
  property string episodesFor: ""
  property bool episodesLoading: false
  property string episodesError: ""

  property var status: ({})
  readonly property real syncedAtMs: status && status.synced_at ? status.synced_at * 1000 : 0
  readonly property bool providerConfigured: !!(status && status.provider)
  property int syncIntervalMs: 24 * 3600 * 1000

  property var favorites: []

  readonly property bool playing: playProc.running || root.casting

  // Stream to TV: a DLNA/UPnP renderer picked in Setup (bin/iptv-cast). With
  // castMode on, rows play on the TV instead of in mpv; the TV fetches the
  // stream itself, so only the catalog id ever leaves the shell.
  property var tv: ({})
  readonly property bool tvConfigured: !!(tv && tv.control_url)
  readonly property string tvName: tv && tv.name ? String(tv.name) : ""
  property bool castMode: false
  property bool casting: false
  property string castTitle: ""
  property var renderers: []
  property bool discovering: false
  property bool castBusy: false
  property string tvError: ""

  property int epgRefreshMs: 5 * 60 * 1000

  function fileFromUrl(u) {
    var s = String(u || "")
    if (s.indexOf("file://") === 0) s = s.substring(7)
    if (s.charAt(0) !== "/") {
      var i = s.indexOf("/")
      if (i >= 0) s = s.substring(i)
    }
    try { return decodeURIComponent(s) } catch (e) { return s }
  }
  function helperPath() {
    return fileFromUrl(Qt.resolvedUrl("bin/iptv-sync").toString())
  }
  function playHelperPath() {
    return fileFromUrl(Qt.resolvedUrl("bin/iptv-play").toString())
  }
  function castHelperPath() {
    return fileFromUrl(Qt.resolvedUrl("bin/iptv-cast").toString())
  }

  Component.onCompleted: loadAll()

  function loadAll() {
    if (!dumpChannelsProc.running) dumpChannelsProc.running = true
    if (!dumpLiveGroupsProc.running) dumpLiveGroupsProc.running = true
    if (!dumpVodGroupsProc.running) dumpVodGroupsProc.running = true
    if (!dumpSeriesGroupsProc.running) dumpSeriesGroupsProc.running = true
    refreshEpg()
    refreshStatus()
    refreshFavRows()
  }

  function refreshStatus() {
    if (!statusProc.running) statusProc.running = true
  }

  function maybeAutoSync() {
    if (root.syncing || !root.providerConfigured) return
    if (Date.now() - root.syncedAtMs > root.syncIntervalMs) root.resync()
  }

  function redact(text, url) {
    var s = String(text || "")
    if (url) s = s.split(url).join("<stream>")
    return s.replace(/https?:\/\/\S+/g, "<url>")
  }

  function refreshEpg() {
    if (!dumpEpgProc.running) dumpEpgProc.running = true
  }

  function resync() {
    if (syncProc.running) return
    root.syncing = true
    root.lastError = ""
    root.statusLine = "Syncing…"
    syncProc.running = true
  }

  function play(id, title) {
    if (!id) return
    if (root.castMode && root.tvConfigured) root.cast(id, title)
    else root.playLocal(id, title)
  }

  function playLocal(id, title) {
    if (!id) return
    var cmd = [root.playHelperPath(), "--id", String(id), "--title", title || "IPTV"]
    root.statusLine = title || "Playing"
    if (playProc.running) {
      playProc.pendingCommand = cmd
      playProc.stopRequested = true
      playProc.running = false
    } else {
      playProc.currentTitle = title || "IPTV"
      playProc.command = cmd
      playProc.running = true
    }
  }

  function stop() {
    playProc.pendingCommand = null
    playProc.stopRequested = true
    playProc.running = false
    if (root.casting || castProc.running) root.castStop()
  }

  function setCastMode(on) {
    root.castMode = !!on && root.tvConfigured
    if (!root.tvConfigured) return
    var cfg = root.tv || ({})
    if (!!cfg.cast_mode === root.castMode) return
    cfg.cast_mode = root.castMode
    root.tv = cfg
    tvFile.setText(JSON.stringify(cfg, null, 1) + "\n")
  }

  function cast(id, title) {
    if (!id || !root.tvConfigured) return
    if (castProc.running) castProc.running = false
    root.castTitle = title || "IPTV"
    root.tvError = ""
    root.castBusy = true
    root.statusLine = "TV ▶ " + root.castTitle + "…"
    castProc.command = [root.castHelperPath(), "--play", "--id", String(id), "--title", root.castTitle]
    castProc.running = true
  }

  function castStop() {
    if (castProc.running) castProc.running = false
    root.casting = false
    root.castBusy = false
    root.updateStatus()
    if (castCtlProc.running) castCtlProc.running = false
    castCtlProc.command = [root.castHelperPath(), "--stop"]
    castCtlProc.running = true
  }

  function discoverTvs() {
    if (discoverProc.running) return
    root.tvError = ""
    root.discovering = true
    discoverProc.running = true
  }

  function selectTv(r) {
    if (!r || !r.location || selectProc.running) return
    root.tvError = ""
    selectProc.command = [root.castHelperPath(), "--select", "--location", String(r.location)]
    selectProc.running = true
  }

  function forgetTv() {
    if (root.casting) root.castStop()
    root.castMode = false
    root.tv = ({})
    if (!forgetProc.running) forgetProc.running = true
  }

  function playChannel(ch) {
    if (ch && ch.id && ch.available !== false) root.play(ch.id, ch.name)
  }

  function lastLine(text, fallback) {
    var lines = String(text || "").trim().split("\n").filter(function(l) { return l.trim() !== "" })
    return lines.length ? lines[lines.length - 1] : fallback
  }

  function requestVod(group, query) {
    root.vodGroup = group || "All"
    root.vodQuery = query || ""
    vodDebounce.restart()
  }

  function dumpVodNow() {
    if (dumpVodProc.running) dumpVodProc.running = false
    dumpVodProc.running = true
  }

  function requestSeries(group, query) {
    root.seriesGroup = group || "All"
    root.seriesQuery = query || ""
    seriesDebounce.restart()
  }

  function dumpSeriesNow() {
    if (dumpSeriesProc.running) dumpSeriesProc.running = false
    dumpSeriesProc.running = true
  }

  function requestEpisodes(seriesId, force) {
    if (!seriesId) return
    var cmd = [root.helperPath(), "--dump-episodes", "--id", String(seriesId)]
    if (force) cmd.push("--refresh")
    root.episodesFor = String(seriesId)
    root.episodes = []
    root.episodesError = ""
    root.episodesLoading = true
    if (dumpEpisodesProc.running) dumpEpisodesProc.running = false
    dumpEpisodesProc.command = cmd
    dumpEpisodesProc.running = true
  }

  function saveProvider(cfg) {
    if (!cfg || saveProvProc.running) return
    var cmd = [root.helperPath(), "--write-provider", "--type", String(cfg.type || "")]
    if (cfg.host) { cmd.push("--host"); cmd.push(String(cfg.host)) }
    if (cfg.username) { cmd.push("--username"); cmd.push(String(cfg.username)) }
    if (cfg.url) { cmd.push("--url"); cmd.push(String(cfg.url)) }
    if (cfg.epg) { cmd.push("--epg"); cmd.push(String(cfg.epg)) }
    if (cfg.user_agent) { cmd.push("--user-agent"); cmd.push(String(cfg.user_agent)) }
    saveProvProc.form = cfg
    saveProvProc.command = cmd
    root.savingProvider = true
    root.lastError = ""
    saveProvProc.running = true
  }

  function epgText(item) {
    if (!item) return ""
    if (!root.epgLoaded) return item.epg_now || ""
    var e = root.epgMap[String(item.id)]
    return e ? e.now : ""
  }
  function epgNextText(item) {
    if (!item) return ""
    if (!root.epgLoaded) return item.epg_next || ""
    var e = root.epgMap[String(item.id)]
    return e ? e.next : ""
  }

  function isFav(kind, id) {
    return Model.isFavInList(root.favorites, kind, id)
  }

  function toggleFav(kind, item) {
    if (!item || !item.id) return
    root.favorites = Model.toggleFavList(root.favorites, kind, item)
    saveFavorites()
    refreshFavRows()
  }

  function saveFavorites() {
    favFile.setText(JSON.stringify(root.favorites) + "\n")
  }

  function favItems(kind) {
    return Model.resolveFavs(root.favorites, kind, root.favRows)
  }

  function refreshFavRows() {
    var ids = []
    var favs = root.favorites || []
    for (var i = 0; i < favs.length; i++)
      if (favs[i] && favs[i].id) ids.push(String(favs[i].id))
    if (!ids.length) {
      root.favRows = []
      return
    }
    dumpFavProc.command = [root.helperPath(), "--dump-ids", ids.join(",")]
    if (!dumpFavProc.running) dumpFavProc.running = true
  }

  function updateStatus() {
    if (root.playing) return
    var n = (root.channels || []).length
    root.statusLine = n > 0 ? n + " channels" : "No channels — see Setup"
  }

  Process {
    id: playProc
    running: false
    command: []
    property var pendingCommand: null
    property bool stopRequested: false
    property string currentTitle: ""
    stderr: StdioCollector { id: playErr; waitForEnd: true }
    onExited: function(code, status) {
      var failed = !stopRequested && status === 0 && code !== 0 && code !== 4
      if (failed) {
        var lines = String(playErr.text || "").trim().split("\n").filter(function(l) { return l.trim() !== "" })
        var why = lines.length ? lines[lines.length - 1] : ("mpv exit " + code)
        root.lastError = "Playback failed (" + currentTitle + "): " + root.redact(why, "")
        root.statusLine = "Stream failed: " + currentTitle
      }
      stopRequested = false
      if (pendingCommand) {
        command = pendingCommand
        currentTitle = pendingCommand[4] || "IPTV"
        pendingCommand = null
        running = true
      } else if (!failed) {
        root.updateStatus()
      }
    }
  }

  Process {
    id: castProc
    running: false
    command: []
    stderr: StdioCollector { id: castErr; waitForEnd: true }
    onExited: function(code, status) {
      root.castBusy = false
      if (status === 0 && code === 0) {
        root.casting = true
        root.statusLine = "TV ▶ " + root.castTitle
      } else if (status === 0) {
        root.casting = false
        var why = root.redact(root.lastLine(castErr.text, "exit " + code), "")
        root.tvError = "TV playback failed (" + root.castTitle + "): " + why
        root.lastError = root.tvError
        root.statusLine = "TV failed: " + root.castTitle
      }
    }
  }

  Process {
    id: castCtlProc
    running: false
    command: []
    stderr: StdioCollector { waitForEnd: true }
  }

  Process {
    id: discoverProc
    running: false
    command: [root.castHelperPath(), "--discover"]
    stdout: StdioCollector { id: discoverOut; waitForEnd: true }
    stderr: StdioCollector { id: discoverErr; waitForEnd: true }
    onExited: function(code) {
      root.discovering = false
      var rows = []
      try { rows = JSON.parse(discoverOut.text || "[]") } catch (e) { rows = [] }
      root.renderers = Array.isArray(rows) ? rows : []
      if (code !== 0) root.tvError = "TV search failed: " + root.redact(root.lastLine(discoverErr.text, "exit " + code), "")
      else if (!root.renderers.length) root.tvError = "No TV found. Make sure it is on and on the same network."
    }
  }

  Process {
    id: selectProc
    running: false
    command: []
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { id: selectErr; waitForEnd: true }
    onExited: function(code) {
      if (code === 0) {
        tvFile.reload()
      } else {
        root.tvError = "Could not use that TV: " + root.redact(root.lastLine(selectErr.text, "exit " + code), "")
      }
    }
  }

  Process {
    id: forgetProc
    running: false
    command: [root.castHelperPath(), "--forget"]
  }

  Process {
    id: dumpChannelsProc
    running: false
    command: [root.helperPath(), "--dump-channels"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          root.channels = JSON.parse(text || "[]")
        } catch (e) { root.channels = [] }
        root.updateStatus()
      }
    }
  }

  Process {
    id: dumpLiveGroupsProc
    running: false
    command: [root.helperPath(), "--dump-groups", "live"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var g = JSON.parse(text || "[]")
          root.liveGroups = Array.isArray(g) && g.length ? g : ["All"]
        } catch (e) { root.liveGroups = ["All"] }
      }
    }
  }

  Process {
    id: dumpVodGroupsProc
    running: false
    command: [root.helperPath(), "--dump-groups", "vod"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var g = JSON.parse(text || "[]")
          root.vodGroups = Array.isArray(g) && g.length ? g : ["All"]
        } catch (e) { root.vodGroups = ["All"] }
      }
    }
  }

  Process {
    id: dumpVodProc
    running: false
    command: {
      var cmd = [root.helperPath(), "--dump-vod", "--limit", String(root.vodLimit)]
      if (root.vodGroup && root.vodGroup !== "All") {
        cmd.push("--group")
        cmd.push(root.vodGroup)
      }
      if (root.vodQuery) {
        cmd.push("--query")
        cmd.push(root.vodQuery)
      }
      return cmd
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.vod = JSON.parse(text || "[]") } catch (e) { root.vod = [] }
      }
    }
  }

  Timer {
    id: vodDebounce
    interval: 180
    repeat: false
    onTriggered: root.dumpVodNow()
  }

  Process {
    id: dumpSeriesGroupsProc
    running: false
    command: [root.helperPath(), "--dump-groups", "series"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var g = JSON.parse(text || "[]")
          root.seriesGroups = Array.isArray(g) && g.length ? g : ["All"]
        } catch (e) { root.seriesGroups = ["All"] }
      }
    }
  }

  Process {
    id: dumpSeriesProc
    running: false
    command: {
      var cmd = [root.helperPath(), "--dump-series", "--limit", String(root.seriesLimit)]
      if (root.seriesGroup && root.seriesGroup !== "All") {
        cmd.push("--group")
        cmd.push(root.seriesGroup)
      }
      if (root.seriesQuery) {
        cmd.push("--query")
        cmd.push(root.seriesQuery)
      }
      return cmd
    }
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.series = JSON.parse(text || "[]") } catch (e) { root.series = [] }
      }
    }
  }

  Timer {
    id: seriesDebounce
    interval: 180
    repeat: false
    onTriggered: root.dumpSeriesNow()
  }

  Process {
    id: dumpEpisodesProc
    running: false
    command: [root.helperPath(), "--dump-episodes", "--id", ""]
    stdout: StdioCollector { id: episodesOut; waitForEnd: true }
    stderr: StdioCollector { id: episodesErr; waitForEnd: true }
    onExited: function(code) {
      root.episodesLoading = false
      if (code === 0) {
        try {
          var rows = JSON.parse(episodesOut.text || "[]")
          root.episodes = Array.isArray(rows) ? rows : []
        } catch (e) { root.episodes = [] }
        if (!root.episodes.length) root.episodesError = "No episodes listed for this show."
      } else {
        var lines = String(episodesErr.text || "").trim().split("\n")
        root.episodesError = "Episodes failed: " + root.redact(lines.length ? lines[lines.length - 1] : ("exit " + code), "")
      }
    }
  }

  Process {
    id: dumpFavProc
    running: false
    command: [root.helperPath(), "--dump-ids", ""]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var rows = JSON.parse(text || "[]")
          root.favRows = Array.isArray(rows) ? rows : []
        } catch (e) { root.favRows = [] }
      }
    }
  }

  Process {
    id: dumpEpgProc
    running: false
    command: [root.helperPath(), "--dump-epg-now"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        var list = []
        try { list = JSON.parse(text || "[]") } catch (e) { list = [] }
        var map = ({})
        for (var i = 0; i < list.length; i++)
          map[String(list[i].channel_id)] = { now: list[i].now || "", next: list[i].next || "" }
        root.epgNow = list
        root.epgMap = map
        root.epgLoaded = true
      }
    }
  }

  Process {
    id: statusProc
    running: false
    command: [root.helperPath(), "--dump-status"]
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try { root.status = JSON.parse(text || "{}") } catch (e) { root.status = ({}) }
        root.maybeAutoSync()
      }
    }
  }

  Process {
    id: saveProvProc
    running: false
    stdinEnabled: true
    property var form: ({})
    command: [root.helperPath(), "--write-provider", "--type", "xtream"]
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { id: saveErr; waitForEnd: true }
    onStarted: {
      write(String((form && form.password) || "") + "\n")
      form = ({})
    }
    onExited: function(code) {
      root.savingProvider = false
      if (code === 0) {
        root.lastError = ""
        root.resync()
      } else {
        var lines = String(saveErr.text || "").trim().split("\n")
        root.lastError = "Save failed: " + (lines.length ? lines[lines.length - 1] : ("exit " + code))
      }
    }
  }

  Process {
    id: syncProc
    running: false
    command: [root.helperPath(), "--sync"]
    stdout: StdioCollector { waitForEnd: true }
    stderr: StdioCollector { id: syncErr; waitForEnd: true }
    onExited: function(code) {
      root.syncing = false
      if (code === 0) {
        root.statusLine = "Synced"
      } else {
        var lines = String(syncErr.text || "").trim().split("\n")
        root.lastError = "Sync failed: " + (lines.length ? lines[lines.length - 1] : ("exit " + code))
        root.statusLine = "Sync failed — see Setup"
      }
      root.loadAll()
    }
  }

  Timer {
    interval: root.epgRefreshMs
    running: true
    repeat: true
    onTriggered: root.refreshEpg()
  }

  Timer {
    interval: 3600 * 1000
    running: true
    repeat: true
    onTriggered: root.refreshStatus()
  }

  FileView {
    id: tvFile
    path: root.configDir + "/tv.json"
    watchChanges: false
    atomicWrites: true
    printErrors: false
    onLoaded: {
      try {
        var v = JSON.parse(text() || "{}")
        root.tv = (v && typeof v === "object") ? v : ({})
      } catch (e) { root.tv = ({}) }
      root.castMode = root.tvConfigured && root.tv.cast_mode === true
    }
    onLoadFailed: { root.tv = ({}); root.castMode = false }
  }

  FileView {
    id: favFile
    path: root.favPath
    watchChanges: false
    atomicWrites: true
    printErrors: false
    onLoaded: {
      try {
        var v = JSON.parse(text() || "[]")
        root.favorites = Array.isArray(v) ? v : []
      } catch (e) { root.favorites = [] }
      root.refreshFavRows()
    }
  }
}

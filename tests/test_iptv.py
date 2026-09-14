#!/usr/bin/env python3
import gzip
import http.server
import importlib.machinery
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sync = importlib.machinery.SourceFileLoader(
    "iptv_sync", str(ROOT / "bin" / "iptv-sync")
).load_module()


class ParseM3u(unittest.TestCase):
    def test_quoted_group_comma(self):
        text = '#EXTM3U\n#EXTINF:-1 tvg-id="bbc" group-title="UK, News",BBC One\nhttp://example/1\n'
        ch = sync.parse_m3u(text)
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0]["name"], "BBC One")
        self.assertEqual(ch[0]["group"], "UK, News")
        self.assertTrue(ch[0]["id"].startswith("m3u-"))

    def test_stable_id(self):
        a = sync.stable_id("u", "n", "g")
        b = sync.stable_id("u", "n", "g")
        c = sync.stable_id("u", "n", "other")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)


class SlimAndMatch(unittest.TestCase):
    def test_slim_drops_url_and_logo(self):
        row = {"id": "xc-1", "name": "A", "logo": "http://l", "url": "http://s/secret", "group": "UK"}
        out = sync.slim(row)
        self.assertNotIn("url", out)
        self.assertNotIn("logo", out)
        self.assertEqual(out["name"], "A")

    def test_row_matches(self):
        row = {"name": "BBC One", "group": "UK"}
        self.assertTrue(sync.row_matches(row, "bbc", "UK"))
        self.assertFalse(sync.row_matches(row, "bbc", "US"))
        self.assertFalse(sync.row_matches(row, "itv", "UK"))

    def test_query_matches_group_when_not_narrowed(self):
        row = {"name": "BBC One", "group": "News"}
        # a category name is a shortcut while the picker is still on All
        self.assertTrue(sync.row_matches(row, "news", "All"))
        self.assertTrue(sync.row_matches(row, "news", ""))
        # ...but not once that group is picked, or every row in it would match
        self.assertFalse(sync.row_matches({"name": "ITV", "group": "News"}, "news", "News"))
        # an unrelated query still misses
        self.assertFalse(sync.row_matches(row, "sports", "All"))

    def test_group_names(self):
        rows = [{"group": "UK"}, {"group": "UK"}, {"group": "US"}]
        self.assertEqual(sync.group_names(rows), ["All", "UK", "US"])


class AccountSummary(unittest.TestCase):
    def test_expired_trial(self):
        a = sync.account_summary({"status": "Expired", "exp_date": "1789401736", "is_trial": "1", "max_connections": "1"})
        self.assertEqual(a["status"], "Expired")
        self.assertEqual(a["exp_date"], 1789401736)
        self.assertTrue(a["is_trial"])
        self.assertEqual(a["max_connections"], 1)

    def test_garbage_is_harmless(self):
        a = sync.account_summary(None)
        self.assertEqual((a["status"], a["exp_date"], a["is_trial"], a["max_connections"]), ("", 0, False, 0))
        self.assertEqual(sync.account_summary({"exp_date": "soon", "max_connections": None})["exp_date"], 0)


class Redact(unittest.TestCase):
    def test_redact_secret_and_urlencoded(self):
        sync.SECRETS[:] = ["p@ss"]
        self.assertEqual(sync.redact("pw=p@ss extra"), "pw=*** extra")
        sync.SECRETS[:] = []


class DumpVodGuard(unittest.TestCase):
    def test_all_without_query_is_empty(self):
        buf = tempfile.NamedTemporaryFile("w+", delete=False)
        try:
            old = sync.VOD_JSON
            sync.VOD_JSON = buf.name
            json.dump([{"id": "xcv-1", "name": "Film", "group": "Movies", "url": "http://x"}], buf)
            buf.close()
            import io
            from contextlib import redirect_stdout
            out = io.StringIO()
            with redirect_stdout(out):
                sync.dump_vod("", "All", 400)
            self.assertEqual(json.loads(out.getvalue()), [])
            sync.VOD_JSON = old
        finally:
            os.unlink(buf.name)


class Series(unittest.TestCase):
    INFO = {
        "info": {"name": "Show"},
        "episodes": {
            "2": [{"id": 20, "episode_num": "1", "season": 2, "title": "Two-One",
                   "container_extension": "mkv", "info": {"duration": "00:41:00"}}],
            "1": [{"id": 11, "episode_num": 2, "title": "One-Two"},
                  {"id": 10, "episode_num": 1, "title": "One-One"}],
        },
    }

    def test_series_rows_map_catalog(self):
        rows = sync.series_rows(
            [{"series_id": 7, "name": "Show", "category_id": 3, "cover": "http://c",
              "plot": "p" * 500, "releaseDate": "2019-05-01", "rating": 8}],
            {"3": "Drama"})
        self.assertEqual(rows[0]["id"], "xcs-7")
        self.assertEqual(rows[0]["group"], "Drama")
        self.assertEqual(rows[0]["year"], "2019")
        self.assertEqual(len(rows[0]["plot"]), 240)
        self.assertNotIn("logo", sync.slim(rows[0]))

    def test_episode_rows_flatten_and_order(self):
        rows = sync.episode_rows(self.INFO, 7, "http://h", "u", "p")
        self.assertEqual([r["id"] for r in rows], ["xce-7-10", "xce-7-11", "xce-7-20"])
        self.assertEqual(rows[0]["name"], "S01E01 · One-One")
        self.assertEqual(rows[2]["group"], "Season 2")
        self.assertEqual(rows[2]["url"], "http://h/series/u/p/20.mkv")
        self.assertEqual(rows[0]["url"], "http://h/series/u/p/10.mp4")
        self.assertEqual(rows[2]["duration"], "00:41:00")
        self.assertEqual(rows[0]["series_name"], "Show")

    def test_episode_title_with_code_is_not_prefixed_twice(self):
        info = {"episodes": {"1": [{"id": 5, "episode_num": 3, "title": "Show - S01E03 - Pilot"}]}}
        rows = sync.episode_rows(info, 9, "http://h", "u", "p")
        self.assertEqual(rows[0]["name"], "Show - S01E03 - Pilot")

    def test_episode_rows_accept_list_form(self):
        info = {"episodes": [[{"id": 1, "episode_num": 1, "title": "a"}],
                             [{"id": 2, "episode_num": 1, "title": "b"}]]}
        rows = sync.episode_rows(info, 3, "http://h", "u", "p")
        self.assertEqual([(r["season"], r["id"]) for r in rows], [(1, "xce-3-1"), (2, "xce-3-2")])

    def test_dump_series_lists_all_and_matches_plot(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            old = sync.SERIES_JSON
            sync.SERIES_JSON = os.path.join(d, "series.json")
            try:
                sync.save_json(sync.SERIES_JSON, [
                    {"id": "xcs-1", "name": "Pirates", "group": "Anime", "plot": "straw hat crew", "logo": "x"},
                    {"id": "xcs-2", "name": "Office", "group": "Comedy", "plot": "paper company", "logo": "x"}])
                out = io.StringIO()
                with redirect_stdout(out):
                    sync.dump_series("", "All", 400)  # a small catalog lists unfiltered
                self.assertEqual([r["id"] for r in json.loads(out.getvalue())], ["xcs-1", "xcs-2"])
                old_max = sync.SERIES_LIST_ALL_MAX
                sync.SERIES_LIST_ALL_MAX = 1  # ...a huge one needs a group or query, like VOD
                try:
                    out = io.StringIO()
                    with redirect_stdout(out):
                        sync.dump_series("", "All", 400)
                    self.assertEqual(json.loads(out.getvalue()), [])
                    out = io.StringIO()
                    with redirect_stdout(out):
                        sync.dump_series("", "Comedy", 400)
                    self.assertEqual([r["id"] for r in json.loads(out.getvalue())], ["xcs-2"])
                finally:
                    sync.SERIES_LIST_ALL_MAX = old_max
                out = io.StringIO()
                with redirect_stdout(out):
                    sync.dump_series("straw hat", "All", 400)
                rows = json.loads(out.getvalue())
                self.assertEqual([r["id"] for r in rows], ["xcs-1"])
                self.assertNotIn("logo", rows[0])
                out = io.StringIO()
                with redirect_stdout(out):
                    sync.dump_groups("series")
                self.assertEqual(json.loads(out.getvalue()), ["All", "Anime", "Comedy"])
            finally:
                sync.SERIES_JSON = old

    def test_play_url_resolves_episode_from_cache(self):
        import io
        from contextlib import redirect_stdout
        with tempfile.TemporaryDirectory() as d:
            old = sync.SERIES_DIR
            sync.SERIES_DIR = os.path.join(d, "series")
            try:
                os.makedirs(sync.SERIES_DIR)
                rows = sync.episode_rows(self.INFO, 7, "http://h", "u", "p")
                sync.save_json(sync.episodes_path(7), rows)  # fresh cache: no network needed
                out = io.StringIO()
                with redirect_stdout(out):
                    sync.play_url("xce-7-11")
                self.assertEqual(out.getvalue(), "http://h/series/u/p/11.mp4")
            finally:
                sync.SERIES_DIR = old


FIELD_TYPES = {"id", "text", "money", "percent", "int", "float", "bool", "date", "datetime", "enum"}


class Describe(unittest.TestCase):
    """--describe is a static schema (DESCRIBE.md protocol v1). Its shape is
    checked here, and its field names are checked against what the real
    --dump-* commands emit after a sync from a fake Xtream panel, so the
    descriptor cannot drift from the data."""

    HOST = "http://127.0.0.1"  # the fake panel; must never leak into the descriptor
    NOW = int(time.time())

    @classmethod
    def setUpClass(cls):
        def ts(t):
            return time.strftime("%Y%m%d%H%M%S", time.gmtime(t)) + " +0000"

        xmltv = ("<tv><channel id=\"c1\"><display-name>One</display-name></channel>"
                 f"<programme start=\"{ts(cls.NOW - 600)}\" stop=\"{ts(cls.NOW + 600)}\" channel=\"c1\">"
                 "<title>Now</title></programme>"
                 f"<programme start=\"{ts(cls.NOW + 600)}\" stop=\"{ts(cls.NOW + 1200)}\" channel=\"c1\">"
                 "<title>Next</title></programme></tv>").encode()
        api = {
            "": {"user_info": {"status": "Active", "exp_date": "1789401736", "max_connections": "1"}},
            "get_live_categories": [{"category_id": 1, "category_name": "News"}],
            "get_vod_categories": [{"category_id": 2, "category_name": "Movies"}],
            "get_series_categories": [{"category_id": 3, "category_name": "Drama"}],
            "get_live_streams": [{"stream_id": 1, "name": "One", "category_id": 1,
                                  "epg_channel_id": "c1", "stream_icon": "http://x/l.png"}],
            "get_vod_streams": [{"stream_id": 2, "name": "Film", "category_id": 2,
                                 "container_extension": "mkv"}],
            "get_series": [{"series_id": 7, "name": "Show", "category_id": 3, "cover": "http://x/c",
                            "plot": "p", "releaseDate": "2019-05-01", "rating": 8}],
            "get_series_info": Series.INFO,
        }

        class Panel(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                u = urllib.parse.urlparse(self.path)
                q = urllib.parse.parse_qs(u.query)
                if q.get("password") != ["pw"]:
                    self.send_response(401)
                    self.end_headers()
                    return
                if u.path == "/xmltv.php":
                    body, ctype = xmltv, "application/xml"
                else:
                    body = json.dumps(api[q.get("action", [""])[0]]).encode()
                    ctype = "application/json"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a):
                pass

        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), Panel)
        cls.base = f"{cls.HOST}:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def run_describe(self, home):
        p = subprocess.run([str(ROOT / "bin" / "iptv-sync"), "--describe"],
                           capture_output=True, text=True, timeout=30,
                           env={**os.environ, "HOME": home})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, "")
        return p.stdout

    def test_prints_one_json_object_without_setup_or_side_effects(self):
        with tempfile.TemporaryDirectory() as home:
            out = self.run_describe(home)
            self.assertEqual(os.listdir(home), [], "describe must not create config or cache")
        d = json.loads(out)  # exactly one object on stdout, nothing else
        self.assertEqual(d, sync.DESCRIBE)
        self.assertEqual(d["tool"], "iptv")
        self.assertEqual(d["version"], 1)
        self.assertTrue(d["summary"])
        for word in ("http", "://", "127.0.0.1", "password", "@"):
            self.assertNotIn(word, out)

        entities = d["entities"]
        self.assertEqual(set(entities), {"channel", "vod", "series", "episode"})
        for name, e in entities.items():
            self.assertEqual(set(e), {"summary", "id", "label", "fields", "columns", "summable"}, name)
            self.assertTrue(e["summary"])
            self.assertIn(e["id"], e["fields"])
            self.assertIn(e["label"], e["fields"])
            self.assertEqual(e["fields"][e["id"]]["type"], "id")
            self.assertTrue(set(e["columns"]) <= set(e["fields"]), name)
            self.assertTrue(set(e["summable"]) <= set(e["fields"]), name)
            for fname, f in e["fields"].items():
                self.assertIn(f["type"], FIELD_TYPES, f"{name}.{fname}")
                self.assertTrue(set(f) <= {"type", "unit", "ref", "null_ok", "values"}, f"{name}.{fname}")
                if "ref" in f:
                    self.assertIn(f["ref"], entities)

        cmds = {c["cmd"]: c for c in d["commands"]}
        self.assertEqual(len(cmds), len(d["commands"]), "duplicate cmd")
        for cmd, c in cmds.items():
            self.assertIn(c["kind"], ("read", "write"), cmd)
            if c["kind"] == "read":
                self.assertNotIn("writes", c, cmd)
                self.assertNotIn("tier", c, cmd)
                if "returns" in c:
                    self.assertIn(c["returns"], entities, cmd)
            else:
                self.assertNotIn("returns", c, cmd)
                self.assertIn(c["tier"], range(5), cmd)
        self.assertEqual({c: cmds[c].get("returns") for c in cmds if cmds[c]["kind"] == "read"}, {
            "iptv-sync --dump-channels": "channel",
            "iptv-sync --dump-vod": "vod",
            "iptv-sync --dump-series": "series",
            "iptv-sync --dump-episodes": "episode",
            "iptv-sync --dump-epg-now": None,
            "iptv-cast --status": None,
        })
        self.assertEqual({c: cmds[c]["tier"] for c in cmds if cmds[c]["kind"] == "write"}, {
            "iptv-cast --play": 2, "iptv-cast --stop": 2, "iptv-cast --pause": 2,
            "iptv-cast --resume": 2, "iptv-play --id": 2, "iptv-sync --sync": 1,
        })

    def test_entity_fields_match_real_dump_keys(self):
        from contextlib import redirect_stdout

        def dump(fn, *args):
            out = io.StringIO()
            with redirect_stdout(out):
                fn(*args)
            rows = json.loads(out.getvalue())
            self.assertTrue(rows, fn.__name__)
            return rows

        names = ("CACHE", "CHANNELS_JSON", "VOD_JSON", "SERIES_JSON", "SERIES_DIR",
                 "ACCOUNT_JSON", "EPG_DB", "CONFIG")
        saved = {n: getattr(sync, n) for n in names}
        old_env = os.environ.get("IPTV_PASSWORD")
        with tempfile.TemporaryDirectory() as d:
            sync.CACHE = d
            sync.CHANNELS_JSON = os.path.join(d, "channels.json")
            sync.VOD_JSON = os.path.join(d, "vod.json")
            sync.SERIES_JSON = os.path.join(d, "series.json")
            sync.SERIES_DIR = os.path.join(d, "series")
            sync.ACCOUNT_JSON = os.path.join(d, "account.json")
            sync.EPG_DB = os.path.join(d, "epg.db")
            sync.CONFIG = os.path.join(d, "provider.json")
            os.environ["IPTV_PASSWORD"] = "pw"
            prov = {"type": "xtream", "host": self.base, "username": "u"}
            with open(sync.CONFIG, "w") as f:
                json.dump(prov, f)
            try:
                self.assertEqual(sync.sync_xtream(prov), (1, 1, 1, 2))
                emitted = {
                    "channel": dump(sync.dump_channels, "", "All", 0),
                    "vod": dump(sync.dump_vod, "", "Movies", 0),
                    "series": dump(sync.dump_series, "", "All", 0),
                    "episode": dump(sync.dump_episodes, "xcs-7"),  # get_series_info via the fake panel
                }
            finally:
                for n, v in saved.items():
                    setattr(sync, n, v)
                sync.SECRETS[:] = []
                if old_env is None:
                    os.environ.pop("IPTV_PASSWORD", None)
                else:
                    os.environ["IPTV_PASSWORD"] = old_env

        self.assertEqual(emitted["channel"][0]["epg_now"], "Now")  # the guide really was joined
        for name, rows in emitted.items():
            fields = set(sync.DESCRIBE["entities"][name]["fields"])
            for row in rows:
                self.assertEqual(set(row), fields, f"{name} row keys drifted from --describe")
                self.assertNotIn("url", row)


class BoundedReaderLimits(unittest.TestCase):
    MIB = 1 << 20

    def reader(self, payload, max_c=None, max_e=None):
        return sync.BoundedReader(io.BytesIO(payload), max_c or self.MIB, max_e or self.MIB, "test")

    def test_identity_passthrough(self):
        data = b"#EXTM3U\n#EXTINF:-1,One\nhttp://x/1\n" * 2000
        self.assertEqual(self.reader(data).read(), data)

    def test_identity_oversized_aborts(self):
        cap = 64 * 1024
        r = self.reader(b"x" * (cap + 1), max_c=cap, max_e=10 * cap)
        with self.assertRaises(sync.ResponseTooLarge):
            r.read()

    def test_gzip_roundtrip_and_partial_reads(self):
        data = b'{"stream_id": 1, "name": "abc"},' * 20000
        r = self.reader(gzip.compress(data))
        self.assertEqual(r.read(5), data[:5])
        self.assertEqual(r.read(), data[5:])

    def test_gzip_bomb_aborts_on_expanded_limit(self):
        bomb = gzip.compress(b"\0" * (32 * self.MIB))  # ~32 KiB on the wire
        self.assertLess(len(bomb), self.MIB)
        r = self.reader(bomb)
        with self.assertRaises(sync.ResponseTooLarge):
            r.read()
        # aborted incrementally: never inflated far past the cap
        self.assertLessEqual(r.n_out, self.MIB + sync.CHUNK)

    def test_truncated_gzip_is_an_error(self):
        gz = gzip.compress(b"abc" * 10000)
        with self.assertRaises(ValueError):
            self.reader(gz[:-20]).read()


class FetchOverHttp(unittest.TestCase):
    """fetch() end to end against a local server: the caps apply on the
    real urllib response object, not only on BytesIO."""

    @classmethod
    def setUpClass(cls):
        cls.bomb = gzip.compress(b"\0" * (8 << 20))
        cls.plain = b"#EXTM3U\n#EXTINF:-1,One\nhttp://x/1\n"
        bomb, plain = cls.bomb, cls.plain

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                body = bomb if self.path == "/bomb" else plain
                self.send_response(200)
                if body is bomb:
                    self.send_header("Content-Encoding", "gzip")
                if self.path == "/huge":
                    self.send_header("Content-Length", str(10 << 30))
                else:
                    self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                try:
                    self.wfile.write(body)
                except OSError:
                    pass  # client hung up early, as it should for /huge

            def log_message(self, *a):
                pass

        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.old = sync.LIMITS["json"]
        sync.LIMITS["json"] = (1 << 20, 1 << 20)

    @classmethod
    def tearDownClass(cls):
        sync.LIMITS["json"] = cls.old
        cls.srv.shutdown()
        cls.srv.server_close()

    def test_small_body_ok(self):
        self.assertEqual(sync.fetch(self.base + "/ok", kind="json"), self.plain)

    def test_gzip_bomb_aborts(self):
        with self.assertRaises(sync.ResponseTooLarge):
            sync.fetch(self.base + "/bomb", kind="json")

    def test_declared_length_over_cap_aborts_before_reading(self):
        with self.assertRaises(sync.ResponseTooLarge):
            sync.fetch(self.base + "/huge", kind="json")


class ImportXmltv(unittest.TestCase):
    def test_streams_gzip_guide_into_db(self):
        now = int(time.time())

        def ts(t):
            return time.strftime("%Y%m%d%H%M%S", time.gmtime(t)) + " +0000"

        xml = ("<tv><channel id=\"c1\"><display-name>One</display-name></channel>"
               f"<programme start=\"{ts(now - 600)}\" stop=\"{ts(now + 600)}\" channel=\"c1\">"
               "<title>Now</title><desc>d</desc></programme>"
               f"<programme start=\"{ts(now + 3 * 86400)}\" stop=\"{ts(now + 3 * 86400 + 600)}\" "
               "channel=\"c1\"><title>Far</title></programme></tv>").encode()
        with tempfile.TemporaryDirectory() as d:
            old = sync.CACHE, sync.EPG_DB
            sync.CACHE, sync.EPG_DB = d, os.path.join(d, "epg.db")
            try:
                reader = sync.BoundedReader(io.BytesIO(gzip.compress(xml)), 1 << 20, 1 << 20)
                self.assertEqual(sync.import_xmltv(reader), 1)
                _now, by_ch = sync.epg_window()
                self.assertEqual([p["title"] for p in by_ch["c1"]], ["Now"])
                # bytes still accepted (and a bad guide keeps the previous EPG)
                with self.assertRaises(sync.ET.ParseError):
                    sync.import_xmltv(b"<tv><programme")
                _now, by_ch = sync.epg_window()
                self.assertEqual([p["title"] for p in by_ch["c1"]], ["Now"])
            finally:
                sync.CACHE, sync.EPG_DB = old


if __name__ == "__main__":
    unittest.main()

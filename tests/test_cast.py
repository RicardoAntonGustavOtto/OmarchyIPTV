#!/usr/bin/env python3
"""bin/iptv-cast: DLNA description parsing, URL/metadata shaping, and the
SOAP sequence against a fake renderer."""
import http.server
import importlib.machinery
import json
import os
import re
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
cast = importlib.machinery.SourceFileLoader(
    "iptv_cast", str(ROOT / "bin" / "iptv-cast")
).load_module()

SAMSUNG_DESC = """<?xml version="1.0"?>
<root xmlns="urn:schemas-upnp-org:device-1-0" xmlns:sec="http://www.sec.co.kr/dlna" xmlns:dlna="urn:schemas-dlna-org:device-1-0">
<specVersion><major>1</major><minor>0</minor></specVersion>{urlbase}
<device>
<deviceType>urn:schemas-upnp-org:device:MediaRenderer:1</deviceType>
<friendlyName>[TV] Samsung 7 Series (50)</friendlyName>
<manufacturer>Samsung Electronics</manufacturer>
<modelName>UE50TU7020WXXN</modelName>
<UDN>uuid:fe7af3e3-0491-4263-aabf-0868e560d92a</UDN>
<serviceList>
<service><serviceType>urn:schemas-upnp-org:service:RenderingControl:1</serviceType><serviceId>urn:upnp-org:serviceId:RenderingControl</serviceId><controlURL>/upnp/control/RenderingControl1</controlURL><eventSubURL>/upnp/event/RenderingControl1</eventSubURL><SCPDURL>/RenderingControl_1.xml</SCPDURL></service>
<service><serviceType>urn:schemas-upnp-org:service:AVTransport:1</serviceType><serviceId>urn:upnp-org:serviceId:AVTransport</serviceId><controlURL>{control}</controlURL><eventSubURL>/upnp/event/AVTransport1</eventSubURL><SCPDURL>/AVTransport_1.xml</SCPDURL></service>
</serviceList>
</device></root>"""

SERVER_DESC = """<?xml version="1.0"?><root xmlns="urn:schemas-upnp-org:device-1-0"><device>
<deviceType>urn:schemas-upnp-org:device:MediaServer:1</deviceType><friendlyName>NAS</friendlyName>
<serviceList><service><serviceType>urn:schemas-upnp-org:service:ContentDirectory:1</serviceType><controlURL>/cd</controlURL></service></serviceList>
</device></root>"""


class Description(unittest.TestCase):
    def test_relative_control_url_joins_location(self):
        d = cast.parse_description(SAMSUNG_DESC.format(urlbase="", control="/upnp/control/AVTransport1").encode(),
                                   "http://192.168.2.3:9197/dmr")
        self.assertEqual(d["name"], "[TV] Samsung 7 Series (50)")
        self.assertEqual(d["model"], "UE50TU7020WXXN")
        self.assertEqual(d["host"], "192.168.2.3")
        self.assertEqual(d["control_url"], "http://192.168.2.3:9197/upnp/control/AVTransport1")
        self.assertTrue(d["udn"].startswith("uuid:"))

    def test_urlbase_wins(self):
        d = cast.parse_description(SAMSUNG_DESC.format(urlbase="<URLBase>http://10.0.0.9:8080/x/</URLBase>",
                                                       control="ctl").encode(), "http://192.168.2.3:9197/dmr")
        self.assertEqual(d["control_url"], "http://10.0.0.9:8080/x/ctl")

    def test_media_server_is_not_a_renderer(self):
        self.assertIsNone(cast.parse_description(SERVER_DESC.encode(), "http://nas/desc.xml"))

    def test_garbage_is_none(self):
        self.assertIsNone(cast.parse_description(b"<html>nope", "http://x/"))


class StreamShaping(unittest.TestCase):
    def test_live_hls_becomes_mpeg_ts_for_the_tv(self):
        self.assertEqual(cast.tv_stream_url("http://h:8080/live/u/p/123.m3u8"), "http://h:8080/live/u/p/123.ts")
        self.assertEqual(cast.tv_stream_url("http://h:8080/live/u/p/123.m3u8?token=1"), "http://h:8080/live/u/p/123.ts?token=1")

    def test_vod_and_episodes_untouched(self):
        for u in ("http://h/movie/u/p/5.mp4", "http://h/series/u/p/9.mkv", "https://cdn/x/playlist.m3u8"):
            self.assertEqual(cast.tv_stream_url(u), u)

    def test_live_format_m3u8_keeps_hls(self):
        u = "http://h:8080/live/u/p/123.m3u8"
        self.assertEqual(cast.tv_stream_url(u, "m3u8"), u)

    def test_mime(self):
        self.assertEqual(cast.mime_for("http://h/live/u/p/1.ts"), "video/mpeg")
        self.assertEqual(cast.mime_for("http://h/movie/u/p/1.mkv?x=1"), "video/x-matroska")
        self.assertEqual(cast.mime_for("http://h/x.m3u8"), "application/vnd.apple.mpegurl")
        self.assertEqual(cast.mime_for("http://h/stream"), "video/mpeg")

    def test_didl_escapes_title_and_url(self):
        m = cast.didl_metadata("http://h/movie/u/p/1.mp4?a=1&b=2", "Tom & Jerry <S1>")
        self.assertIn("<dc:title>Tom &amp; Jerry &lt;S1&gt;</dc:title>", m)
        self.assertIn('protocolInfo="http-get:*:video/mp4:*">http://h/movie/u/p/1.mp4?a=1&amp;b=2</res>', m)
        self.assertIn("object.item.videoItem", m)

    def test_redact_hides_stream_urls(self):
        self.assertEqual(cast.redact("TV refused http://h/live/user/secret/1.ts (714)"), "TV refused <url> (714)")


class SoapFault(unittest.TestCase):
    def test_upnp_error_code_and_description(self):
        body = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
                '<faultcode>s:Client</faultcode><faultstring>UPnPError</faultstring><detail>'
                '<UPnPError xmlns="urn:schemas-upnp-org:control-1-0"><errorCode>714</errorCode>'
                '<errorDescription>Illegal MIME-Type</errorDescription></UPnPError></detail></s:Fault></s:Body></s:Envelope>')
        self.assertEqual(cast.soap_fault(body.encode()), "error 714 Illegal MIME-Type")

    def test_not_xml(self):
        self.assertEqual(cast.soap_fault(b"Internal Server Error"), "")


class FakeRenderer(unittest.TestCase):
    """select() then cast() against a local AVTransport: the TV must see
    Stop, SetAVTransportURI (MPEG-TS rewrite, metadata) and Play, in order."""

    @classmethod
    def setUpClass(cls):
        cls.calls = []
        calls = cls.calls

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                if self.path != "/dmr":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = SAMSUNG_DESC.format(urlbase="", control="/upnp/control/AVTransport1").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/xml")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n).decode()
                action = re.search(r"#(\w+)\"", self.headers.get("SOAPACTION", "")).group(1)
                uri = re.search(r"<CurrentURI>(.*?)</CurrentURI>", body)
                calls.append((action, uri.group(1) if uri else None, body))
                if action == "SetAVTransportURI" and "refuse" in body:
                    out = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
                           '<faultstring>UPnPError</faultstring><detail><UPnPError xmlns="urn:schemas-upnp-org:control-1-0">'
                           '<errorCode>716</errorCode><errorDescription>Resource not found</errorDescription>'
                           '</UPnPError></detail></s:Fault></s:Body></s:Envelope>').encode()
                    self.send_response(500)
                else:
                    inner = ""
                    if action == "GetTransportInfo":
                        inner = "<CurrentTransportState>PLAYING</CurrentTransportState><CurrentTransportStatus>OK</CurrentTransportStatus>"
                    elif action == "GetPositionInfo":
                        inner = "<RelTime>0:01:02</RelTime><TrackDuration>0:00:00</TrackDuration>"
                    out = ('<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body>'
                           f'<u:{action}Response xmlns:u="urn:schemas-upnp-org:service:AVTransport:1">{inner}'
                           f'</u:{action}Response></s:Body></s:Envelope>').encode()
                    self.send_response(200)
                self.send_header("Content-Type", 'text/xml; charset="utf-8"')
                self.send_header("Content-Length", str(len(out)))
                self.end_headers()
                self.wfile.write(out)

            def log_message(self, *a):
                pass

        cls.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.tmp = tempfile.TemporaryDirectory()
        cls.old_tv = cast.TV_JSON
        cast.TV_JSON = os.path.join(cls.tmp.name, "tv.json")

    @classmethod
    def tearDownClass(cls):
        cast.TV_JSON = cls.old_tv
        cls.tmp.cleanup()
        cls.srv.shutdown()
        cls.srv.server_close()

    def setUp(self):
        self.calls.clear()

    def test_no_tv_selected(self):
        try:
            os.remove(cast.TV_JSON)
        except FileNotFoundError:
            pass
        with self.assertRaises(cast.TvError) as cm:
            cast.cast("http://h/live/u/p/1.m3u8", "x", relay=False, wait=False)
        self.assertEqual(cm.exception.code, 3)

    def test_select_then_cast_sequence(self):
        d = cast.select(location=self.base + "/dmr")
        self.assertEqual(d["control_url"], self.base + "/upnp/control/AVTransport1")
        self.assertTrue(d["cast_mode"])
        self.assertEqual(oct(os.stat(cast.TV_JSON).st_mode & 0o777), "0o600")
        saved = json.load(open(cast.TV_JSON))
        self.assertEqual(saved["name"], "[TV] Samsung 7 Series (50)")

        cast.cast("http://h:8080/live/u/p/123.m3u8", "BBC One", relay=False, wait=False)
        self.assertEqual([c[0] for c in self.calls], ["Stop", "SetAVTransportURI", "Play"])
        self.assertEqual(self.calls[1][1], "http://h:8080/live/u/p/123.ts")
        self.assertIn("BBC One", self.calls[1][2])
        self.assertIn("http-get:*:video/mpeg:*", self.calls[1][2].replace("&quot;", '"'))

        st = cast.status()
        self.assertEqual(st["state"], "PLAYING")
        self.assertEqual(st["position"], "0:01:02")

        cast.transport("Stop")
        self.assertEqual(self.calls[-1][0], "Stop")

    def test_fault_is_reported_not_swallowed(self):
        cast.select(location=self.base + "/dmr")
        with self.assertRaises(cast.TvError) as cm:
            cast.cast("http://h/movie/u/p/refuse.mp4", "Nope", relay=False, wait=False)
        self.assertEqual(cm.exception.code, 5)
        self.assertIn("error 716 Resource not found", str(cm.exception))
        self.assertNotIn("Play", [c[0] for c in self.calls])

    def test_unreachable(self):
        json.dump({"control_url": "http://127.0.0.1:9/ctl", "name": "gone"}, open(cast.TV_JSON, "w"))
        with self.assertRaises(cast.TvError) as cm:
            cast.transport("Stop")
        self.assertEqual(cm.exception.code, 4)


class Relay(unittest.TestCase):
    """The relay must look like a DLNA server to the TV (HEAD, ranges,
    DLNA headers) while hiding the provider's quirks (no HEAD, redirects,
    User-Agent gate)."""

    @classmethod
    def setUpClass(cls):
        cls.file = bytes(range(256)) * 40  # 10240 bytes "movie"
        data = cls.file
        cls.hits = []
        hits = cls.hits

        class Up(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def do_HEAD(self):  # like the panels: HEAD is not welcome
                self.send_error(502)

            def do_GET(self):
                hits.append((self.path, self.headers.get("User-Agent"), self.headers.get("Range")))
                if "Python" in (self.headers.get("User-Agent") or ""):
                    self.send_error(403)
                    return
                if self.path == "/redir":
                    self.send_response(302)
                    self.send_header("Location", "/movie.mp4")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if self.path == "/live.ts":
                    self.send_response(200)
                    self.send_header("Content-Type", "video/mp2t")
                    self.send_header("Connection", "close")
                    self.end_headers()
                    for _ in range(5):
                        self.wfile.write(b"G" * 188)
                    self.close_connection = True
                    return
                rng = self.headers.get("Range")
                m = re.match(r"bytes=(\d+)-(\d*)", rng or "")
                if m:
                    a = int(m.group(1))
                    b = int(m.group(2)) if m.group(2) else len(data) - 1
                    if self.path == "/sliced.mp4":  # edge-server style: at most 3000 bytes per connection
                        b = min(b, a + 2999)
                    body = data[a:b + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {a}-{b}/{len(data)}")
                else:
                    body = data
                    self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cls.up = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Up)
        cls.base = f"http://127.0.0.1:{cls.up.server_port}"
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.up.server_close()

    def relay(self, path):
        up = cast.Upstream(self.base + path, "VLC/3.0")
        srv = cast.RelayServer(0, up)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv, up, f"http://127.0.0.1:{srv.server_address[1]}{srv.relay_path}"

    def fetch(self, url, method="GET", headers=None):
        req = urllib.request.Request(url, method=method, headers=headers or {})
        try:
            r = urllib.request.urlopen(req, timeout=5)
            body = r.read()
            return r.status, dict(r.headers), body
        except urllib.error.HTTPError as e:
            with e:
                return e.code, dict(e.headers), b""

    def test_file_probe_head_and_ranges(self):
        srv, up, url = self.relay("/movie.mp4")
        self.assertFalse(up.live)
        self.assertEqual(up.size, len(self.file))
        self.assertEqual(up.mime, "video/mp4")
        st, h, _ = self.fetch(url, "HEAD")
        self.assertEqual(st, 200)
        self.assertEqual(h["Content-Length"], str(len(self.file)))
        self.assertEqual(h["Accept-Ranges"], "bytes")
        self.assertEqual(h["transferMode.dlna.org"], "Streaming")
        self.assertIn("DLNA.ORG_OP=01", h["contentFeatures.dlna.org"])
        st, h, body = self.fetch(url, headers={"Range": "bytes=100-199"})
        self.assertEqual(st, 206)
        self.assertEqual(body, self.file[100:200])
        self.assertEqual(h["Content-Range"], f"bytes 100-199/{len(self.file)}")
        st, _, body = self.fetch(url)
        self.assertEqual((st, body), (200, self.file))
        # the provider only ever saw the player agent, never Python's
        self.assertTrue(all("VLC" in ua for _, ua, _ in self.hits if ua))

    def test_live_is_streamed_without_length(self):
        srv, up, url = self.relay("/live.ts")
        self.assertTrue(up.live)
        self.assertEqual(up.mime, "video/mpeg")  # mp2t normalised to what TVs list
        st, h, _ = self.fetch(url, "HEAD")
        self.assertEqual(st, 200)
        self.assertNotIn("Content-Length", h)
        self.assertEqual(h["Accept-Ranges"], "none")
        self.assertIn("DLNA.ORG_OP=00", h["contentFeatures.dlna.org"])
        st, h, body = self.fetch(url, headers={"Range": "bytes=0-"})  # ranges ignored on live
        self.assertEqual(st, 200)
        self.assertEqual(body, b"G" * 188 * 5)

    def test_tv_style_scan_reuses_one_upstream_connection(self):
        # A TV parses a file with open-ended ranges a few KB apart and jumps
        # to the end; that must not reopen the provider each time.
        srv, up, url = self.relay("/movie.mp4")
        before = len(self.hits)
        got = []
        for start in (0, 2048, 4096, 6144, 8192):
            st, h, body = self.fetch(url, headers={"Range": f"bytes={start}-"})
            self.assertEqual(st, 206)
            self.assertEqual(h["Content-Range"], f"bytes {start}-{len(self.file)-1}/{len(self.file)}")
            self.assertEqual(body, self.file[start:])
            got.append(len(body))
        self.assertEqual(len(self.hits) - before, 1, "sequential reads must share one upstream connection")
        st, _, body = self.fetch(url, headers={"Range": "bytes=-128"})  # suffix range: the tail
        self.assertEqual(st, 206)
        self.assertEqual(body, self.file[-128:])
        st, _, body = self.fetch(url, headers={"Range": "bytes=100-199"})  # bounded, back at the start
        self.assertEqual((st, body), (206, self.file[100:200]))

    def test_sliced_provider_is_stitched_back_together(self):
        # The provider ends each connection after a slice; the TV must still
        # receive the whole range in one response.
        srv, up, url = self.relay("/sliced.mp4")
        self.assertEqual(up.size, len(self.file))
        before = up.opens
        st, h, body = self.fetch(url, headers={"Range": "bytes=100-"})
        self.assertEqual(st, 206)
        self.assertEqual(h["Content-Length"], str(len(self.file) - 100))
        self.assertEqual(body, self.file[100:])
        self.assertGreater(up.opens - before, 2)
        st, _, body = self.fetch(url)
        self.assertEqual((st, body), (200, self.file))

    def test_bad_range_is_answered_with_the_whole_file(self):
        srv, up, url = self.relay("/movie.mp4")
        st, h, body = self.fetch(url, headers={"Range": "bytes=999999999-"})
        self.assertEqual((st, body), (200, self.file))

    def test_parse_range(self):
        self.assertEqual(cast.parse_range("bytes=0-", 100), (0, 99))
        self.assertEqual(cast.parse_range("bytes=10-19", 100), (10, 19))
        self.assertEqual(cast.parse_range("bytes=90-500", 100), (90, 99))
        self.assertEqual(cast.parse_range("bytes=-10", 100), (90, 99))
        self.assertIsNone(cast.parse_range("bytes=100-", 100))
        self.assertIsNone(cast.parse_range("", 100))
        self.assertIsNone(cast.parse_range("bytes=0-", None))

    def test_redirect_is_followed_by_the_relay(self):
        srv, up, url = self.relay("/redir")
        self.assertEqual(up.size, len(self.file))
        st, _, body = self.fetch(url)
        self.assertEqual((st, body), (200, self.file))

    def test_unknown_path_is_404(self):
        srv, up, url = self.relay("/movie.mp4")
        st, _, _ = self.fetch(url.rsplit("/", 2)[0] + "/other/stream.mp4")
        self.assertEqual(st, 404)
        st, _, _ = self.fetch(url, "HEAD")
        self.assertEqual(st, 200)

    def test_provider_refusal_surfaces(self):
        with self.assertRaises(cast.TvError) as cm:
            cast.Upstream(self.base + "/movie.mp4", "Python-urllib/3")
        self.assertEqual(cm.exception.code, 6)


class WatchPlayback(unittest.TestCase):
    def fake_tv(self, states):
        it = iter(states)
        def soap(ctl, action, args=None, timeout=None):
            st = next(it)
            return {"CurrentTransportState": st[0], "CurrentTransportStatus": st[1]}
        return soap

    def test_returns_when_tv_stops_after_playing(self):
        old = cast.soap_call
        cast.soap_call = self.fake_tv([("TRANSITIONING", "OK"), ("PLAYING", "OK"), ("PLAYING", "OK"), ("STOPPED", "OK"), ("STOPPED", "OK")])
        try:
            cast.watch_playback("ctl", poll=0.01, grace=5)
        finally:
            cast.soap_call = old

    def test_error_status_raises(self):
        old = cast.soap_call
        cast.soap_call = self.fake_tv([("TRANSITIONING", "OK"), ("STOPPED", "ERROR_OCCURRED")])
        try:
            with self.assertRaises(cast.TvError) as cm:
                cast.watch_playback("ctl", poll=0.01, grace=5)
            self.assertEqual(cm.exception.code, 8)
        finally:
            cast.soap_call = old

    def test_never_plays_gives_up(self):
        old = cast.soap_call
        cast.soap_call = self.fake_tv([("STOPPED", "OK")] * 50)
        try:
            with self.assertRaises(cast.TvError):
                cast.watch_playback("ctl", poll=0.01, grace=0.1)
        finally:
            cast.soap_call = old


if __name__ == "__main__":
    unittest.main()

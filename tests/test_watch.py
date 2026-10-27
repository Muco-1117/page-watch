import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError

from page_watch import WatchStore, fetch, normalize


def response(content):
    return {"not_modified": False, "content": content, "content_type": "text/html", "etag": "v1", "modified": ""}


class WatchTest(unittest.TestCase):
    def setUp(self):
        self.store = WatchStore(":memory:")
        self.watch_id = self.store.add("release", "https://example.org/releases", "content", [r"Updated: [0-9-]+"])

    def tearDown(self):
        self.store.close()

    def test_initial_unchanged_changed_and_diff_history(self):
        first = '<main id="content"><p>Version 1</p><p>Updated: 2026-01-01</p></main>'
        self.assertEqual(self.store.check(self.watch_id, lambda *args: response(first))["status"], "initial")
        second = first.replace("2026-01-01", "2026-01-02") + "<footer>ignored</footer>"
        self.assertEqual(self.store.check(self.watch_id, lambda *args: response(second))["status"], "unchanged")
        third = second.replace("Version 1", "Version 2")
        self.assertEqual(self.store.check(self.watch_id, lambda *args: response(third))["status"], "changed")
        events = self.store.history(self.watch_id)
        self.assertEqual(len(events), 2)
        self.assertIn("-Version 1", events[0]["diff"])
        self.assertIn("+Version 2", events[0]["diff"])

    def test_failures_and_missing_element_preserve_last_good_baseline(self):
        content = '<div id="content">Version 1</div>'
        self.store.check(self.watch_id, lambda *args: response(content))
        def fail(*args):
            raise HTTPError("https://example.org", 503, "unavailable", {}, None)
        self.assertEqual(self.store.check(self.watch_id, fail)["status"], "error")
        self.assertEqual(self.store.check(self.watch_id, lambda *args: response("<p>Login required</p>"))["status"], "error")
        self.assertEqual(self.store.check(self.watch_id, lambda *args: response(content))["status"], "unchanged")
        self.assertEqual(len(self.store.history(self.watch_id)), 1)

    def test_scripts_entities_and_nested_elements(self):
        text = normalize('<main id="content"><h1>A &amp; B</h1><p>Hello <b>world</b>!</p><script>secret()</script><img src="x"></main><p>outside</p>', "text/html", "content")
        self.assertEqual(text, "A & B\nHello world!\n")

    def test_conditional_304_requests_and_real_http_fetch(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                if self.headers.get("If-None-Match") == '"v1"':
                    self.send_response(304)
                    self.end_headers()
                else:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("ETag", '"v1"')
                    self.end_headers()
                    self.wfile.write(b"<p>Release 1</p>")
            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            watch_id = self.store.add("local", f"http://127.0.0.1:{server.server_port}/")
            self.assertEqual(self.store.check(watch_id)["status"], "initial")
            self.assertEqual(self.store.check(watch_id)["status"], "unchanged")
            self.assertEqual(len(self.store.history(watch_id)), 1)
        finally:
            server.shutdown()
            thread.join()
            server.server_close()

    def test_rejects_invalid_regex_and_embedded_credentials(self):
        with self.assertRaises(ValueError):
            self.store.add("invalid", "file:///etc/passwd")
        with self.assertRaises(ValueError):
            self.store.add("invalid", "https://name:password@example.org")
        with self.assertRaises(re.error):
            self.store.add("invalid", "https://example.org", ignore=["["])


if __name__ == "__main__":
    unittest.main()
import re

"""The Information page's plugin list from the marketplace catalog (plugin_catalog)."""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import plugin_catalog as pc  # noqa: E402

CATALOG = {"plugins": [
    {"id": "ubruckhaus.wlan-monitor", "name": "WLAN Monitor", "repo": "https://github.com/uBruckhaus/omarchy-wlan",
     "description": "Who is on your Wi-Fi.", "version": "0.7.1", "verificationStatus": "verified"},
    {"id": "ubruckhaus.new-thing", "name": "New Thing", "repo": "https://github.com/uBruckhaus/new-thing",
     "description": "Does a new thing for you. And more besides.", "verificationStatus": "unverified"},
    {"id": "someone.other", "name": "Other", "repo": "https://github.com/someone/other", "description": "x"},
    {"id": "../bad", "name": "Bad", "repo": "https://github.com/uBruckhaus/bad"},
]}


def answer(status: int, body: bytes = b"", etag: str = '"abc"') -> bytes:
    return f"HTTP/2 {status}\r\netag: {etag}\r\ncontent-type: text/plain\r\n\r\n".encode() + body


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.cache = Path(self.tmp.name) / "plugin-catalog.json"

    def test_only_the_authors_plugins(self):
        found = pc.parse(CATALOG)
        self.assertEqual([p["id"] for p in found], ["ubruckhaus.wlan-monitor", "ubruckhaus.new-thing"])
        self.assertTrue(found[0]["verified"])

    def test_read_then_not_changed_then_offline(self):
        asked = []

        def fetch(url, **kwargs):
            asked.append(kwargs.get("headers", {}))
            return answer(200, json.dumps(CATALOG).encode())
        self.assertEqual(len(pc.refresh(fetch, self.cache, now=1000)), 2)
        self.assertEqual(asked, [{}])
        self.assertEqual(len(pc.refresh(fetch, self.cache, now=1500)), 2)       # within the hour: no request
        self.assertEqual(len(asked), 1)

        def not_changed(url, **kwargs):
            asked.append(kwargs.get("headers", {}))
            return answer(304)
        self.assertEqual(len(pc.refresh(not_changed, self.cache, now=9000)), 2)
        self.assertEqual(asked[-1], {"If-None-Match": '"abc"'})

        def offline(url, **kwargs):
            raise OSError("no network")
        self.assertEqual(len(pc.refresh(offline, self.cache, now=99000)), 2)    # the cached list

    def test_cards_known_first_new_after(self):
        cards = pc.cards(pc.parse(CATALOG))
        self.assertEqual(cards[0][0], "ubruckhaus.wlan-monitor")
        self.assertEqual(cards[0][3], "Who is on your Wi-Fi, with a network map")    # the built-in one-liner
        self.assertEqual(cards[1], ("ubruckhaus.new-thing", "New Thing", pc.NEW_GLYPH, "Does a new thing for you."))
        self.assertEqual(len(pc.cards([])), len(pc.more_plugins.PLUGINS))          # nothing read yet

    def test_deeply_nested_cache_is_no_cache(self):
        self.cache.write_text('{"plugins": ' + "[" * 100000 + "]" * 100000 + "}")
        self.assertEqual(pc.load(self.cache), {})

    def test_redirect_headers(self):
        raw = b"HTTP/1.1 301 Moved\r\nlocation: x\r\n\r\n" + answer(200, b"{}")
        self.assertEqual(pc.split_response(raw), (200, {"etag": '"abc"', "content-type": "text/plain"}, b"{}"))


if __name__ == "__main__":
    unittest.main()

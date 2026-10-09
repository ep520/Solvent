import json
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402


def call(base, method, path, body=None, headers=None):
    data = body if isinstance(body, (bytes, type(None))) else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, r.headers, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode()


class TriggerApiTest(unittest.TestCase):
    def setUp(self):
        self.srv = server.make_server("127.0.0.1", 0, data_file=None, limit=3)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = f"http://127.0.0.1:{self.srv.server_address[1]}"

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def post(self, body):
        status, _, text = call(self.base, "POST", "/triggers", body)
        return status, json.loads(text)

    def test_post_then_get_lists_the_trigger(self):
        status, created = self.post({"url": "https://example.com/a", "title": "First", "description": "why"})
        self.assertEqual(status, 201)
        self.assertEqual(created["id"], 1)
        status, _, text = call(self.base, "GET", "/triggers")
        listed = json.loads(text)
        self.assertEqual((status, listed["count"]), (200, 1))
        self.assertEqual(listed["triggers"][0]["title"], "First")
        self.assertEqual(listed["triggers"][0]["description"], "why")

    def test_description_is_optional(self):
        status, created = self.post({"url": "https://example.com/a", "title": "No description"})
        self.assertEqual((status, created["description"]), (201, ""))

    def test_titel_is_accepted_as_alias(self):
        status, created = self.post({"url": "https://example.com/a", "titel": "Deutsch"})
        self.assertEqual((status, created["title"]), (201, "Deutsch"))

    def test_missing_or_bad_fields_are_rejected(self):
        for body in ({"title": "no url"}, {"url": "https://example.com"}, {"url": "", "title": "x"},
                     {"url": "javascript:alert(1)", "title": "x"}, {"url": "example.com/a", "title": "x"},
                     {"url": "https://example.com", "title": "x", "description": 5}, ["not", "an", "object"]):
            status, answer = self.post(body)
            self.assertEqual(status, 400, body)
            self.assertIn("error", answer)
        status, _, _ = call(self.base, "POST", "/triggers", b"not json")
        self.assertEqual(status, 400)

    def test_browser_gets_html_with_links_in_a_new_tab_and_escaped_text(self):
        self.post({"url": "https://example.com/a?x=1&y=2", "title": "<script>alert(1)</script>"})
        status, headers, text = call(self.base, "GET", "/triggers", headers={"Accept": "text/html"})
        self.assertEqual(status, 200)
        self.assertIn("text/html", headers["Content-Type"])
        self.assertIn('href="https://example.com/a?x=1&amp;y=2" target="_blank"', text)
        self.assertNotIn("<script>alert(1)</script>", text)
        _, headers, _ = call(self.base, "GET", "/triggers?format=json", headers={"Accept": "text/html"})
        self.assertIn("application/json", headers["Content-Type"])

    def test_only_the_newest_triggers_are_kept(self):
        for i in range(5):
            self.post({"url": f"https://example.com/{i}", "title": str(i)})
        _, _, text = call(self.base, "GET", "/triggers")
        self.assertEqual([t["title"] for t in json.loads(text)["triggers"]], ["2", "3", "4"])

    def test_delete_clears_the_list(self):
        self.post({"url": "https://example.com/a", "title": "x"})
        self.assertEqual(call(self.base, "DELETE", "/triggers")[0], 204)
        self.assertEqual(json.loads(call(self.base, "GET", "/triggers")[2])["count"], 0)

    def test_cors_preflight_and_docs(self):
        status, headers, _ = call(self.base, "OPTIONS", "/triggers")
        self.assertEqual((status, headers["Access-Control-Allow-Origin"]), (204, "*"))
        self.assertEqual(call(self.base, "GET", "/docs")[0], 200)
        self.assertEqual(call(self.base, "GET", "/nope")[0], 404)


class PersistenceTest(unittest.TestCase):
    def test_data_file_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "triggers.json"
            first = server.Store(path)
            first.add("https://example.com/a", "kept", "")
            second = server.Store(path)
            self.assertEqual([t["title"] for t in second.all()], ["kept"])
            self.assertEqual(second.add("https://example.com/b", "next", "")["id"], 2)


if __name__ == "__main__":
    unittest.main()

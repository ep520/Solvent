import json
import os
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from callguard import models  # noqa: E402


class FakeBackend(BaseHTTPRequestHandler):
    replies = []
    requests = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = self.rfile.read(int(self.headers["Content-Length"]))
        FakeBackend.requests.append((self.path, dict(self.headers), body))
        code, payload = FakeBackend.replies.pop(0)
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def chat_reply(content):
    return 200, {"choices": [{"message": {"content": content}}]}


class RouterTest(unittest.TestCase):
    def setUp(self):
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), FakeBackend)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        FakeBackend.replies, FakeBackend.requests = [], []
        url = f"http://127.0.0.1:{self.srv.server_address[1]}/v1"
        base = {"api": "openai_compatible", "base_url": url, "retry_backoff": 0, "self_hostable": True}
        self.config = {
            "defaults": {"chat": "a", "asr": "a"},
            "profiles": {
                "a": {**base, "chat_model": "model-a", "asr_model": "asr-a", "json_mode": "json_schema"},
                "b": {**base, "chat_model": "model-b", "json_mode": "none", "api_key_env": "TEST_ROUTER_KEY"},
            },
        }

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        os.environ.pop("CALLGUARD_CHAT", None)
        os.environ.pop("TEST_ROUTER_KEY", None)

    def test_json_schema_mode_sends_schema_and_parses_reply(self):
        FakeBackend.replies = [chat_reply('{"events": []}')]
        out = models.chat_json([{"role": "user", "content": "hi"}], {"type": "object"}, config=self.config)
        self.assertEqual(out, {"events": []})
        path, _, body = FakeBackend.requests[0]
        sent = json.loads(body)
        self.assertEqual(path, "/v1/chat/completions")
        self.assertEqual((sent["model"], sent["temperature"]), ("model-a", 0))
        self.assertEqual(sent["response_format"]["type"], "json_schema")

    def test_env_var_switches_profile_and_adds_key(self):
        os.environ["CALLGUARD_CHAT"] = "b"
        os.environ["TEST_ROUTER_KEY"] = "secret"
        FakeBackend.replies = [chat_reply('Sure:\n```json\n{"ok": true}\n```')]
        out = models.chat_json([{"role": "user", "content": "hi"}], {"type": "object"}, config=self.config)
        self.assertEqual(out, {"ok": True})
        _, headers, body = FakeBackend.requests[0]
        sent = json.loads(body)
        self.assertEqual(sent["model"], "model-b")
        self.assertNotIn("response_format", sent)
        self.assertIn("with this shape", sent["messages"][0]["content"])
        self.assertEqual(headers["Authorization"], "Bearer secret")

    def test_env_file_is_loaded_without_overriding_the_shell(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('# comment\nexport TEST_ENV_A="quoted value"\nTEST_ENV_B=plain\nTEST_ENV_C=from-file\n')
            os.environ["TEST_ENV_C"] = "from-shell"
            try:
                models.load_env(env)
                self.assertEqual((os.environ["TEST_ENV_A"], os.environ["TEST_ENV_B"], os.environ["TEST_ENV_C"]),
                                 ("quoted value", "plain", "from-shell"))
            finally:
                for k in ("TEST_ENV_A", "TEST_ENV_B", "TEST_ENV_C"):
                    os.environ.pop(k, None)

    def test_prompt_injected_modes_send_a_compact_skeleton_not_the_schema(self):
        schema = {"type": "object", "required": ["events"], "properties": {"events": {"type": "array", "items": {
            "type": "object", "properties": {"state": {"enum": ["true", "false", "unknown"]}, "quote": {"type": "string"}}}}}}
        self.assertEqual(models.skeleton(schema), {"events": [{"state": "true|false|unknown", "quote": "..."}]})
        os.environ["TEST_ROUTER_KEY"] = "k"
        FakeBackend.replies = [chat_reply('{"events": []}')]
        models.chat_json([{"role": "user", "content": "hi"}], schema, profile="b", config=self.config)
        injected = json.loads(FakeBackend.requests[0][2])["messages"][0]["content"]
        self.assertIn('{"events":[{"state":"true|false|unknown","quote":"..."}]}', injected)
        self.assertNotIn('"required"', injected)

    def test_context_guard_refuses_instead_of_truncating(self):
        self.config["profiles"]["a"].update(context_tokens=1000, max_output_tokens=500)
        with self.assertRaisesRegex(models.ModelError, "context is 1000"):
            models.chat_json([{"role": "user", "content": "x" * 3000}], {}, config=self.config)
        self.assertEqual(FakeBackend.requests, [])

    def test_output_cap_is_sent_when_configured(self):
        self.config["profiles"]["a"].update(context_tokens=100000, max_output_tokens=4096)
        FakeBackend.replies = [chat_reply('{"ok": 1}')]
        models.chat_json([{"role": "user", "content": "short"}], {}, config=self.config)
        self.assertEqual(json.loads(FakeBackend.requests[0][2])["max_tokens"], 4096)

    def test_missing_key_is_a_clear_error(self):
        with self.assertRaisesRegex(models.ModelError, "TEST_ROUTER_KEY"):
            models.chat_json([], {}, profile="b", config=self.config)

    def test_retries_server_error_then_invalid_json(self):
        FakeBackend.replies = [(500, {"error": "busy"}), chat_reply("not json"), chat_reply('{"ok": 1}')]
        out = models.chat_json([{"role": "user", "content": "hi"}], {}, config=self.config)
        self.assertEqual(out, {"ok": 1})
        self.assertEqual(len(FakeBackend.requests), 3)

    def test_client_error_is_not_retried(self):
        FakeBackend.replies = [(400, {"error": "bad"})]
        with self.assertRaisesRegex(models.ModelError, "400"):
            models.chat_json([], {}, config=self.config)
        self.assertEqual(len(FakeBackend.requests), 1)

    def test_transcribe_returns_segments_with_quality_fields(self):
        FakeBackend.replies = [(200, {"segments": [
            {"start": 0.0, "end": 2.5, "text": " Grüezi ", "avg_logprob": -0.2, "no_speech_prob": 0.01, "tokens": [1]},
            {"start": 2.5, "end": 4.0, "text": "Danke"},
        ]})]
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(b"RIFFfake")
            f.flush()
            segs = models.transcribe(f.name, config=self.config)
        self.assertEqual(segs[0], {"id": "s001", "start": 0.0, "end": 2.5, "text": "Grüezi",
                                   "avg_logprob": -0.2, "no_speech_prob": 0.01})
        self.assertEqual(segs[1]["id"], "s002")
        path, headers, body = FakeBackend.requests[0]
        self.assertEqual(path, "/v1/audio/transcriptions")
        self.assertIn(b'name="model"\r\n\r\nasr-a', body)
        self.assertIn(b"RIFFfake", body)

    def test_transcribe_sends_a_verbatim_prompt_when_requested(self):
        FakeBackend.replies = [(200, {"text": "Grüezi", "duration": 1.0})]
        with tempfile.NamedTemporaryFile(suffix=".wav") as f:
            f.write(b"RIFFfake")
            f.flush()
            models.transcribe(f.name, prompt="Do not substitute words.", config=self.config)
        self.assertIn(b'name="prompt"\r\n\r\nDo not substitute words.', FakeBackend.requests[0][2])

    def test_profile_without_asr_model_is_rejected(self):
        with self.assertRaisesRegex(models.ModelError, "asr_model"):
            models.transcribe("x.wav", profile="b", config=self.config)

    def test_submission_check_rejects_a_non_self_hostable_profile(self):
        self.config["profiles"]["a"]["self_hostable"] = False
        with self.assertRaisesRegex(models.ModelError, "not self-hostable"):
            models.check("chat", config=self.config, require_self_hostable=True)


FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, sys
log = os.environ["FAKE_CLAUDE_LOG"]
calls = json.load(open(log)) if os.path.exists(log) else []
calls.append({"args": sys.argv[1:], "stdin": sys.stdin.read(), "cwd": os.getcwd()})
json.dump(calls, open(log, "w"))
mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "error" or (mode == "flaky" and len(calls) == 1):
    print(json.dumps({"type": "result", "subtype": "error_during_execution", "is_error": True, "result": "boom"}))
else:
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "{}",
                      "structured_output": {"events": [], "n": len(calls)}}))
"""


class ClaudeCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cmd = Path(self.tmp.name) / "fake-claude"
        cmd.write_text(FAKE_CLAUDE)
        cmd.chmod(0o755)
        self.log = Path(self.tmp.name) / "calls.json"
        os.environ["FAKE_CLAUDE_LOG"] = str(self.log)
        self.config = {"defaults": {"chat": "cc"}, "profiles": {"cc": {
            "api": "claude_cli", "command": str(cmd), "chat_model": "claude-sonnet-5", "retries": 1, "self_hostable": True}}}

    def tearDown(self):
        os.environ.pop("FAKE_CLAUDE_LOG", None)
        os.environ.pop("FAKE_CLAUDE_MODE", None)
        self.tmp.cleanup()

    def calls(self):
        return json.loads(self.log.read_text())

    def test_sends_system_prompt_schema_and_stdin_without_tools(self):
        messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "TRANSCRIPT"}]
        out = models.chat_json(messages, {"type": "object"}, config=self.config)
        self.assertEqual(out, {"events": [], "n": 1})
        call = self.calls()[0]
        args = call["args"]
        self.assertEqual(args[args.index("--model") + 1], "claude-sonnet-5")
        self.assertEqual(args[args.index("--system-prompt") + 1], "SYS")
        self.assertEqual(args[args.index("--tools") + 1], "")
        self.assertEqual(json.loads(args[args.index("--json-schema") + 1]), {"type": "object"})
        self.assertEqual(call["stdin"], "TRANSCRIPT")
        self.assertNotEqual(Path(call["cwd"]).resolve(), Path.cwd().resolve())

    def test_retries_once_after_an_error_result(self):
        os.environ["FAKE_CLAUDE_MODE"] = "flaky"
        self.assertEqual(models.chat_json([], {}, config=self.config)["n"], 2)

    def test_persistent_error_raises(self):
        os.environ["FAKE_CLAUDE_MODE"] = "error"
        with self.assertRaisesRegex(models.ModelError, "boom"):
            models.chat_json([], {}, config=self.config)

    def test_missing_command_fails_check(self):
        self.config["profiles"]["cc"]["command"] = "/nonexistent/claude"
        with self.assertRaisesRegex(models.ModelError, "not found"):
            models.check("chat", config=self.config)


if __name__ == "__main__":
    unittest.main()

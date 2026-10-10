"""Model router: config/models.json decides which backend serves chat and ASR.

Pick a profile per task with CALLGUARD_CHAT / CALLGUARD_ASR, or pass profile=...
Adapters: "openai_compatible" (OpenAI, vLLM, Ollama, llama.cpp server, speaches,
private gateways) and "claude_cli" (the local `claude -p` CLI, chat only).
Another backend is one more entry in ADAPTERS.
"""
import json
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_env(path=ROOT / ".env"):
    """Load KEY=VALUE lines from a local, untracked .env; variables already set in the shell win."""
    if not Path(path).is_file():
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.removeprefix("export ").split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key.strip(), value)


load_env()
CONFIG_PATH = Path(os.environ.get("CALLGUARD_MODELS", ROOT / "config" / "models.json"))
_warned = set()


class ModelError(RuntimeError):
    pass


def load_config(path=CONFIG_PATH):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def resolve(task, profile=None, config=None):
    """Return the settings for task 'chat' or 'asr', with the chosen model under 'model'."""
    config = config or load_config()
    name = profile or os.environ.get(f"CALLGUARD_{task.upper()}") or config["defaults"][task]
    if name not in config["profiles"]:
        raise ModelError(f"unknown {task} profile '{name}'; known: {', '.join(config['profiles'])}")
    p = dict(config["profiles"][name], name=name)
    # The served model name can come from the environment (e.g. whatever runpod/setup.sh picked for the GPU).
    p["model"] = os.environ.get(p.get(f"{task}_model_env", ""), "") or p.get(f"{task}_model")
    if not p["model"]:
        raise ModelError(f"profile '{name}' has no {task}_model")
    if p.get("api", "openai_compatible") not in ADAPTERS:
        raise ModelError(f"profile '{name}' uses unknown api '{p.get('api')}'")
    if not p.get("self_hostable") and name not in _warned:
        _warned.add(name)
        print(f"callguard: profile '{name}' is not self-hostable (the README asks for self-hostable "
              "models in the core pipeline).", file=sys.stderr)
    return p


def check(task, profile=None, config=None, require_self_hostable=False):
    """Resolve a profile and fail now if it cannot be used (missing URL, key or command)."""
    p = resolve(task, profile, config)
    if require_self_hostable and not p.get("self_hostable"):
        raise ModelError(f"profile '{p['name']}' is not self-hostable; choose a self-hostable {task} profile")
    ADAPTERS[p.get("api", "openai_compatible")]["check"](p)
    return p


def probe(task, profile=None, config=None, require_self_hostable=False):
    """Verify that the selected OpenAI-compatible endpoint is live and serves the configured model.

    This deliberately bypasses CallGuard caches. Release acceptance uses it before smoke/full evaluation,
    so a green cached replay cannot be mistaken for a running self-hosted deployment.
    """
    p = check(task, profile, config, require_self_hostable=require_self_hostable)
    if p.get("api", "openai_compatible") != "openai_compatible":
        raise ModelError(f"profile '{p['name']}' cannot be live-probed through /models")
    url = _base_url(p) + "/models"
    try:
        req = urllib.request.Request(url, headers=_headers(p))
        with urllib.request.urlopen(req, timeout=min(p.get("timeout", 120), 15)) as response:
            payload = json.loads(response.read())
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:500]
        error.close()
        raise ModelError(f"{error.code} from {url}: {detail}") from None
    except (urllib.error.URLError, TimeoutError, ValueError) as error:
        raise ModelError(f"cannot probe {url}: {error}") from None
    served = {item.get("id") for item in payload.get("data", []) if isinstance(item, dict)}
    if p["model"] not in served:
        raise ModelError(f"profile '{p['name']}' expects model '{p['model']}', served: {', '.join(sorted(x for x in served if x)) or 'none'}")
    return p


def chat_json(messages, schema, profile=None, config=None):
    """Send chat messages and return the reply parsed as one JSON object following schema."""
    p = resolve("chat", profile, config)
    return ADAPTERS[p.get("api", "openai_compatible")]["chat_json"](p, messages, schema)


def transcribe(audio_path, language="de", prompt=None, profile=None, config=None):
    """Transcribe audio into segments: [{id, start, end, text, avg_logprob?, no_speech_prob?, ...}]."""
    p = resolve("asr", profile, config)
    return ADAPTERS[p.get("api", "openai_compatible")]["transcribe"](p, Path(audio_path), language, prompt)


def _base_url(p):
    url = p.get("base_url") or os.environ.get(p.get("base_url_env", ""), "")
    if not url:
        raise ModelError(f"profile '{p['name']}': set base_url or the env var {p.get('base_url_env')}")
    return url.rstrip("/")


def _headers(p):
    env = p.get("api_key_env")
    if not env:
        return {}
    key = os.environ.get(env)
    if not key:
        raise ModelError(f"profile '{p['name']}': set the env var {env}")
    return {"Authorization": f"Bearer {key}"}


def _post(p, path, body, content_type):
    url = _base_url(p) + path
    headers = {"Content-Type": content_type, **_headers(p)}
    retries, backoff = p.get("retries", 2), p.get("retry_backoff", 1.0)
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=p.get("timeout", 120)) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:500]
            e.close()
            if (e.code in (408, 429) or e.code >= 500) and attempt < retries:
                time.sleep(backoff * 2 ** attempt)
                continue
            raise ModelError(f"{e.code} from {url}: {detail}") from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(backoff * 2 ** attempt)
                continue
            raise ModelError(f"cannot reach {url}: {e}") from None


THINK = re.compile(r"<think>.*?</think>", re.S)


def _parse_json_object(text):
    # Reasoning models (Qwen3 Thinking, DeepSeek-R1) may put their reasoning in the reply when the server
    # runs without a reasoning parser; it can contain braces, so drop it before looking for the JSON.
    text = THINK.sub("", text)
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def skeleton(schema):
    """Compact example of the JSON shape, far shorter than the schema itself; used when it must go in the prompt."""
    if "enum" in schema:
        return "|".join(str(v) for v in schema["enum"])
    kind = schema.get("type")
    if kind == "array":
        return [skeleton(schema.get("items", {}))]
    if kind == "object" or "properties" in schema:
        return {k: skeleton(v) for k, v in schema.get("properties", {}).items()}
    return "..."


def estimate_tokens(text):
    """Conservative token estimate for German/Swiss German text (~3 characters per token)."""
    return len(text) // 3 + 1


def _check_context(p, messages):
    limit = p.get("context_tokens")
    if not limit:
        return
    needed = sum(estimate_tokens(m["content"]) for m in messages) + p.get("max_output_tokens", 2048)
    if needed > limit:
        raise ModelError(f"profile '{p['name']}': call needs about {needed} tokens, context is {limit}; "
                         "refusing instead of letting the server truncate the transcript")


def _oa_chat_json(p, messages, schema):
    mode = p.get("json_mode", "json_schema")
    body = {"model": p["model"], "messages": list(messages), "temperature": p.get("temperature", 0),
            "seed": p.get("seed", 7)}
    # Server-specific fields, e.g. vLLM's {"chat_template_kwargs": {"enable_thinking": false}} or top_p/top_k.
    body.update(p.get("extra_body") or {})
    if p.get("max_output_tokens"):
        body["max_tokens"] = p["max_output_tokens"]
    if mode == "json_schema":
        body["response_format"] = {"type": "json_schema",
                                   "json_schema": {"name": "result", "schema": schema, "strict": p.get("strict_schema", False)}}
    else:
        shape = json.dumps(skeleton(schema), ensure_ascii=False, separators=(",", ":"))
        body["messages"].insert(0, {"role": "system", "content":
                                    "Reply with exactly one JSON object with this shape (a|b means one of the values):\n" + shape})
        if mode == "json_object":
            body["response_format"] = {"type": "json_object"}
    _check_context(p, body["messages"])
    last_error = None
    for _ in range(p.get("parse_retries", 1) + 1):
        reply = _post(p, "/chat/completions", json.dumps(body).encode(), "application/json")
        try:
            return _parse_json_object(reply["choices"][0]["message"]["content"] or "")
        except (KeyError, IndexError, ValueError) as e:
            last_error = e
    raise ModelError(f"profile '{p['name']}' returned no valid JSON: {last_error}")


def _oa_transcribe(p, audio_path, language, prompt=None):
    boundary = uuid.uuid4().hex
    fields = [("model", p["model"]), ("language", language), ("response_format", "verbose_json"),
              ("temperature", "0"), ("timestamp_granularities[]", "segment")]
    if prompt:
        fields.append(("prompt", prompt))
    parts = [f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode() for k, v in fields]
    parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\n'
                 f"Content-Type: audio/wav\r\n\r\n".encode() + audio_path.read_bytes() + b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode())
    reply = _post(p, "/audio/transcriptions", b"".join(parts), f"multipart/form-data; boundary={boundary}")
    raw = reply.get("segments") or [{"start": 0.0, "end": reply.get("duration"), "text": reply.get("text", "")}]
    keep = ("avg_logprob", "no_speech_prob", "compression_ratio")
    return [{"id": f"s{i:03d}", "start": s.get("start"), "end": s.get("end"), "text": s.get("text", "").strip(),
             **{k: s[k] for k in keep if k in s}} for i, s in enumerate(raw, 1)]


def _oa_check(p):
    _base_url(p)
    _headers(p)


def _cli_check(p):
    if not shutil.which(p.get("command", "claude")):
        raise ModelError(f"profile '{p['name']}': command '{p.get('command', 'claude')}' not found on PATH")


def _cli_chat_json(p, messages, schema):
    """One non-interactive `claude -p` call: no tools, no session, no user/project settings, neutral cwd."""
    system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
    user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
    cmd = [shutil.which(p.get("command", "claude")), "-p", "--model", p["model"], "--output-format", "json",
           "--tools", "", "--no-session-persistence", "--setting-sources", "",
           "--system-prompt", system, "--json-schema", json.dumps(schema)]
    retries, last_error = p.get("retries", 1), None
    for attempt in range(retries + 1):
        with tempfile.TemporaryDirectory() as cwd:
            try:
                run = subprocess.run(cmd, input=user, capture_output=True, text=True, cwd=cwd, timeout=p.get("timeout", 300))
            except subprocess.TimeoutExpired:
                last_error = f"timeout after {p.get('timeout', 300)}s"
                continue
        try:
            out = json.loads(run.stdout)
        except ValueError:
            last_error = f"exit {run.returncode}: {(run.stderr or run.stdout)[:300]}"
            continue
        if out.get("is_error") or out.get("subtype") != "success":
            last_error = f"{out.get('subtype')}: {str(out.get('result'))[:300]}"
            continue
        if isinstance(out.get("structured_output"), dict):
            return out["structured_output"]
        try:
            return _parse_json_object(out.get("result") or "")
        except ValueError as e:
            last_error = e
    raise ModelError(f"profile '{p['name']}' failed: {last_error}")


def _cli_transcribe(p, audio_path, language, prompt=None):
    raise ModelError(f"profile '{p['name']}' (claude_cli) cannot transcribe audio")


_fw_models, _fw_lock = {}, threading.Lock()


def _fw_check(p):
    try:
        import faster_whisper  # noqa: F401
    except ImportError:
        raise ModelError(f"profile '{p['name']}': run  pip install faster-whisper") from None


def _fw_transcribe(p, audio_path, language, prompt=None):
    """Whisper in this process via faster-whisper (CTranslate2): no server, GPU if present, else CPU.

    The English instruction prompt is not passed by default: Whisper treats a prompt as preceding speech,
    and English text there can pull the output towards English. Set "initial_prompt" in the profile if needed."""
    from faster_whisper import WhisperModel
    with _fw_lock:                       # one model per process; transcribe one file at a time on it
        model = _fw_models.get(p["model"])
        if model is None:
            model = _fw_models[p["model"]] = WhisperModel(p["model"], device=p.get("device", "auto"),
                                                          compute_type=p.get("compute_type", "default"))
        segments, _ = model.transcribe(str(audio_path), language=language, initial_prompt=p.get("initial_prompt"),
                                       temperature=0, beam_size=p.get("beam_size", 5),
                                       vad_filter=p.get("vad_filter", False))
        segments = list(segments)
    return [{"id": f"s{i:03d}", "start": s.start, "end": s.end, "text": s.text.strip(), "avg_logprob": s.avg_logprob,
             "no_speech_prob": s.no_speech_prob, "compression_ratio": s.compression_ratio}
            for i, s in enumerate(segments, 1)]


def _fw_chat_json(p, messages, schema):
    raise ModelError(f"profile '{p['name']}' (faster_whisper) is for ASR only")


ADAPTERS = {
    "openai_compatible": {"check": _oa_check, "chat_json": _oa_chat_json, "transcribe": _oa_transcribe},
    "claude_cli": {"check": _cli_check, "chat_json": _cli_chat_json, "transcribe": _cli_transcribe},
    "faster_whisper": {"check": _fw_check, "chat_json": _fw_chat_json, "transcribe": _fw_transcribe},
}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(prog="callguard.models")
    parser.add_argument("--probe", action="store_true", help="GET /models and require the configured model to be served")
    parser.add_argument("--require-self-hostable", action="store_true")
    args = parser.parse_args()
    for task in ("chat", "asr"):
        try:
            operation = probe if args.probe else check
            p = operation(task, require_self_hostable=args.require_self_hostable)
            where = p.get("command", "claude") if p.get("api") == "claude_cli" else _base_url(p)
            state = "live" if args.probe else "configured"
            print(f"{task}: {state} profile={p['name']} model={p['model']} via {where}")
        except ModelError as e:
            print(f"{task}: {e}", file=sys.stderr)
            sys.exit(1)

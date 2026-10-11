"""Local OpenAI-compatible ASR server for the pinned Swiss German Whisper checkpoint.

Backend tuning belongs here, not in the HTTP request contract. One WhisperModel stays resident for
the lifetime of this process; each request receives timestamped verbose_json segments on the source timeline.
"""
import argparse
import os
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from faster_whisper import WhisperModel

DEFAULT_MODEL = "jayr23/whisper-large-v3-turbo-swiss-german-ct2"
DEFAULT_REVISION = "49b9099c486ff980c9dc6f907678c2ff29253550"


def create_app(model_name=DEFAULT_MODEL, revision=DEFAULT_REVISION, served_model=None):
    app = FastAPI(title="CallGuard local ASR", docs_url=None, redoc_url=None)
    served_model = served_model or model_name
    # The model is deliberately constructed once at process startup, never per request.
    model = WhisperModel(model_name, device="cuda", compute_type="float16", download_root=os.environ.get("HF_HOME"),
                         revision=revision)

    @app.get("/v1/models")
    def models():
        return {"object": "list", "data": [{"id": served_model, "object": "model", "owned_by": "local"}]}

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(file: UploadFile = File(...), requested_model: str = Form(..., alias="model"), language: str = Form("de"),
                             response_format: str = Form("verbose_json"), temperature: str = Form("0"),
                             prompt: str | None = Form(None)):
        if requested_model != served_model:
            raise HTTPException(400, "requested model does not match the configured local model")
        if language != "de" or response_format != "verbose_json" or temperature not in ("0", "0.0"):
            raise HTTPException(400, "CallGuard ASR contract requires language=de, verbose_json and temperature=0")
        suffix = Path(file.filename or "audio.wav").suffix or ".wav"
        with tempfile.NamedTemporaryFile(suffix=suffix) as local:
            local.write(await file.read())
            local.flush()
            segments, _ = model.transcribe(local.name, language="de", task="transcribe", beam_size=5,
                                            temperature=0.0, condition_on_previous_text=True, vad_filter=False)
            rows = [{"id": f"s{i:03d}", "start": part.start, "end": part.end, "text": part.text.strip(),
                     "avg_logprob": part.avg_logprob, "no_speech_prob": part.no_speech_prob,
                     "compression_ratio": part.compression_ratio}
                    for i, part in enumerate(segments, 1)]
        return {"text": " ".join(row["text"] for row in rows), "segments": rows}

    return app


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8002)
    parser.add_argument("--model", default=os.environ.get("WHISPER_MODEL", DEFAULT_MODEL))
    parser.add_argument("--revision", default=os.environ.get("WHISPER_MODEL_REVISION", DEFAULT_REVISION))
    parser.add_argument("--served-model", default=os.environ.get("WHISPER_SERVED_MODEL"))
    args = parser.parse_args()
    import uvicorn
    uvicorn.run(create_app(args.model, args.revision, args.served_model), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()

"""Audio evidence: clips cut from the original WAV with the standard library."""
import io
import wave
from pathlib import Path

CONTEXT_SECONDS = 10.0


def duration(wav_path):
    with wave.open(str(wav_path), "rb") as w:
        return w.getnframes() / w.getframerate()


def clip(wav_path, start, end, context=CONTEXT_SECONDS):
    """WAV bytes from start-context to end+context of the original recording, clamped to the file."""
    with wave.open(str(Path(wav_path)), "rb") as src:
        rate, total = src.getframerate(), src.getnframes()
        first = max(0, int((start - context) * rate))
        last = min(total, int((end + context) * rate))
        src.setpos(first)
        frames = src.readframes(max(0, last - first))
        params = src.getparams()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames)
    return buf.getvalue(), first / rate, last / rate

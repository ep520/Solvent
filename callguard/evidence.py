"""Audio evidence: clips cut from the original WAV with the standard library."""
import io
import wave
from pathlib import Path

CONTEXT_SECONDS = 10.0


def duration(wav_path):
    with wave.open(str(wav_path), "rb") as w:
        return w.getnframes() / w.getframerate()


def window(start, end, recording_duration, context=CONTEXT_SECONDS):
    """Canonical evidence window, clamped to the original recording bounds."""
    if not all(isinstance(value, (int, float)) for value in (start, end, recording_duration)):
        raise ValueError("evidence timestamps and recording duration must be numeric")
    if start < 0 or end < start or recording_duration < 0:
        raise ValueError("invalid evidence timestamp range")
    return max(0.0, start - context), min(float(recording_duration), end + context)


def clip_window(wav_path, start, end):
    """Return an exact precomputed clip window from the original WAV."""
    with wave.open(str(Path(wav_path)), "rb") as src:
        rate, total = src.getframerate(), src.getnframes()
        recording_duration = total / rate
        if start < 0 or end < start or end > recording_duration:
            raise ValueError("clip window is outside the recording")
        first = int(start * rate)
        last = int(end * rate)
        src.setpos(first)
        frames = src.readframes(max(0, last - first))
        params = src.getparams()
    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setparams(params)
        dst.writeframes(frames)
    return buf.getvalue(), first / rate, last / rate


def clip(wav_path, start, end, context=CONTEXT_SECONDS):
    """WAV bytes from start-context to end+context of the original recording, clamped to the file."""
    clip_start, clip_end = window(start, end, duration(wav_path), context)
    return clip_window(wav_path, clip_start, clip_end)

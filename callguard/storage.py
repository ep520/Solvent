"""Small durable-write helpers for pipeline caches and run artifacts."""
import json
import os
import tempfile
from pathlib import Path


def atomic_write_text(path, text, encoding="utf-8"):
    """Replace *path* only after a complete, flushed sibling temporary file exists."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding=encoding) as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        # replace() has moved the file on success. On failure remove only our sibling temporary file.
        if temporary.exists():
            temporary.unlink()


def atomic_write_json(path, value, *, indent=1):
    """Serialize JSON and atomically publish it at *path*."""
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=indent) + "\n")

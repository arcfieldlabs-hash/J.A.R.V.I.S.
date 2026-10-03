"""A standalone child-process entry point; never imported to discover tools."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from pathlib import Path


MAX_REQUEST_BYTES = 40_960
MAX_SOURCE_BYTES = 120_000
MAX_RESULT_BYTES = 65_536


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", required=True)
    options = parser.parse_args()
    protocol = os.dup(sys.stdout.fileno())
    # Plugin prints (including os.write(1, ...)) go to the separate log pipe.
    sys.stdout.flush()
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    result = None
    try:
        raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
        if len(raw) > MAX_REQUEST_BYTES:
            raise ValueError("Extension execution request is too large.")
        request = json.loads(raw)
        if not isinstance(request, dict) or not isinstance(request.get("args"), dict):
            raise ValueError("Extension arguments must be an object.")
        context = request.get("context")
        if not isinstance(context, dict) or set(context) != {"workspace", "data_dir"}:
            raise ValueError("Extension context is invalid.")
        source_path = Path(options.file)
        if not source_path.is_absolute() or source_path.is_symlink():
            raise ValueError("Extension source must be a regular absolute file.")
        descriptor = os.open(source_path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_BYTES:
                raise ValueError("Extension source is not a regular file within its size limit.")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                source = stream.read(MAX_SOURCE_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(source) > MAX_SOURCE_BYTES or hashlib.sha256(source).hexdigest() != request.get("code_sha256"):
            raise ValueError("Extension source does not match the approved digest.")
        namespace = {"__name__": "jarvis_extension", "__file__": str(source_path)}
        exec(compile(source, str(source_path), "exec"), namespace)
        entry = namespace.get("run")
        if not callable(entry):
            raise ValueError("Extension must define run(args, context).")
        value = entry(request["args"], context)
        result = {"ok": True, "result": value}
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(encoded) > MAX_RESULT_BYTES:
            raise ValueError(f"Extension result exceeds its {MAX_RESULT_BYTES}-byte limit.")
    except BaseException as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {str(exc)[:2000]}"}
        encoded = json.dumps(result, ensure_ascii=False, allow_nan=False).encode("utf-8")
    try:
        sys.stdout.flush()
        with os.fdopen(protocol, "wb", closefd=True) as stream:
            stream.write(encoded)
            stream.flush()
    except (OSError, ValueError):
        return 1
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

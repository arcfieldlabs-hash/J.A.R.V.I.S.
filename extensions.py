"""Reviewed Python tools that run in a separate, time-limited process.

This is process isolation, not an operating-system sandbox. Approved extension
code receives the same OS privileges as Jarvis and can import Python modules.
Discovery only reads metadata and source bytes; it never imports an extension.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import re
import selectors
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable


MAX_EXTENSIONS = 12
MAX_SOURCE_BYTES = 120_000
MAX_ARGS_BYTES = 32_768
MAX_METADATA_BYTES = 32_768
MAX_RESULT_BYTES = 65_536
MAX_LOG_BYTES = 16_384
RUN_TIMEOUT = 15.0
NAME_PATTERN = re.compile(r"ext_[a-z][a-z0-9_]{0,39}\Z")
DIGEST_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


def _json_bytes(value: Any, maximum: int, label: str) -> bytes:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError(f"{label} must be finite JSON values.") from exc
    if len(encoded) > maximum:
        raise ValueError(f"{label} exceeds its {maximum}-byte limit.")
    return encoded


def _valid_name(name: Any) -> str:
    if not isinstance(name, str) or NAME_PATTERN.fullmatch(name) is None:
        raise ValueError("Extension names must match ext_[a-z][a-z0-9_]{0,39}.")
    return name


def _check_schema(schema: dict[str, Any], depth: int = 0) -> None:
    if depth > 12:
        raise ValueError("Extension parameter schema is nested too deeply.")
    if not isinstance(schema, dict):
        raise ValueError("Every extension parameter schema must be an object.")
    kind = schema.get("type")
    allowed = {"object", "array", "string", "integer", "number", "boolean", "null"}
    if kind is not None and (not isinstance(kind, str) or kind not in allowed):
        raise ValueError("Extension schemas support a single JSON type per field.")
    if "$ref" in schema:
        raise ValueError("Extension parameter schemas cannot contain references.")
    properties = schema.get("properties", {})
    if not isinstance(properties, dict) or any(not isinstance(key, str) for key in properties):
        raise ValueError("Extension schema properties must be a JSON object.")
    for child in properties.values():
        _check_schema(child, depth + 1)
    required = schema.get("required", [])
    if (not isinstance(required, list) or any(not isinstance(key, str) for key in required)
            or len(set(required)) != len(required) or any(key not in properties for key in required)):
        raise ValueError("Extension schema required fields must name unique properties.")
    if "items" in schema:
        _check_schema(schema["items"], depth + 1)
    additional = schema.get("additionalProperties", True)
    if not isinstance(additional, (bool, dict)):
        raise ValueError("Extension additionalProperties must be a boolean or schema.")
    if isinstance(additional, dict):
        _check_schema(additional, depth + 1)
    if "enum" in schema and (not isinstance(schema["enum"], list) or not schema["enum"]):
        raise ValueError("Extension enum must be a nonempty list.")
    for key in ("minLength", "maxLength", "minItems", "maxItems"):
        if key in schema and (type(schema[key]) is not int or schema[key] < 0):
            raise ValueError(f"Extension schema {key} must be a nonnegative integer.")
    for key in ("minimum", "maximum"):
        if key in schema and (type(schema[key]) not in (int, float)):
            raise ValueError(f"Extension schema {key} must be numeric.")
    # These compositional schemas need a full JSON-schema engine to interpret.
    if any(key in schema for key in ("anyOf", "allOf", "oneOf", "not", "if", "then", "else")):
        raise ValueError("Extension schemas support types, properties, arrays, bounds, and enum.")


def validate_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Validate and copy an extension's public tool declaration."""
    if not isinstance(metadata, dict):
        raise ValueError("Extension metadata must be a JSON object.")
    if set(metadata) - {"name", "description", "parameters", "code_sha256"}:
        raise ValueError("Extension metadata contains unknown fields.")
    name = _valid_name(metadata.get("name"))
    description = metadata.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > 1000:
        raise ValueError("Extension description must contain 1 to 1000 characters.")
    parameters = metadata.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("type") != "object":
        raise ValueError("Extension parameters must be an object JSON schema.")
    _check_schema(parameters)
    result = {"name": name, "description": description.strip(), "parameters": parameters}
    if "code_sha256" in metadata:
        digest = metadata["code_sha256"]
        if not isinstance(digest, str) or DIGEST_PATTERN.fullmatch(digest) is None:
            raise ValueError("Extension code_sha256 must be a SHA-256 hexadecimal digest.")
        result["code_sha256"] = digest
    return json.loads(_json_bytes(result, MAX_METADATA_BYTES, "Extension metadata"))


def validate_code(source: str) -> None:
    """Compile without executing, and require a plain run(args, context) function."""
    if not isinstance(source, str) or not source.strip():
        raise ValueError("Extension source must be nonempty Python text.")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ValueError(f"Extension source exceeds its {MAX_SOURCE_BYTES}-byte limit.")
    try:
        parsed = ast.parse(source, filename="<jarvis-extension>")
        compile(parsed, "<jarvis-extension>", "exec")
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise ValueError(f"Extension source is not valid Python: {exc}") from exc
    functions = [node for node in parsed.body if isinstance(node, ast.FunctionDef) and node.name == "run"]
    if len(functions) != 1:
        raise ValueError("Extension source must define one top-level run(args, context) function.")
    function = functions[0]
    arguments = function.args
    if (arguments.posonlyargs or [arg.arg for arg in arguments.args] != ["args", "context"]
            or arguments.vararg or arguments.kwarg or arguments.kwonlyargs
            or arguments.defaults or arguments.kw_defaults or function.decorator_list):
        raise ValueError("Extension entry point must be exactly def run(args, context), without decorators.")


def _validate_args(args: Any, schema: dict[str, Any], path: str = "args", depth: int = 0) -> None:
    if depth > 20:
        raise ValueError("Extension arguments are nested too deeply.")
    kind = schema.get("type")
    matches = {
        "object": isinstance(args, dict), "array": isinstance(args, list),
        "string": isinstance(args, str), "integer": type(args) is int,
        "number": type(args) in (int, float), "boolean": type(args) is bool,
        "null": args is None,
    }
    if kind and not matches[kind]:
        raise ValueError(f"{path} must be {kind}.")
    if "enum" in schema and args not in schema["enum"]:
        raise ValueError(f"{path} must match one of the declared enum values.")
    if isinstance(args, dict):
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in args]
        if missing:
            raise ValueError(f"{path} is missing required field: {missing[0]}.")
        for key, value in args.items():
            if key in properties:
                _validate_args(value, properties[key], f"{path}.{key}", depth + 1)
            else:
                additional = schema.get("additionalProperties", True)
                if additional is False:
                    raise ValueError(f"{path} contains an undeclared field: {key}.")
                if isinstance(additional, dict):
                    _validate_args(value, additional, f"{path}.{key}", depth + 1)
    if isinstance(args, list):
        if len(args) < schema.get("minItems", 0) or len(args) > schema.get("maxItems", MAX_ARGS_BYTES):
            raise ValueError(f"{path} has an invalid number of items.")
        if "items" in schema:
            for index, value in enumerate(args):
                _validate_args(value, schema["items"], f"{path}[{index}]", depth + 1)
    if isinstance(args, str):
        if len(args) < schema.get("minLength", 0) or len(args) > schema.get("maxLength", MAX_ARGS_BYTES):
            raise ValueError(f"{path} has an invalid string length.")
    if type(args) in (int, float):
        if args < schema.get("minimum", float("-inf")) or args > schema.get("maximum", float("inf")):
            raise ValueError(f"{path} is outside its declared numeric bounds.")


def _read_regular(path: Path, maximum: int) -> bytes:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > maximum:
            raise ValueError("Extension files must be regular files within their size limits.")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            data = stream.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Extension file exceeds its size limit.")
        return data
    finally:
        os.close(descriptor)


def _kill_process_group(process: subprocess.Popen) -> None:
    try:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL)
        else:  # pragma: no cover - Jarvis targets macOS.
            process.kill()
    except ProcessLookupError:
        pass


class ExtensionRegistry:
    def __init__(self, directory: Path, workspace: Path, data_dir: Path) -> None:
        raw_directory = Path(directory).expanduser().absolute()
        if raw_directory.is_symlink():
            raise ValueError("The extension directory cannot be a symbolic link.")
        raw_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.directory = raw_directory.resolve()
        self.directory.chmod(0o700)
        self.workspace = Path(workspace).expanduser().resolve()
        self.data_dir = Path(data_dir).expanduser().resolve()
        self._lock = threading.RLock()

    def _check_directory(self) -> None:
        if self.directory.is_symlink() or not self.directory.is_dir():
            raise ValueError("The private extension directory is unavailable or was replaced.")

    def _path(self, name: str, suffix: str) -> Path:
        self._check_directory()
        return self.directory / (_valid_name(name) + suffix)

    def _read(self, name: str) -> dict[str, Any]:
        manifest_path = self._path(name, ".json")
        source_path = self._path(name, ".py")
        if manifest_path.is_symlink() or source_path.is_symlink():
            raise ValueError("Extension files cannot be symbolic links.")
        metadata = validate_metadata(json.loads(_read_regular(manifest_path, MAX_METADATA_BYTES)))
        if metadata["name"] != name or "code_sha256" not in metadata:
            raise ValueError("Extension manifest does not match its registered name and digest.")
        source_bytes = _read_regular(source_path, MAX_SOURCE_BYTES)
        if hashlib.sha256(source_bytes).hexdigest() != metadata["code_sha256"]:
            raise ValueError("Extension code changed since approval; review and install it again.")
        source = source_bytes.decode("utf-8")
        validate_code(source)
        return {"metadata": metadata, "source": source}

    def read(self, name: str) -> dict[str, Any]:
        with self._lock:
            return self._read(_valid_name(name))

    def snapshot(self, name: str) -> dict[str, Any] | None:
        with self._lock:
            name = _valid_name(name)
            if not self._path(name, ".json").exists() and not self._path(name, ".py").exists():
                return None
            return self._read(name)

    def tools(self) -> list[dict[str, Any]]:
        with self._lock:
            self._check_directory()
            declarations = []
            for path in sorted(self.directory.glob("ext_*.json")):
                if len(declarations) >= MAX_EXTENSIONS:
                    break
                try:
                    metadata = self._read(path.stem)["metadata"]
                except (OSError, ValueError, UnicodeError):
                    continue
                declarations.append({key: metadata[key] for key in ("name", "description", "parameters")})
            return declarations

    catalog = tools

    def _write(self, path: Path, content: bytes) -> None:
        if path.is_symlink():
            raise ValueError("Extension files cannot be symbolic links.")
        descriptor, temporary_path = tempfile.mkstemp(prefix=".install-", dir=self.directory)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary_path, path)
        finally:
            try:
                os.unlink(temporary_path)
            except FileNotFoundError:
                pass

    def install(self, metadata: dict[str, Any], source: str) -> dict[str, Any]:
        """Persist an already approved declaration; this method does not ask approval."""
        normalized = validate_metadata(metadata)
        validate_code(source)
        encoded = source.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        if "code_sha256" in normalized and normalized["code_sha256"] != digest:
            raise ValueError("The supplied extension digest does not match its source.")
        normalized["code_sha256"] = digest
        with self._lock:
            name = normalized["name"]
            if not self._path(name, ".json").exists() and len(self.tools()) >= MAX_EXTENSIONS:
                raise ValueError(f"At most {MAX_EXTENSIONS} extension tools may be installed.")
            self._write(self._path(name, ".py"), encoded)
            self._write(self._path(name, ".json"), _json_bytes(normalized, MAX_METADATA_BYTES, "Extension metadata"))
        return {"ok": True, "name": name, "code_sha256": digest}

    def remove(self, name: str) -> dict[str, Any]:
        name = _valid_name(name)
        with self._lock:
            for suffix in (".json", ".py"):
                path = self._path(name, suffix)
                if path.is_symlink():
                    raise ValueError("Extension files cannot be symbolic links.")
                path.unlink(missing_ok=True)
        return {"ok": True, "name": name}

    def restore(self, name: str, snapshot: dict[str, Any] | None) -> dict[str, Any]:
        name = _valid_name(name)
        if snapshot is None:
            return self.remove(name)
        if not isinstance(snapshot, dict) or snapshot.get("metadata", {}).get("name") != name:
            raise ValueError("Extension snapshot does not match the requested tool.")
        return self.install(snapshot["metadata"], snapshot["source"])

    def _approval_details(self, metadata: dict[str, Any], source: str, args: dict[str, Any]) -> dict[str, Any]:
        details = {
            "name": metadata["name"], "description": metadata["description"],
            "parameters": metadata["parameters"], "code": source, "args": args,
            "code_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest(),
            "context": {"workspace": str(self.workspace), "data_dir": str(self.data_dir)},
        }
        return json.loads(json.dumps(details, ensure_ascii=False, allow_nan=False))

    def _prepare(self, metadata: dict[str, Any], source: str, args: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        normalized = validate_metadata(metadata)
        validate_code(source)
        if not isinstance(args, dict):
            raise ValueError("Extension arguments must be a JSON object.")
        # Copy before approval so a caller cannot mutate the approved arguments.
        copied_args = json.loads(_json_bytes(args, MAX_ARGS_BYTES, "Extension arguments"))
        _validate_args(copied_args, normalized["parameters"])
        digest = hashlib.sha256(source.encode("utf-8")).hexdigest()
        if "code_sha256" in normalized and normalized["code_sha256"] != digest:
            raise ValueError("The extension digest does not match its approved source.")
        return normalized, copied_args

    def invoke(self, name: str, args: dict[str, Any], permission: Callable[[str, dict], bool]) -> dict[str, Any]:
        try:
            approved = self.read(name)
            metadata, copied_args = self._prepare(approved["metadata"], approved["source"], args)
            details = self._approval_details(metadata, approved["source"], copied_args)
            if not callable(permission) or not permission(f"Run extension {name}", details):
                return {"ok": False, "content": "Extension execution was not approved."}
            # Compare both declaration and code after the permission prompt.
            if self.read(name) != approved:
                return {"ok": False, "content": "The extension changed during approval. Review it again."}
            return self._execute(metadata, approved["source"], copied_args)
        except (OSError, ValueError, UnicodeError) as exc:
            return {"ok": False, "content": str(exc)}

    def run_source(self, metadata: dict[str, Any], source: str, args: dict[str, Any],
                   permission: Callable[[str, dict], bool]) -> dict[str, Any]:
        """Test staged source in a child process, without installing or exposing it."""
        try:
            normalized, copied_args = self._prepare(metadata, source, args)
            details = self._approval_details(normalized, source, copied_args)
            if not callable(permission) or not permission(f"Test extension {normalized['name']}", details):
                return {"ok": False, "content": "Extension test execution was not approved."}
            return self._execute(normalized, source, copied_args)
        except (OSError, ValueError, UnicodeError) as exc:
            return {"ok": False, "content": str(exc)}

    def _execute(self, metadata: dict[str, Any], source: str, args: dict[str, Any]) -> dict[str, Any]:
        runner = Path(__file__).with_name("extension_runner.py").resolve()
        payload = _json_bytes({"args": args, "context": {"workspace": str(self.workspace),
                              "data_dir": str(self.data_dir)},
                              "code_sha256": hashlib.sha256(source.encode("utf-8")).hexdigest()},
                             MAX_ARGS_BYTES + 8192, "Extension execution request")
        with tempfile.TemporaryDirectory(prefix="jarvis-extension-") as temporary:
            source_path = Path(temporary) / (metadata["name"] + ".py")
            source_path.write_text(source, encoding="utf-8")
            source_path.chmod(0o600)
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as logs:
                process = subprocess.Popen(
                    [sys.executable, "-I", str(runner), "--file", str(source_path)],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    cwd=self.workspace, start_new_session=(os.name == "posix"),
                )
                deadline = time.monotonic() + RUN_TIMEOUT
                counts = {"stdout": 0, "stderr": 0}
                issue = ""
                try:
                    try:
                        process.stdin.write(payload)
                        process.stdin.close()
                    except BrokenPipeError:
                        pass
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stdout, selectors.EVENT_READ, ("stdout", output, MAX_RESULT_BYTES))
                        selector.register(process.stderr, selectors.EVENT_READ, ("stderr", logs, MAX_LOG_BYTES))
                        while selector.get_map():
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                issue = f"Extension timed out after {RUN_TIMEOUT:g} seconds; its processes were stopped."
                                break
                            for key, _ in selector.select(min(remaining, 0.1)):
                                chunk = os.read(key.fileobj.fileno(), 8192)
                                if not chunk:
                                    selector.unregister(key.fileobj)
                                    continue
                                label, destination, maximum = key.data
                                allowed = maximum - counts[label]
                                destination.write(chunk[:max(0, allowed)])
                                counts[label] += len(chunk)
                                if counts[label] > maximum:
                                    issue = f"Extension {label} exceeded its {maximum}-byte limit; its processes were stopped."
                                    break
                            if issue:
                                break
                    if not issue:
                        try:
                            process.wait(timeout=max(0.001, deadline - time.monotonic()))
                        except subprocess.TimeoutExpired:
                            issue = f"Extension timed out after {RUN_TIMEOUT:g} seconds; its processes were stopped."
                finally:
                    # Stop children that remain in the runner's process group.
                    _kill_process_group(process)
                    process.wait(timeout=2)
                    process.stdout.close()
                    process.stderr.close()
                    if not process.stdin.closed:
                        process.stdin.close()
                logs.seek(0)
                log_text = logs.read(MAX_LOG_BYTES).decode("utf-8", errors="replace")
                if issue:
                    return {"ok": False, "content": issue, "logs": log_text}
                output.seek(0)
                try:
                    answer = json.loads(output.read(MAX_RESULT_BYTES))
                except (ValueError, UnicodeError):
                    return {"ok": False, "content": "The extension runner did not return valid JSON.", "logs": log_text}
                if not isinstance(answer, dict) or type(answer.get("ok")) is not bool:
                    return {"ok": False, "content": "The extension runner returned an invalid result.", "logs": log_text}
                if not answer["ok"]:
                    return {"ok": False, "content": str(answer.get("error", "Extension execution failed.")), "logs": log_text}
                result = answer.get("result")
                content = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, allow_nan=False)
                return {"ok": True, "content": content, "logs": log_text}

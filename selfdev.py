"""Reviewable, approved changes to Jarvis's source and extension tools.

Staging and inspection never execute proposed code. Tests execute as the user's
account after approval; a temporary source copy is not an operating-system
sandbox. Installing a tested proposal requires a separate approval.
"""
from __future__ import annotations

import ast
import difflib
import hashlib
import json
import os
import signal
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .extensions import validate_code, validate_metadata


MAX_PROPOSAL_BYTES = 256 * 1024
MAX_RECORD_BYTES = 1024 * 1024
MAX_INSPECT_CHARS = 12_000
MAX_TEST_OUTPUT = 12_000
MAX_COPY_BYTES = 20 * 1024 * 1024
MAX_COPY_FILES = 2000
TEST_TIMEOUT = 120
BLOCKED_PARTS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".jarvis",
    ".wrangler", "build", "dist", ".pytest_cache", ".mypy_cache",
})
CODE_SUFFIXES = frozenset({".py", ".js", ".html", ".css", ".toml", ".json", ".yml", ".yaml"})
EDIT_SUFFIXES = CODE_SUFFIXES | {".md", ".txt"}


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


def _hash(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


class SelfDevelopment:
    def __init__(
        self, source_root: Path, data_dir: Path, workspace: Path, registry: Any,
        permission_handler: Callable[[str, dict[str, Any]], bool] | None = None,
    ) -> None:
        self.source_root = Path(source_root).expanduser().resolve()
        self.data_dir = Path(data_dir).expanduser().resolve()
        self.workspace = Path(workspace).expanduser().resolve()
        self.registry = registry
        self.permission_handler = permission_handler
        self.directory = self.data_dir / "selfdev"
        self.proposals_dir = self.directory / "proposals"
        self._lock = threading.RLock()
        self._operation_lock = threading.Lock()
        self._make_private_directory(self.directory)
        self._make_private_directory(self.proposals_dir)
        self._enabled = False
        settings = self.directory / "settings.json"
        if settings.exists():
            value = self._read_json(settings)
            self._enabled = value.get("enabled") is True

    @staticmethod
    def _make_private_directory(path: Path) -> None:
        if path.is_symlink():
            raise ValueError("Self-development storage cannot be a symbolic link.")
        path.mkdir(parents=True, exist_ok=True)
        if not path.is_dir():
            raise ValueError("Self-development storage must be a directory.")
        os.chmod(path, 0o700)

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    def set_enabled(self, enabled: bool) -> dict[str, Any]:
        if not isinstance(enabled, bool):
            raise ValueError("Self-development must be enabled or disabled with a boolean.")
        with self._lock:
            self._write_json(self.directory / "settings.json", {"enabled": enabled})
            self._enabled = enabled
        return {"ok": True, "enabled": enabled}

    def _require_enabled(self) -> None:
        if not self.enabled:
            raise ValueError("Self-development is off. Enable it in Permissions before staging, testing, or applying a change.")

    def _read_json(self, path: Path) -> dict[str, Any]:
        self._check_private_storage()
        if path.is_symlink():
            raise ValueError("Self-development records cannot be symbolic links.")
        with path.open("rb") as handle:
            raw = handle.read(MAX_RECORD_BYTES + 1)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("Self-development record is too large.")
        value = json.loads(raw)
        if not isinstance(value, dict):
            raise ValueError("Invalid self-development record.")
        return value

    def _write_json(self, path: Path, value: dict[str, Any]) -> None:
        self._check_private_storage()
        raw = _json_bytes(value)
        if len(raw) > MAX_RECORD_BYTES:
            raise ValueError("Self-development record is too large.")
        self._atomic_write(path, raw, private=True)

    def _check_private_storage(self) -> None:
        if any(path.is_symlink() or not path.is_dir() for path in (self.data_dir, self.directory, self.proposals_dir)):
            raise ValueError("Jarvis's private self-development directories were replaced or are unavailable.")

    @staticmethod
    def _atomic_write(path: Path, raw: bytes, *, private: bool = False) -> None:
        if path.is_symlink():
            raise ValueError("A symbolic link cannot be overwritten.")
        path.parent.mkdir(parents=True, exist_ok=True)
        mode = 0o600 if private else (path.stat().st_mode & 0o777 if path.exists() else 0o644)
        descriptor, temporary = tempfile.mkstemp(prefix=".jarvis-change-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(raw)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _source_path(self, value: str, *, directory: bool = False) -> Path:
        if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
            raise ValueError("Use a relative path inside Jarvis's source directory.")
        relative = PurePosixPath(value)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Source paths cannot leave Jarvis's source directory.")
        if any(part in BLOCKED_PARTS or part.startswith(".env") or part.endswith(".egg-info") for part in relative.parts):
            raise ValueError("This path contains private data, build files, or repository metadata.")
        path = self.source_root / Path(*relative.parts)
        current = self.source_root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError("Source paths cannot follow symbolic links.")
        if not _within(path.resolve(), self.source_root):
            raise ValueError("Source paths cannot leave Jarvis's source directory.")
        if _within(path.resolve(), self.data_dir):
            raise ValueError("Jarvis's private data cannot be changed through source proposals.")
        extension_root = getattr(self.registry, "directory", None)
        if extension_root and _within(path.resolve(), Path(extension_root).resolve()):
            raise ValueError("Use an extension proposal to change installed extension tools.")
        if not directory and (not relative.parts or path.suffix.lower() not in EDIT_SUFFIXES):
            raise ValueError("Proposals may change Python, web, configuration, documentation, and text source files.")
        return path

    def protect_path(self, path: Path | str) -> bool:
        """Keep generic file writes away from source code and authoritative data."""
        resolved = Path(path).expanduser().resolve()
        if _within(resolved, self.directory):
            return True
        extension_root = getattr(self.registry, "directory", None)
        if extension_root and _within(resolved, Path(extension_root).resolve()):
            return True
        if _within(resolved, self.source_root):
            relative = resolved.relative_to(self.source_root)
            return (
                resolved.suffix.lower() in CODE_SUFFIXES
                or ".git" in relative.parts
                or any(part.startswith(".env") for part in relative.parts)
            )
        return False

    def inspect(self, path: str = ".", line: int = 1) -> dict[str, Any]:
        target = self._source_path(path, directory=True)
        if target.is_dir():
            entries = []
            for child in sorted(target.iterdir(), key=lambda item: item.name):
                try:
                    self._source_path(child.relative_to(self.source_root).as_posix(), directory=True)
                except ValueError:
                    continue
                entries.append({"path": child.relative_to(self.source_root).as_posix(), "directory": child.is_dir()})
            return {"ok": True, "source_root": str(self.source_root), "path": path,
                    "files": entries[:250], "truncated": len(entries) > 250, "enabled": self.enabled}
        if isinstance(line, bool) or not isinstance(line, int) or line < 1 or line > 1_000_000:
            raise ValueError("The starting line must be a positive integer.")
        with target.open("rb") as handle:
            raw = handle.read(MAX_PROPOSAL_BYTES + 1)
        text = raw[:MAX_PROPOSAL_BYTES].decode("utf-8")
        remaining = "".join(text.splitlines(keepends=True)[line - 1:])
        return {"ok": True, "source_root": str(self.source_root), "path": path, "line": line,
                "content": remaining[:MAX_INSPECT_CHARS], "truncated": len(remaining) > MAX_INSPECT_CHARS
                or len(raw) > MAX_PROPOSAL_BYTES, "enabled": self.enabled}

    def _source_files(self) -> list[Path]:
        paths: list[Path]
        if (self.source_root / ".git").exists():
            completed = subprocess.run(
                ["git", "-C", str(self.source_root), "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                capture_output=True, timeout=5,
            )
            if completed.returncode != 0 or len(completed.stdout) > MAX_RECORD_BYTES:
                raise ValueError("Could not enumerate Jarvis's source checkout.")
            paths = [self.source_root / os.fsdecode(name) for name in completed.stdout.split(b"\0") if name]
        else:
            paths = []
            for parent, directories, files in os.walk(self.source_root, followlinks=False):
                directories[:] = [name for name in directories if name not in BLOCKED_PARTS
                                  and not name.endswith(".egg-info") and not (Path(parent) / name).is_symlink()]
                paths.extend(Path(parent) / name for name in files)
                if len(paths) > MAX_COPY_FILES * 2:
                    raise ValueError("Jarvis's source tree has too many files for a bounded test copy.")
        accepted = []
        for path in sorted(set(paths)):
            try:
                self._source_path(path.relative_to(self.source_root).as_posix(), directory=True)
            except ValueError:
                continue
            if path.is_file() and path.suffix.lower() in EDIT_SUFFIXES:
                accepted.append(path)
        if len(accepted) > MAX_COPY_FILES:
            raise ValueError("Jarvis's source tree has too many files for a bounded test copy.")
        return accepted

    def _source_snapshot(self) -> dict[str, bytes]:
        result = {}
        total = 0
        for path in self._source_files():
            with path.open("rb") as handle:
                raw = handle.read(MAX_COPY_BYTES + 1)
            total += len(raw)
            if total > MAX_COPY_BYTES:
                raise ValueError("Jarvis's source tree exceeds the bounded test-copy size.")
            result[path.relative_to(self.source_root).as_posix()] = raw
        return result

    @staticmethod
    def _source_digest(snapshot: dict[str, bytes]) -> str:
        return _hash(_json_bytes({name: _hash(raw) for name, raw in snapshot.items()}))

    def _core_editable(self) -> None:
        if not (self.source_root / "pyproject.toml").is_file() or not (
            (self.source_root / ".git").exists() or (self.source_root / "__init__.py").is_file()
        ):
            raise ValueError("Core updates require Jarvis's editable source checkout. Use extension tools in an installed package.")

    def _patch_files(self, patch: str) -> list[dict[str, str]]:
        """Apply a checked text patch in a copy; never alter the running source."""
        if not isinstance(patch, str) or not patch.strip() or len(patch.encode("utf-8")) > MAX_PROPOSAL_BYTES:
            raise ValueError("A core patch must be a nonempty unified diff of at most 256 KiB.")
        if not patch.endswith("\n"):
            patch += "\n"
        old = None
        names = []
        for line in patch.splitlines():
            if line.startswith(("GIT binary patch", "Binary files ", "rename from ", "rename to ",
                                "copy from ", "copy to ", "deleted file mode ", "new file mode ",
                                "old mode ", "new mode ", "similarity index ")):
                raise ValueError("Core patches support text changes to existing files, without renames, deletions, or mode changes.")
            if line.startswith("diff --git "):
                parts = shlex.split(line)
                if len(parts) != 4 or not parts[2].startswith("a/") or not parts[3].startswith("b/") or parts[2][2:] != parts[3][2:]:
                    raise ValueError("Every patch file must use matching a/path and b/path names.")
                self._source_path(parts[2][2:])
            elif line.startswith("--- "):
                if old is not None or not line[4:].startswith("a/"):
                    raise ValueError("Every patch file must use --- a/path and +++ b/path headers.")
                old = line[6:].split("\t", 1)[0]
                self._source_path(old)
            elif line.startswith("+++ "):
                new = line[6:].split("\t", 1)[0] if line[4:].startswith("b/") else None
                if old is None or old != new or new in names:
                    raise ValueError("Patch headers must name the same existing source file once.")
                path = self._source_path(new)
                if not path.is_file():
                    raise ValueError("Core patches update existing files; use files/content to propose a new file.")
                names.append(new)
                old = None
            elif line and not line.startswith(("index ", "@@ ", " ", "+", "-", "\\")):
                raise ValueError("Use a plain unified diff without surrounding explanation or Markdown fences.")
        if old is not None or not 1 <= len(names) <= 8:
            raise ValueError("A core patch must change between one and eight existing files.")
        with tempfile.TemporaryDirectory(prefix="jarvis-patch-stage-") as temporary:
            root = Path(temporary)
            for name in names:
                path = self._source_path(name)
                with path.open("rb") as handle:
                    raw = handle.read(MAX_PROPOSAL_BYTES + 1)
                if len(raw) > MAX_PROPOSAL_BYTES:
                    raise ValueError("A patch target exceeds the complete review size limit.")
                destination = root / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
            for flags in (["--check"], []):
                try:
                    completed = subprocess.run(
                        ["git", "-C", str(root), "apply", *flags, "--whitespace=nowarn", "-p1", "-"],
                        input=patch.encode("utf-8"), capture_output=True, timeout=5,
                    )
                except FileNotFoundError as exc:
                    raise ValueError("Git is required to stage unified-diff source proposals.") from exc
                if completed.returncode:
                    error = completed.stderr.decode("utf-8", errors="replace")[:2000]
                    raise ValueError("The patch does not apply to the current source: " + error)
            return [{"path": name, "content": (root / name).read_text(encoding="utf-8")} for name in names]

    def propose(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._require_enabled()
        if not isinstance(payload, dict):
            raise ValueError("A proposal must be an object.")
        kind = payload.get("kind", "extension")
        title = payload.get("title", "Add a reviewed extension tool" if kind == "extension" else "Update Jarvis's source")
        if not isinstance(title, str) or not title.strip() or len(title) > 200:
            raise ValueError("Give the proposal a title of at most 200 characters.")
        proposal: dict[str, Any] = {"id": uuid.uuid4().hex, "kind": kind, "title": title.strip(), "created_at": time.time()}
        if kind == "extension":
            metadata = validate_metadata({key: payload.get(key) for key in ("name", "description", "parameters")})
            source = payload.get("code")
            validate_code(source)
            sample_args = payload.get("sample_args", {})
            if not isinstance(sample_args, dict) or len(_json_bytes(sample_args)) > 16_384:
                raise ValueError("Extension sample arguments must be a small JSON object.")
            before = self.registry.snapshot(metadata["name"])
            diff = "".join(difflib.unified_diff(
                (before["source"] if before else "").splitlines(keepends=True), source.splitlines(keepends=True),
                fromfile=f"extensions/{metadata['name']}.py (current)",
                tofile=f"extensions/{metadata['name']}.py (proposed)",
            ))
            previous_metadata = ({key: before["metadata"].get(key) for key in metadata} if before else {})
            diff += "\n" + "".join(difflib.unified_diff(
                json.dumps(previous_metadata, indent=2, ensure_ascii=False).splitlines(keepends=True),
                json.dumps(metadata, indent=2, ensure_ascii=False).splitlines(keepends=True),
                fromfile=f"extensions/{metadata['name']}.json (current)",
                tofile=f"extensions/{metadata['name']}.json (proposed)",
            ))
            proposal.update(metadata=metadata, code=source, sample_args=sample_args, before=before, diff=diff)
        elif kind == "core":
            self._core_editable()
            files = payload.get("files")
            if "patch" in payload:
                if files is not None:
                    raise ValueError("Use either patch or files/content for a core proposal.")
                files = self._patch_files(payload["patch"])
            if not isinstance(files, list) or not 1 <= len(files) <= 8:
                raise ValueError("A core proposal must contain between one and eight files.")
            staged = []
            seen = set()
            total = 0
            diffs = []
            for entry in files:
                if not isinstance(entry, dict) or not isinstance(entry.get("content"), str):
                    raise ValueError("Each proposed file needs a relative path and text content.")
                path = self._source_path(entry.get("path", ""))
                relative = path.relative_to(self.source_root).as_posix()
                if relative in seen:
                    raise ValueError("A file can appear only once in a proposal.")
                seen.add(relative)
                content = entry["content"]
                total += len(content.encode("utf-8"))
                if total > MAX_PROPOSAL_BYTES:
                    raise ValueError("Proposed file contents exceed 256 KiB. Split the change into smaller proposals.")
                if path.suffix.lower() == ".py":
                    ast.parse(content, filename=relative)
                elif path.suffix.lower() == ".json":
                    json.loads(content)
                if path.exists():
                    with path.open("rb") as handle:
                        raw = handle.read(MAX_PROPOSAL_BYTES + 1)
                    if len(raw) > MAX_PROPOSAL_BYTES:
                        raise ValueError("An existing file is too large for a complete reviewed change.")
                    before = raw.decode("utf-8")
                else:
                    before = None
                staged.append({"path": relative, "content": content, "before": before})
                diffs.append("".join(difflib.unified_diff(
                    (before or "").splitlines(keepends=True), content.splitlines(keepends=True),
                    fromfile=f"{relative} (current)", tofile=f"{relative} (proposed)",
                )))
            proposal.update(files=staged, diff="\n".join(diffs), base_source_digest=self._source_digest(self._source_snapshot()))
        else:
            raise ValueError("Proposal kind must be 'extension' or 'core'.")
        if len(proposal["diff"].encode("utf-8")) > MAX_PROPOSAL_BYTES:
            raise ValueError("The complete diff exceeds 256 KiB. Split the change into smaller proposals.")
        record = {"proposal": proposal, "digest": _hash(_json_bytes(proposal)), "status": "staged", "test_result": None}
        with self._lock:
            self._require_enabled()
            self._write_json(self.proposals_dir / f"{proposal['id']}.json", record)
        return self._review(record)

    def _load(self, identifier: str) -> dict[str, Any]:
        if not isinstance(identifier, str) or len(identifier) != 32 or any(char not in "0123456789abcdef" for char in identifier):
            raise ValueError("Use the proposal ID returned when the change was staged.")
        record = self._read_json(self.proposals_dir / f"{identifier}.json")
        proposal = record.get("proposal")
        if not isinstance(proposal, dict) or proposal.get("id") != identifier or record.get("digest") != _hash(_json_bytes(proposal)):
            raise ValueError("The staged proposal was changed. Create and review a fresh proposal.")
        return record

    @staticmethod
    def _review(record: dict[str, Any]) -> dict[str, Any]:
        result = {"ok": True, **record["proposal"], "digest": record["digest"],
                  "status": record["status"], "test_result": record.get("test_result")}
        result["execution_warning"] = "Tests and extension execution run as your user account. A temporary test copy is not an operating-system sandbox."
        return json.loads(json.dumps(result))

    def review(self, identifier: str) -> dict[str, Any]:
        with self._lock:
            return self._review(self._load(identifier))

    def list(self) -> list[dict[str, Any]]:
        self._check_private_storage()
        records = []
        for path in self.proposals_dir.glob("*.json"):
            try:
                record = self._load(path.stem)
                proposal = record["proposal"]
                records.append({key: proposal[key] for key in ("id", "kind", "title", "created_at")}
                               | {"digest": record["digest"], "status": record["status"], "test_result": record.get("test_result")})
            except (OSError, ValueError, KeyError):
                continue
        return sorted(records, key=lambda item: item["created_at"], reverse=True)[:30]

    def _save(self, record: dict[str, Any]) -> None:
        with self._lock:
            self._write_json(self.proposals_dir / f"{record['proposal']['id']}.json", record)

    def _approved(self, action: str, record: dict[str, Any]) -> None:
        if self.permission_handler is None or not self.permission_handler(action, self._review(record)):
            raise ValueError("User approval was not granted. No proposed code was executed or applied.")
        fresh = self._load(record["proposal"]["id"])
        if fresh["digest"] != record["digest"] or fresh["status"] != record["status"] or fresh.get("test_result") != record.get("test_result"):
            raise ValueError("The proposal changed while approval was pending. Review it again.")

    def _check_base(self, proposal: dict[str, Any]) -> None:
        if proposal["kind"] == "extension":
            if self.registry.snapshot(proposal["metadata"]["name"]) != proposal["before"]:
                raise ValueError("The installed extension changed since this proposal. Stage a fresh change.")
        else:
            self._core_editable()
            for entry in proposal["files"]:
                path = self._source_path(entry["path"])
                if path.exists():
                    with path.open("rb") as handle:
                        raw = handle.read(MAX_PROPOSAL_BYTES + 1)
                    if len(raw) > MAX_PROPOSAL_BYTES:
                        raise ValueError(f"{entry['path']} grew beyond its reviewed size limit.")
                    current = raw.decode("utf-8")
                else:
                    current = None
                if current != entry["before"]:
                    raise ValueError(f"{entry['path']} changed since this proposal. Stage a fresh change.")
            if self._source_digest(self._source_snapshot()) != proposal["base_source_digest"]:
                raise ValueError("Jarvis's source changed since this proposal. Stage a fresh change and test it again.")

    def test(self, identifier: str) -> dict[str, Any]:
        with self._operation_lock:
            self._require_enabled()
            record = self._load(identifier)
            if record["status"] not in {"staged", "tested", "test_failed"}:
                raise ValueError("Only unapplied proposals can be tested.")
            self._check_base(record["proposal"])
            self._approved("selfdev.test", record)
            self._require_enabled()
            self._check_base(record["proposal"])
            proposal = record["proposal"]
            if proposal["kind"] == "extension":
                tested = self.registry.run_source(proposal["metadata"], proposal["code"], proposal["sample_args"],
                                                  permission=lambda _action, _details: self.enabled)
                result = {"passed": tested.get("ok") is True, "output": str(tested.get("content", ""))[:MAX_TEST_OUTPUT]}
            else:
                result = self._test_core(proposal)
            result.update(digest=record["digest"], tested_at=time.time())
            record.update(status="tested" if result["passed"] else "test_failed", test_result=result)
            self._save(record)
            return {"ok": result["passed"], "id": identifier, "status": record["status"], "test_result": result}

    def _test_core(self, proposal: dict[str, Any]) -> dict[str, Any]:
        snapshot = self._source_snapshot()
        if self._source_digest(snapshot) != proposal["base_source_digest"]:
            raise ValueError("Jarvis's source changed before the test copy could be created.")
        with tempfile.TemporaryDirectory(prefix="jarvis-proposal-test-") as temporary:
            package = Path(temporary) / "jarvis"
            package.mkdir()
            for name, raw in snapshot.items():
                destination = package / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(raw)
            for entry in proposal["files"]:
                destination = package / entry["path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_text(entry["content"], encoding="utf-8")
            # Isolated mode ignores PYTHONPATH and omits the current directory.
            # Insert this explicit test-copy parent before an editable install's
            # finder can resolve the original Jarvis package.
            wrapper = (
                "import os,sys,unittest;"
                "package=sys.argv[1];parent=os.path.dirname(package);"
                "sys.path.insert(0,parent);os.chdir(package);"
                "suite=unittest.defaultTestLoader.discover(package,top_level_dir=parent);"
                "result=unittest.TextTestRunner(verbosity=1).run(suite);"
                "sys.exit(0 if result.wasSuccessful() and result.testsRun else 1)"
            )
            command = [sys.executable, "-I", "-c", wrapper, str(package)]
            return self._run_test_command(command, Path(temporary))

    def _run_test_command(self, command: list[str], cwd: Path) -> dict[str, Any]:
        with tempfile.TemporaryFile() as output:
            process = subprocess.Popen(command, cwd=cwd, stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
            limit_reason = None
            deadline = time.monotonic() + TEST_TIMEOUT
            while True:
                try:
                    process.wait(timeout=0.1)
                    break
                except subprocess.TimeoutExpired:
                    if not self.enabled:
                        limit_reason = "Self-development was switched off; the test process was stopped."
                    elif time.monotonic() >= deadline:
                        limit_reason = f"Tests exceeded the {TEST_TIMEOUT}-second limit."
                    elif os.fstat(output.fileno()).st_size > MAX_RECORD_BYTES:
                        limit_reason = "Test output exceeded the 1 MiB limit."
                    if limit_reason:
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait(timeout=5)
                        break
            output.seek(0)
            text = output.read(MAX_TEST_OUTPUT + 1).decode("utf-8", errors="replace")
            if len(text) > MAX_TEST_OUTPUT:
                text = text[:MAX_TEST_OUTPUT] + "\n...[test output truncated]"
            if limit_reason:
                text += "\n" + limit_reason
            no_tests = "Ran 0 tests" in text
            if no_tests:
                text += "\nA core proposal needs a test suite with at least one discovered test."
            return {"passed": process.returncode == 0 and not limit_reason and not no_tests,
                    "returncode": process.returncode, "output": text}

    def apply(self, identifier: str) -> dict[str, Any]:
        with self._operation_lock:
            self._require_enabled()
            record = self._load(identifier)
            tested = record.get("test_result")
            if record["status"] != "tested" or not tested or tested.get("passed") is not True or tested.get("digest") != record["digest"]:
                raise ValueError("Run and pass the approved tests for this exact proposal before applying it.")
            proposal = record["proposal"]
            self._check_base(proposal)
            self._approved("selfdev.apply", record)
            self._require_enabled()
            self._check_base(proposal)
            if proposal["kind"] == "extension":
                installed = self.registry.install(proposal["metadata"], proposal["code"])
                if installed.get("ok") is not True:
                    return {"ok": False, "id": identifier, "error": str(installed.get("content", "Extension installation failed."))}
            else:
                changed = []
                try:
                    for entry in proposal["files"]:
                        path = self._source_path(entry["path"])
                        self._atomic_write(path, entry["content"].encode("utf-8"))
                        changed.append(entry)
                except Exception:
                    self._restore_core_entries(changed)
                    raise
            record.update(status="applied", applied_at=time.time())
            try:
                self._save(record)
            except Exception:
                # Keep the durable proposal state and the live installation in
                # agreement if the disk cannot record the successful change.
                if proposal["kind"] == "extension":
                    self.registry.restore(proposal["metadata"]["name"], proposal["before"])
                else:
                    self._restore_core_entries(proposal["files"])
                raise
            return {"ok": True, "id": identifier, "status": "applied", "restart_required": proposal["kind"] == "core",
                    "message": "The approved extension is available now." if proposal["kind"] == "extension" else "Source updated. Restart Jarvis to load the change."}

    def _restore_core_entries(self, entries: list[dict[str, Any]]) -> None:
        for entry in reversed(entries):
            path = self._source_path(entry["path"])
            if entry["before"] is None:
                if path.exists():
                    path.unlink()
            else:
                self._atomic_write(path, entry["before"].encode("utf-8"))

    def _check_applied(self, proposal: dict[str, Any]) -> None:
        if proposal["kind"] == "extension":
            installed = self.registry.snapshot(proposal["metadata"]["name"])
            if not installed or installed.get("source") != proposal["code"]:
                raise ValueError("The installed extension changed after application. It cannot be overwritten by rollback.")
            metadata = {key: installed["metadata"].get(key) for key in proposal["metadata"]}
            if metadata != proposal["metadata"]:
                raise ValueError("The installed extension's metadata changed after application.")
        else:
            for entry in proposal["files"]:
                path = self._source_path(entry["path"])
                expected = entry["content"].encode("utf-8")
                if not path.is_file():
                    raise ValueError(f"{entry['path']} changed after application. It cannot be overwritten by rollback.")
                with path.open("rb") as handle:
                    actual = handle.read(len(expected) + 1)
                if actual != expected:
                    raise ValueError(f"{entry['path']} changed after application. It cannot be overwritten by rollback.")

    def rollback(self, identifier: str) -> dict[str, Any]:
        with self._operation_lock:
            record = self._load(identifier)
            if record["status"] != "applied":
                raise ValueError("Only an applied proposal can be rolled back.")
            proposal = record["proposal"]
            self._check_applied(proposal)
            self._approved("selfdev.rollback", record)
            self._check_applied(proposal)
            if proposal["kind"] == "extension":
                restored = self.registry.restore(proposal["metadata"]["name"], proposal["before"])
                if restored.get("ok") is not True:
                    return {"ok": False, "id": identifier, "error": "Could not restore the previous extension."}
            else:
                restored = []
                try:
                    for entry in reversed(proposal["files"]):
                        self._restore_core_entries([entry])
                        restored.append(entry)
                except Exception:
                    for entry in restored:
                        self._atomic_write(self._source_path(entry["path"]), entry["content"].encode("utf-8"))
                    raise
            record.update(status="rolled_back", rolled_back_at=time.time())
            try:
                self._save(record)
            except Exception:
                if proposal["kind"] == "extension":
                    self.registry.install(proposal["metadata"], proposal["code"])
                else:
                    for entry in proposal["files"]:
                        self._atomic_write(self._source_path(entry["path"]), entry["content"].encode("utf-8"))
                raise
            return {"ok": True, "id": identifier, "status": "rolled_back", "restart_required": proposal["kind"] == "core"}

    def handle(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("Self-development arguments must be an object.")
        action = payload.get("action", "inspect")
        if action == "inspect":
            return self.inspect(payload.get("path", "."), payload.get("line", 1))
        if action == "propose":
            return self.propose(payload)
        if action == "list":
            return {"ok": True, "proposals": self.list(), "enabled": self.enabled}
        if action in {"review", "test", "apply", "rollback"}:
            return getattr(self, action)(payload.get("id", ""))
        raise ValueError("Self-development actions are inspect, propose, list, review, test, apply, and rollback. Permissions are changed by the user in Permissions.")

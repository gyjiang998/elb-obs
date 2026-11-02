#!/usr/bin/env python3
"""Portless workspace file/exec service carried by one SSH exec channel.

The worker intentionally uses only the Python standard library.  Stdout is a
binary protocol transport; diagnostics and accidental prints are redirected to
stderr so file contents cannot corrupt framing.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import errno
import fcntl
import fnmatch
import hashlib
import json
import os
import re
import selectors
import signal
import secrets
import stat as stat_module
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import PurePosixPath
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, Tuple


SCHEMA_VERSION = "remote_workspace_rpc/v1"
STREAM_PROTOCOL = "remote_workspace_rpc_stream/v1"
READY_LINE = f"HETERO_WORKSPACE_RPC_READY\t{STREAM_PROTOCOL}\n".encode("ascii")
REQUEST_HEADER = b"HETERO_WORKSPACE_RPC_REQUEST\t"
RESPONSE_HEADER = b"HETERO_WORKSPACE_RPC_RESPONSE\t"
MAX_HEADER_BYTES = 64 * 1024
MAX_REQUEST_BYTES = 4 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_PENDING_REQUESTS = 64
MAX_PATH_BYTES = 4096
MAX_TEXT_BYTES_HARD = 4 * 1024 * 1024
MAX_DOWNLOAD_CHUNK_BYTES = 512 * 1024
MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024 * 1024
MAX_LIST_ENTRIES = 5000
MAX_SEARCH_RESULTS = 1000
MAX_SEARCH_FILES = 20000
MAX_EXEC_OUTPUT_BYTES = 8 * 1024 * 1024
MAX_EXEC_COMMAND_BYTES = 64 * 1024
DEFAULT_SKIPPED_DIRECTORIES = frozenset({
    ".git", ".cache", ".cargo", ".heterogeneous-workbench", ".local",
    ".mypy_cache", ".next", ".npm", ".pytest_cache", ".rustup", ".venv",
    ".vscode-server", "__pycache__", "build", "dist", "node_modules", "out",
    "target", "venv",
})

# Python's backtracking `re` engine cannot be interrupted safely from another
# thread. Regex search therefore runs in an isolated helper process. The
# parent enforces the request deadline/cancellation and can kill the whole
# helper process group without sacrificing the persistent Workspace lane.
REGEX_SEARCH_HELPER = r'''import hashlib, json, os, re, stat, sys
p = json.load(sys.stdin)
flags = 0 if p["caseSensitive"] else re.IGNORECASE
expr = re.compile(p["query"], flags)
dir_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
file_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
root_fd = os.open(p["root"], dir_flags)
matches = []
scanned = 0
skipped = 0
try:
    for candidate in p["files"]:
        parent_fd = os.dup(root_fd)
        descriptor = -1
        try:
            parts = candidate.split("/")
            for component in parts[:-1]:
                next_fd = os.open(component, dir_flags, dir_fd=parent_fd)
                os.close(parent_fd)
                parent_fd = next_fd
            descriptor = os.open(parts[-1], file_flags, dir_fd=parent_fd)
            attributes = os.fstat(descriptor)
            if not stat.S_ISREG(attributes.st_mode) or attributes.st_size > p["maxFileBytes"]:
                skipped += 1
                continue
            chunks = []
            remaining = p["maxFileBytes"] + 1
            while remaining:
                chunk = os.read(descriptor, min(262144, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            data = b"".join(chunks)
            if len(data) > p["maxFileBytes"] or b"\0" in data:
                skipped += 1
                continue
            try:
                text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                skipped += 1
                continue
            scanned += 1
            file_revision = "sha256:" + hashlib.sha256(data).hexdigest()
            mtime_ms = int(attributes.st_mtime_ns / 1_000_000)
            for number, line in enumerate(text.splitlines(), start=1):
                match = expr.search(line)
                if match is None:
                    continue
                matches.append({"path": candidate, "line": number, "column": match.start() + 1,
                                "preview": line[:500], "revision": file_revision,
                                "size": int(attributes.st_size), "mtimeMs": mtime_ms})
                if len(matches) >= p["maxResults"]:
                    print(json.dumps({"matches": matches, "scanned": scanned, "skipped": skipped, "limited": True}, ensure_ascii=True, separators=(",", ":")))
                    raise SystemExit(0)
        except (OSError, ValueError):
            skipped += 1
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            os.close(parent_fd)
finally:
    os.close(root_fd)
print(json.dumps({"matches": matches, "scanned": scanned, "skipped": skipped, "limited": False}, ensure_ascii=True, separators=(",", ":")))
'''


class WorkspaceError(Exception):
    def __init__(self, code: str, message: str, details: Any = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details


def bounded_integer(value: Any, default: int, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(maximum, max(minimum, parsed))


def revision(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def validate_json_unicode(value: Any) -> None:
    """Reject lone JSON surrogate code points before they reach UTF-8 APIs."""
    if isinstance(value, str):
        try:
            value.encode("utf-8", errors="strict")
        except UnicodeEncodeError as error:
            raise WorkspaceError(
                "WORKSPACE_RPC_INVALID_REQUEST",
                "Workspace 请求包含无效的 Unicode 代理码点。",
            ) from error
        return
    if isinstance(value, list):
        for item in value:
            validate_json_unicode(item)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            validate_json_unicode(key)
            validate_json_unicode(item)


def workspace_glob_matches(path: str, pattern: str) -> bool:
    """Match POSIX paths: '*' stays in one segment and '**' spans segments."""
    path_parts = path.split("/") if path else []
    pattern_parts = pattern.split("/") if pattern else []
    memo: Dict[Tuple[int, int], bool] = {}

    def matches(path_index: int, pattern_index: int) -> bool:
        key = (path_index, pattern_index)
        if key in memo:
            return memo[key]
        if pattern_index == len(pattern_parts):
            result = path_index == len(path_parts)
        elif pattern_parts[pattern_index] == "**":
            result = matches(path_index, pattern_index + 1) or (
                path_index < len(path_parts) and matches(path_index + 1, pattern_index)
            )
        else:
            result = (
                path_index < len(path_parts)
                and fnmatch.fnmatchcase(path_parts[path_index], pattern_parts[pattern_index])
                and matches(path_index + 1, pattern_index + 1)
            )
        memo[key] = result
        return result

    return matches(0, 0)


def file_kind(mode: int) -> str:
    if stat_module.S_ISREG(mode):
        return "file"
    if stat_module.S_ISDIR(mode):
        return "directory"
    if stat_module.S_ISLNK(mode):
        return "symlink"
    return "other"


def metadata(value: os.stat_result) -> Dict[str, Any]:
    mtime_ms = int(value.st_mtime_ns / 1_000_000)
    return {
        "size": int(value.st_size),
        "mtimeMs": mtime_ms,
        "modifiedAt": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(value.st_mtime))
        + f".{mtime_ms % 1000:03d}Z",
    }


def decode_text_document(data: bytes) -> Dict[str, Any]:
    if b"\0" in data:
        raise WorkspaceError("WORKSPACE_BINARY_FILE", "远程文件包含二进制数据，无法作为文本打开。")
    bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if bom else data
    try:
        text = body.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise WorkspaceError("WORKSPACE_BINARY_FILE", "远程文件不是有效的 UTF-8 文本。") from error
    controls = sum(1 for byte in body if byte < 0x20 and byte not in (0x09, 0x0A, 0x0C, 0x0D))
    if controls > max(2, len(body) // 100):
        raise WorkspaceError("WORKSPACE_BINARY_FILE", "远程文件包含大量控制字符，无法作为文本打开。")
    has_crlf = "\r\n" in text
    without_crlf = text.replace("\r\n", "")
    has_cr = "\r" in without_crlf
    has_lf = "\n" in without_crlf
    endings = int(has_crlf) + int(has_cr) + int(has_lf)
    line_ending = "mixed" if endings > 1 else "crlf" if has_crlf else "cr" if has_cr else "lf"
    return {"content": text.replace("\r\n", "\n").replace("\r", "\n"), "bom": bom, "lineEnding": line_ending}


def encode_text_document(content: Any, bom: Any, line_ending: Any) -> bytes:
    if not isinstance(content, str) or "\0" in content:
        raise WorkspaceError("WORKSPACE_TEXT_INVALID", "只能保存不含 NUL 字节的 UTF-8 文本。")
    if not isinstance(bom, bool):
        raise WorkspaceError("WORKSPACE_TEXT_INVALID", "UTF-8 BOM 元数据无效。")
    if line_ending == "mixed":
        raise WorkspaceError("WORKSPACE_LINE_ENDING_MIXED", "请先选择统一的换行格式再保存。")
    if line_ending not in ("lf", "crlf", "cr"):
        raise WorkspaceError("WORKSPACE_TEXT_INVALID", "换行符元数据无效。")
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    separator = {"lf": "\n", "crlf": "\r\n", "cr": "\r"}[line_ending]
    data = normalized.replace("\n", separator).encode("utf-8")
    return (b"\xef\xbb\xbf" + data) if bom else data


class WorkspaceService:
    def __init__(self, root: str, max_text_bytes: int):
        canonical = os.path.realpath(root)
        if not os.path.isabs(canonical) or not os.path.isdir(canonical):
            raise WorkspaceError("WORKSPACE_ROOT_INVALID", "Workspace root 必须是可访问的绝对目录。")
        self.root = canonical
        self.max_text_bytes = min(MAX_TEXT_BYTES_HARD, max(1024, int(max_text_bytes)))
        self._processes: Dict[str, subprocess.Popen[bytes]] = {}
        self._process_lock = threading.Lock()
        self._closing = threading.Event()
        self._request_context = threading.local()
        # Keep an independently-owned directory descriptor for every temporary
        # file. A bounded shutdown watchdog can unlink it even if the worker
        # thread is blocked in a filesystem call when the SSH channel closes.
        self._temporary_files: Dict[str, Tuple[int, str]] = {}
        self._temporary_lock = threading.Lock()
        self._lock_directory = os.path.join(tempfile.gettempdir(), f"hetero-workspace-locks-{os.getuid()}")
        try:
            os.makedirs(self._lock_directory, mode=0o700, exist_ok=True)
            lock_attributes = os.lstat(self._lock_directory)
            if not stat_module.S_ISDIR(lock_attributes.st_mode) or stat_module.S_ISLNK(lock_attributes.st_mode):
                raise OSError("lock namespace is not a real directory")
            if lock_attributes.st_uid != os.getuid():
                raise OSError("lock namespace has a different owner")
            if stat_module.S_IMODE(lock_attributes.st_mode) & 0o077:
                os.chmod(self._lock_directory, 0o700)
        except OSError as error:
            raise WorkspaceError("WORKSPACE_LOCK_UNAVAILABLE", f"无法建立 Workspace 写锁目录：{error}") from error

    def close(self) -> None:
        self._closing.set()
        with self._process_lock:
            processes = list(self._processes.values())
        for process in processes:
            self._kill_process(process)

    def _track_temporary(self, parent_fd: int, name: str) -> str:
        token = secrets.token_hex(16)
        duplicate = -1
        try:
            duplicate = os.dup(parent_fd)
            with self._temporary_lock:
                if self._closing.is_set():
                    raise WorkspaceError("WORKSPACE_RPC_CANCELLED", "Workspace 正在关闭，未提交临时文件。")
                self._temporary_files[token] = (duplicate, name)
                duplicate = -1
            return token
        finally:
            if duplicate >= 0:
                os.close(duplicate)

    def _release_temporary(self, token: str, remove: bool) -> bool:
        with self._temporary_lock:
            tracked = self._temporary_files.pop(token, None)
        if tracked is None:
            return False
        descriptor, name = tracked
        try:
            if remove:
                try:
                    os.unlink(name, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
        finally:
            os.close(descriptor)
        return True

    def cleanup_temporary_files(self, blocking: bool = True) -> bool:
        acquired = self._temporary_lock.acquire(blocking=blocking)
        if not acquired:
            return False
        try:
            tracked = list(self._temporary_files.values())
            self._temporary_files.clear()
        finally:
            self._temporary_lock.release()
        for descriptor, name in tracked:
            try:
                try:
                    os.unlink(name, dir_fd=descriptor)
                except FileNotFoundError:
                    pass
                except OSError:
                    pass
            finally:
                os.close(descriptor)
        return True

    def cancel(self, request_id: str) -> bool:
        cancellations = getattr(self, "_cancellations", None)
        cancellations_lock = getattr(self, "_cancellations_lock", None)
        if cancellations is not None and cancellations_lock is not None:
            with cancellations_lock:
                event = cancellations.get(request_id)
        else:
            event = None
        if event is not None:
            event.set()
        with self._process_lock:
            process = self._processes.get(request_id)
        if process is not None:
            self._kill_process(process)
        return event is not None or process is not None

    def attach_cancellations(self, cancellations: Dict[str, threading.Event],
                             lock: threading.Lock) -> None:
        self._cancellations = cancellations
        self._cancellations_lock = lock

    def _cancelled(self) -> bool:
        event = getattr(self._request_context, "cancel_event", None)
        return self._closing.is_set() or (event is not None and event.is_set())

    def _raise_if_cancelled(self) -> None:
        if self._cancelled():
            raise WorkspaceError("WORKSPACE_RPC_CANCELLED", "Workspace 请求已取消，未提交更改。")

    def _relative(self, value: Any, allow_root: bool = True) -> str:
        if value is None:
            value = ""
        if not isinstance(value, str) or "\0" in value or "\\" in value:
            raise WorkspaceError("WORKSPACE_PATH_INVALID", "Workspace 路径无效。")
        if len(value.encode("utf-8")) > MAX_PATH_BYTES or value.startswith("/"):
            raise WorkspaceError("WORKSPACE_PATH_OUTSIDE_ROOT", "路径必须位于 Workspace root 内。")
        pieces = value.split("/")
        if ".." in pieces:
            raise WorkspaceError("WORKSPACE_PATH_OUTSIDE_ROOT", "路径超出 Workspace root，已拒绝访问。")
        normalized = "/".join(piece for piece in pieces if piece not in ("", "."))
        if not allow_root and not normalized:
            raise WorkspaceError("WORKSPACE_PATH_INVALID", "请选择 Workspace 内的文件。")
        return normalized

    def _inside(self, candidate: str) -> bool:
        try:
            return os.path.commonpath((self.root, candidate)) == self.root
        except ValueError:
            return False

    def _lexical(self, relative: str) -> str:
        return os.path.join(self.root, *relative.split("/")) if relative else self.root

    def _fd_canonical_path(self, descriptor: int) -> Optional[str]:
        proc_path = f"/proc/self/fd/{descriptor}"
        if os.path.exists(proc_path):
            return os.path.realpath(proc_path)
        get_path = getattr(fcntl, "F_GETPATH", None)
        if get_path is not None:
            try:
                raw = fcntl.fcntl(descriptor, get_path, b"\0" * 1024)
                return os.path.realpath(raw.split(b"\0", 1)[0].decode("utf-8"))
            except (OSError, UnicodeDecodeError, ValueError):
                return None
        return None

    def _verify_fd_inside(self, descriptor: int) -> str:
        actual = self._fd_canonical_path(descriptor)
        if actual is None or actual.endswith(" (deleted)") or not self._inside(actual):
            raise WorkspaceError(
                "WORKSPACE_SYMLINK_OUTSIDE_ROOT",
                "打开后的文件描述符不再位于 Workspace root 内，已拒绝访问。",
            )
        return actual

    def _verify_parent_fd(self, descriptor: int, relative: str) -> str:
        actual = self._verify_fd_inside(descriptor)
        parent_relative = relative.rsplit("/", 1)[0] if "/" in relative else ""
        expected = os.path.realpath(self._lexical(parent_relative))
        if actual != expected:
            raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件的父目录已在保存期间发生变化。")
        return actual

    def _open_parent_fd(self, relative: str) -> Tuple[int, str]:
        parts = relative.split("/")
        basename = parts[-1]
        flags = (
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
        descriptor = -1
        try:
            descriptor = os.open(self.root, flags)
            self._verify_fd_inside(descriptor)
            for component in parts[:-1]:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
                if not stat_module.S_ISDIR(os.fstat(descriptor).st_mode):
                    raise WorkspaceError("WORKSPACE_NOT_DIRECTORY", "远程文件的父路径不是目录。")
                self._verify_fd_inside(descriptor)
            return descriptor, basename
        except WorkspaceError:
            if descriptor >= 0:
                os.close(descriptor)
            raise
        except OSError as error:
            if descriptor >= 0:
                os.close(descriptor)
            if error.errno in (errno.ELOOP, errno.ENOTDIR):
                raise WorkspaceError(
                    "WORKSPACE_SYMLINK_WRITE_BLOCKED",
                    "远程文件的父路径包含符号链接或不是目录，已拒绝保存。",
                ) from error
            if error.errno == errno.ENOENT:
                raise WorkspaceError("WORKSPACE_PATH_NOT_FOUND", "远程文件的父路径不存在。") from error
            raise WorkspaceError("WORKSPACE_WRITE_FAILED", f"远程文件父路径打开失败：{error}") from error

    def _acquire_write_lock(self, relative: str) -> int:
        lock_identity = (self.root + "\0" + relative).encode("utf-8")
        lock_name = hashlib.sha256(lock_identity).hexdigest() + ".lock"
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(os.path.join(self._lock_directory, lock_name), flags, 0o600)
            if not stat_module.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError("lock is not a regular file")
            os.fchmod(descriptor, 0o600)
            while True:
                self._raise_if_cancelled()
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return descriptor
                except BlockingIOError:
                    time.sleep(0.01)
        except WorkspaceError:
            if "descriptor" in locals():
                os.close(descriptor)
            raise
        except OSError as error:
            if "descriptor" in locals():
                os.close(descriptor)
            raise WorkspaceError("WORKSPACE_LOCK_UNAVAILABLE", f"无法获取 Workspace 写锁：{error}") from error

    def _existing(self, value: Any, allow_root: bool = True) -> Tuple[str, str]:
        relative = self._relative(value, allow_root=allow_root)
        lexical = self._lexical(relative)
        canonical = os.path.realpath(lexical)
        if not self._inside(canonical):
            raise WorkspaceError("WORKSPACE_SYMLINK_OUTSIDE_ROOT", "符号链接指向 Workspace root 之外，已拒绝访问。")
        if not os.path.exists(canonical):
            raise WorkspaceError("WORKSPACE_PATH_NOT_FOUND", f"Workspace 路径不存在：{relative or '.'}")
        return relative, canonical

    def _read_bytes(self, canonical: str, maximum: Optional[int] = None) -> Tuple[bytes, os.stat_result]:
        limit = self.max_text_bytes if maximum is None else min(MAX_TEXT_BYTES_HARD, maximum)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(canonical, flags)
        except OSError as error:
            raise WorkspaceError("WORKSPACE_READ_FAILED", f"远程文件打开失败：{error}") from error
        try:
            self._verify_fd_inside(descriptor)
            return self._read_descriptor(descriptor, limit)
        finally:
            os.close(descriptor)

    def _read_bytes_at(self, parent_fd: int, basename: str,
                       maximum: Optional[int] = None) -> Tuple[bytes, os.stat_result]:
        limit = self.max_text_bytes if maximum is None else min(MAX_TEXT_BYTES_HARD, maximum)
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(basename, flags, dir_fd=parent_fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise WorkspaceError("WORKSPACE_SYMLINK_WRITE_BLOCKED", "目标文件是符号链接，已拒绝保存。") from error
            raise WorkspaceError("WORKSPACE_READ_FAILED", f"远程文件打开失败：{error}") from error
        try:
            self._verify_fd_inside(descriptor)
            return self._read_descriptor(descriptor, limit)
        finally:
            os.close(descriptor)

    def _read_descriptor(self, descriptor: int, limit: int) -> Tuple[bytes, os.stat_result]:
        try:
            attributes = os.fstat(descriptor)
            if not stat_module.S_ISREG(attributes.st_mode):
                raise WorkspaceError("WORKSPACE_NOT_FILE", "所选 Workspace 路径不是普通文件。")
            if attributes.st_size > limit:
                raise WorkspaceError("WORKSPACE_FILE_TOO_LARGE", f"远程文本文件超过 {limit} 字节限制。")
            chunks: List[bytes] = []
            total = 0
            while total <= limit:
                chunk = os.read(descriptor, min(256 * 1024, limit + 1 - total))
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            if total > limit:
                raise WorkspaceError("WORKSPACE_FILE_TOO_LARGE", f"远程文本文件超过 {limit} 字节限制。")
            return b"".join(chunks), attributes
        except OSError as error:
            raise WorkspaceError("WORKSPACE_READ_FAILED", f"远程文件读取失败：{error}") from error

    def ping(self, _params: Dict[str, Any]) -> Dict[str, Any]:
        return {"protocol": SCHEMA_VERSION, "root": self.root, "pid": os.getpid()}

    def stat(self, params: Dict[str, Any]) -> Dict[str, Any]:
        relative, canonical = self._existing(params.get("path", ""))
        requested = self._lexical(relative)
        symlink = os.path.islink(requested)
        attributes = os.stat(canonical, follow_symlinks=True)
        return {"path": relative, "type": file_kind(attributes.st_mode), "symlink": symlink, **metadata(attributes)}

    def list(self, params: Dict[str, Any]) -> Dict[str, Any]:
        relative, canonical = self._existing(params.get("path", ""))
        if not os.path.isdir(canonical):
            raise WorkspaceError("WORKSPACE_NOT_DIRECTORY", "所选 Workspace 路径不是目录。")
        limit = bounded_integer(params.get("maxEntries"), MAX_LIST_ENTRIES, 1, MAX_LIST_ENTRIES)
        entries: List[Dict[str, Any]] = []
        truncated = False
        try:
            with os.scandir(canonical) as iterator:
                for entry in iterator:
                    if len(entries) >= limit:
                        truncated = True
                        break
                    entry_relative = f"{relative}/{entry.name}" if relative else entry.name
                    is_symlink = entry.is_symlink()
                    accessible = True
                    try:
                        target = os.path.realpath(entry.path)
                        if not self._inside(target):
                            accessible = False
                            attributes = entry.stat(follow_symlinks=False)
                            kind = "symlink"
                        else:
                            attributes = entry.stat(follow_symlinks=True)
                            kind = file_kind(attributes.st_mode)
                    except OSError:
                        accessible = False
                        attributes = entry.stat(follow_symlinks=False)
                        kind = file_kind(attributes.st_mode)
                    entries.append({
                        "name": entry.name, "path": entry_relative, "type": kind,
                        "symlink": is_symlink, "accessible": accessible, **metadata(attributes),
                    })
        except OSError as error:
            raise WorkspaceError("WORKSPACE_LIST_FAILED", f"Workspace 目录读取失败：{error}") from error
        order = {"directory": 0, "file": 1, "symlink": 2, "other": 3}
        entries.sort(key=lambda item: (order.get(item["type"], 4), item["name"].casefold(), item["name"]))
        parent = relative.rsplit("/", 1)[0] if "/" in relative else "" if relative else None
        if not params.get("recursive", False):
            return {"path": relative, "parentPath": parent, "entries": entries, "truncated": truncated}
        max_depth = bounded_integer(params.get("maxDepth"), 5, 1, 12)
        flattened = list(entries)
        queue: List[Tuple[str, int]] = [
            (entry["path"], 1) for entry in entries
            if entry["type"] == "directory" and entry["accessible"] and not entry["symlink"]
            and entry["name"] not in DEFAULT_SKIPPED_DIRECTORIES
        ]
        cursor = 0
        reason: Optional[str] = "entry_limit" if truncated else None
        while cursor < len(queue) and len(flattened) < limit:
            if self._cancelled():
                reason = "cancelled"
                break
            directory, depth = queue[cursor]
            cursor += 1
            if depth > max_depth:
                reason = reason or "depth_limit"
                continue
            nested = self.list({"path": directory, "maxEntries": limit - len(flattened)})
            flattened.extend(nested["entries"])
            if nested["truncated"]:
                reason = reason or "entry_limit"
            if depth < max_depth:
                queue.extend((entry["path"], depth + 1) for entry in nested["entries"]
                             if entry["type"] == "directory" and entry["accessible"]
                             and not entry["symlink"] and entry["name"] not in DEFAULT_SKIPPED_DIRECTORIES)
        if cursor < len(queue) and len(flattened) >= limit:
            reason = reason or "entry_limit"
        return {
            "path": relative, "parentPath": parent, "entries": flattened[:limit],
            "truncated": reason is not None, "recursive": True,
            "maxDepth": max_depth, "reason": reason,
            "stats": {"entryCount": min(len(flattened), limit), "directoryCount": cursor},
        }

    def prefetch(self, params: Dict[str, Any]) -> Dict[str, Any]:
        raw_paths = params.get("paths", [])
        if not isinstance(raw_paths, list):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "prefetch.paths 必须是数组。")
        max_depth = bounded_integer(params.get("maxDepth"), 5, 1, 12)
        max_directories = bounded_integer(params.get("maxDirectories"), 192, 1, 512)
        max_entries = bounded_integer(params.get("maxEntries"), 5000, 1, 15000)
        time_budget_ms = bounded_integer(params.get("timeBudgetMs"), 15000, 100, 30000)
        paths: List[str] = []
        for raw in raw_paths[:max_directories]:
            value = self._relative(raw)
            if value not in paths and (not value or PurePosixPath(value).name not in DEFAULT_SKIPPED_DIRECTORIES):
                paths.append(value)
        started = time.monotonic()
        queue: List[Tuple[str, int, bool]] = []
        seen: set[str] = set()
        directories: List[Dict[str, Any]] = []
        failures: List[Dict[str, str]] = []
        entry_count = 0
        skipped = 0
        reason: Optional[str] = None

        def enqueue(value: str, depth: int, seed: bool = False) -> None:
            nonlocal reason
            if value in seen:
                return
            if len(seen) >= max_directories:
                reason = reason or "directory_limit"
                return
            seen.add(value)
            queue.append((value, depth, seed))

        for value in paths:
            enqueue(value, len(value.split("/")) if value else 0, True)
        cursor = 0
        while cursor < len(queue):
            if self._cancelled():
                reason = reason or "cancelled"
                break
            if (time.monotonic() - started) * 1000 >= time_budget_ms:
                reason = reason or "time_budget"
                break
            if entry_count >= max_entries:
                reason = reason or "entry_limit"
                break
            current, depth, seed = queue[cursor]
            cursor += 1
            try:
                if seed and current and os.path.islink(self._lexical(current)):
                    skipped += 1
                    continue
                result = self.list({"path": current, "maxEntries": MAX_LIST_ENTRIES})
            except WorkspaceError as error:
                failures.append({"path": current, "code": error.code})
                continue
            values = result["entries"]
            if result.get("truncated"):
                reason = reason or "entry_limit"
            if entry_count + len(values) > max_entries:
                reason = reason or "entry_limit"
                continue
            directories.append(result)
            entry_count += len(values)
            if depth >= max_depth:
                continue
            for entry in values:
                if entry["type"] != "directory" or not entry["accessible"] or entry["symlink"]:
                    continue
                if entry["name"] in DEFAULT_SKIPPED_DIRECTORIES:
                    skipped += 1
                    continue
                enqueue(entry["path"], depth + 1)
        cancelled = reason == "cancelled"
        return {
            "directories": directories,
            "truncated": bool(reason and not cancelled),
            "cancelled": cancelled,
            "reason": reason,
            "stats": {
                "directoryCount": len(directories), "entryCount": entry_count,
                "failedDirectories": len(failures), "skippedDirectories": skipped,
                "durationMs": int((time.monotonic() - started) * 1000),
            },
            "failures": failures[:32],
        }

    def readText(self, params: Dict[str, Any]) -> Dict[str, Any]:
        relative, canonical = self._existing(params.get("path"), allow_root=False)
        requested_limit = bounded_integer(params.get("maxBytes"), self.max_text_bytes, 1, self.max_text_bytes)
        data, attributes = self._read_bytes(canonical, requested_limit)
        document = decode_text_document(data)
        result = {
            "path": relative, **document, "encoding": "utf8", **metadata(attributes),
            "revision": revision(data),
        }
        has_range = "lineStart" in params or "lineEnd" in params
        if has_range:
            line_start = params.get("lineStart", 1)
            line_end = params.get("lineEnd")
            if isinstance(line_start, bool) or not isinstance(line_start, int) or line_start < 1:
                raise WorkspaceError("WORKSPACE_INVALID_LINE_RANGE", "lineStart 必须是从 1 开始的整数。")
            if line_end is None:
                line_end = line_start + 499
            if isinstance(line_end, bool) or not isinstance(line_end, int) or line_end < line_start:
                raise WorkspaceError("WORKSPACE_INVALID_LINE_RANGE", "lineEnd 必须是不小于 lineStart 的整数。")
            if line_end - line_start + 1 > 5000:
                raise WorkspaceError("WORKSPACE_INVALID_LINE_RANGE", "单次最多读取 5000 行。")
            lines = document["content"].splitlines(keepends=True)
            total_lines = len(lines)
            actual_end = min(line_end, total_lines)
            result["content"] = "".join(lines[line_start - 1:actual_end]) if line_start <= total_lines else ""
            result.update({"lineStart": line_start, "lineEnd": actual_end, "totalLines": total_lines})
        return result

    @staticmethod
    def _download_snapshot(relative: str, attributes: os.stat_result) -> str:
        identity = "\0".join((
            relative,
            str(attributes.st_dev),
            str(attributes.st_ino),
            str(attributes.st_size),
            str(attributes.st_mtime_ns),
            str(attributes.st_ctime_ns),
        )).encode("utf-8")
        return "snapshot:" + hashlib.sha256(identity).hexdigest()

    def readChunk(self, params: Dict[str, Any]) -> Dict[str, Any]:
        """Read one bounded download chunk under a stable remote-file fence."""
        relative, canonical = self._existing(params.get("path"), allow_root=False)
        offset = params.get("offset", 0)
        length = params.get("length", MAX_DOWNLOAD_CHUNK_BYTES)
        if (isinstance(offset, bool) or not isinstance(offset, int) or offset < 0
                or offset > MAX_DOWNLOAD_BYTES):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "download offset 必须是有效的非负整数。")
        if (isinstance(length, bool) or not isinstance(length, int) or length < 1
                or length > MAX_DOWNLOAD_CHUNK_BYTES):
            raise WorkspaceError(
                "WORKSPACE_INVALID_REQUEST",
                f"download length 必须在 1–{MAX_DOWNLOAD_CHUNK_BYTES} 字节之间。",
            )
        expected_snapshot = params.get("expectedSnapshot")
        if expected_snapshot is not None and (
                not isinstance(expected_snapshot, str)
                or not re.fullmatch(r"snapshot:[0-9a-f]{64}", expected_snapshot)):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "expectedSnapshot 无效。")
        expected_size = params.get("expectedSize")
        expected_mtime = params.get("expectedMtimeMs")
        expected_revision = params.get("expectedRevision")
        if expected_size is not None and (
                isinstance(expected_size, bool) or not isinstance(expected_size, int)
                or expected_size < 0 or expected_size > MAX_DOWNLOAD_BYTES):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "expectedSize 必须是有效的非负整数。")
        if expected_mtime is not None and (
                isinstance(expected_mtime, bool) or not isinstance(expected_mtime, int)
                or expected_mtime < 0):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "expectedMtimeMs 必须是有效的非负整数。")
        if expected_revision is not None and (
                not isinstance(expected_revision, str)
                or not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_revision)):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "expectedRevision 无效。")
        if offset != 0 and any(value is not None for value in (
                expected_size, expected_mtime, expected_revision)):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "远程文件身份只能随首个下载分块校验。")
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(canonical, flags)
        except OSError as error:
            raise WorkspaceError("WORKSPACE_READ_FAILED", f"远程下载文件打开失败：{error}") from error
        try:
            self._verify_fd_inside(descriptor)
            before = os.fstat(descriptor)
            if not stat_module.S_ISREG(before.st_mode):
                raise WorkspaceError("WORKSPACE_NOT_FILE", "所选 Workspace 路径不是普通文件。")
            if before.st_size > MAX_DOWNLOAD_BYTES:
                raise WorkspaceError(
                    "WORKSPACE_DOWNLOAD_TOO_LARGE",
                    "远程文件超过 32 GiB 下载上限；请使用 SSH/SCP 传输该超大文件。",
                )
            snapshot = self._download_snapshot(relative, before)
            if expected_snapshot is not None and snapshot != expected_snapshot:
                raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件已在下载期间发生变化，请重新下载。")
            if expected_size is not None and before.st_size != expected_size:
                raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件大小已在下载前发生变化，请刷新后重试。")
            actual_mtime = int(before.st_mtime_ns / 1_000_000)
            if expected_mtime is not None and actual_mtime != expected_mtime:
                raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件修改时间已在下载前发生变化，请刷新后重试。")
            if expected_revision is not None:
                # Revisions originate from the bounded UTF-8 editor read.  Do
                # not let an untrusted request turn first-chunk validation into
                # an unbounded 32 GiB hashing job.
                if before.st_size > MAX_TEXT_BYTES_HARD:
                    raise WorkspaceError(
                        "WORKSPACE_INVALID_REQUEST",
                        "expectedRevision 仅适用于可在编辑器中读取的有界文件。",
                    )
                digest = hashlib.sha256()
                revision_offset = 0
                while revision_offset < before.st_size:
                    self._raise_if_cancelled()
                    revision_chunk = os.pread(
                        descriptor,
                        min(256 * 1024, before.st_size - revision_offset),
                        revision_offset,
                    )
                    if not revision_chunk:
                        break
                    digest.update(revision_chunk)
                    revision_offset += len(revision_chunk)
                if (revision_offset != before.st_size
                        or "sha256:" + digest.hexdigest() != expected_revision):
                    raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件内容已在下载前发生变化，请重新加载。")
                if self._download_snapshot(relative, os.fstat(descriptor)) != snapshot:
                    raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件已在身份校验期间发生变化，请重新下载。")
            if hasattr(os, "pread"):
                data = os.pread(descriptor, length, offset)
            else:
                os.lseek(descriptor, offset, os.SEEK_SET)
                data = os.read(descriptor, length)
            after = os.fstat(descriptor)
            if self._download_snapshot(relative, after) != snapshot:
                raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件已在下载期间发生变化，请重新下载。")
            return {
                "path": relative,
                "offset": offset,
                "bytesRead": len(data),
                "contentBase64": base64.b64encode(data).decode("ascii"),
                "encoding": "base64",
                "eof": offset + len(data) >= before.st_size,
                "snapshot": snapshot,
                **metadata(before),
            }
        except OSError as error:
            raise WorkspaceError("WORKSPACE_READ_FAILED", f"远程文件分块读取失败：{error}") from error
        finally:
            os.close(descriptor)

    def _validate_entry_snapshot(self, params: Dict[str, Any], parent_fd: int,
                                 basename: str, attributes: os.stat_result) -> None:
        expected_size = params.get("expectedSize")
        expected_mtime = params.get("expectedMtimeMs")
        if expected_size is not None and (isinstance(expected_size, bool) or not isinstance(expected_size, int)):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "expectedSize 必须是整数。")
        if expected_mtime is not None and (isinstance(expected_mtime, bool) or not isinstance(expected_mtime, int)):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "expectedMtimeMs 必须是整数。")
        if expected_size is not None and int(attributes.st_size) != expected_size:
            raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程条目已在操作前发生变化，请刷新后重试。")
        actual_mtime = int(attributes.st_mtime_ns / 1_000_000)
        if expected_mtime is not None and actual_mtime != expected_mtime:
            raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程条目已在操作前发生变化，请刷新后重试。")
        expected_revision = params.get("expectedRevision")
        if expected_revision is not None:
            if not stat_module.S_ISREG(attributes.st_mode):
                raise WorkspaceError("WORKSPACE_NOT_FILE", "revision 只适用于普通文件。")
            current, _ = self._read_bytes_at(parent_fd, basename, MAX_TEXT_BYTES_HARD)
            if revision(current) != expected_revision:
                raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件已发生变化，请重新加载后再操作。")

    def rename(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._raise_if_cancelled()
        relative = self._relative(params.get("path"), allow_root=False)
        new_name = params.get("newName")
        if (not isinstance(new_name, str) or not new_name or new_name in (".", "..")
                or "/" in new_name or "\\" in new_name or "\0" in new_name
                or len(new_name.encode("utf-8")) > 255):
            raise WorkspaceError("WORKSPACE_PATH_INVALID", "新名称必须是当前目录中的单个有效名称。")
        parent_relative = relative.rsplit("/", 1)[0] if "/" in relative else ""
        destination = f"{parent_relative}/{new_name}" if parent_relative else new_name
        if destination == relative:
            return {"path": relative, "previousPath": relative, "renamed": False}
        parent_fd, basename = self._open_parent_fd(relative)
        locks: List[int] = []
        try:
            for lock_path in sorted((relative, destination)):
                locks.append(self._acquire_write_lock(lock_path))
            self._verify_parent_fd(parent_fd, relative)
            try:
                attributes = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError as error:
                raise WorkspaceError("WORKSPACE_PATH_NOT_FOUND", "待重命名条目不存在。") from error
            if stat_module.S_ISLNK(attributes.st_mode):
                raise WorkspaceError("WORKSPACE_SYMLINK_WRITE_BLOCKED", "为避免改变符号链接目标，已拒绝重命名。")
            if not (stat_module.S_ISREG(attributes.st_mode) or stat_module.S_ISDIR(attributes.st_mode)):
                raise WorkspaceError("WORKSPACE_NOT_FILE", "仅支持重命名普通文件或目录。")
            self._validate_entry_snapshot(params, parent_fd, basename, attributes)
            try:
                os.stat(new_name, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise WorkspaceError("WORKSPACE_FILE_EXISTS", "目标名称已存在。")
            self._raise_if_cancelled()
            self._verify_parent_fd(parent_fd, relative)
            try:
                if stat_module.S_ISREG(attributes.st_mode):
                    # link() has create-exclusive semantics. It prevents the
                    # check/rename race in which POSIX rename would silently
                    # replace a target created after the existence check.
                    os.link(basename, new_name, src_dir_fd=parent_fd,
                            dst_dir_fd=parent_fd, follow_symlinks=False)
                    try:
                        linked = os.stat(new_name, dir_fd=parent_fd, follow_symlinks=False)
                        latest = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
                        if ((linked.st_dev, linked.st_ino) != (attributes.st_dev, attributes.st_ino)
                                or (latest.st_dev, latest.st_ino) != (attributes.st_dev, attributes.st_ino)):
                            raise WorkspaceError(
                                "WORKSPACE_FILE_CHANGED",
                                "远程文件已在重命名期间发生变化，请刷新后重试。",
                            )
                        os.unlink(basename, dir_fd=parent_fd)
                    except BaseException:
                        try:
                            os.unlink(new_name, dir_fd=parent_fd)
                        except OSError:
                            pass
                        raise
                else:
                    # Reserve the directory name exclusively. POSIX rename can
                    # then replace only our empty placeholder; a concurrent
                    # creator cannot be overwritten.
                    os.mkdir(new_name, mode=0o700, dir_fd=parent_fd)
                    reserved = True
                    try:
                        os.rename(basename, new_name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                        reserved = False
                    finally:
                        if reserved:
                            try:
                                os.rmdir(new_name, dir_fd=parent_fd)
                            except OSError:
                                pass
            except FileExistsError as error:
                raise WorkspaceError("WORKSPACE_FILE_EXISTS", "目标名称已存在。") from error
            except OSError as error:
                raise WorkspaceError("WORKSPACE_RENAME_FAILED", f"远程条目重命名失败：{error}") from error
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            updated = os.stat(new_name, dir_fd=parent_fd, follow_symlinks=False)
            return {
                "path": destination, "previousPath": relative, "renamed": True,
                "type": file_kind(updated.st_mode), **metadata(updated),
            }
        finally:
            for descriptor in reversed(locks):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            os.close(parent_fd)

    def copy(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._raise_if_cancelled()
        source = self._relative(params.get("path"), allow_root=False)
        destination = self._relative(params.get("destinationPath"), allow_root=False)
        if source == destination:
            raise WorkspaceError("WORKSPACE_FILE_EXISTS", "复制目标与源文件相同。")
        source_parent, source_name = self._open_parent_fd(source)
        target_parent, target_name = self._open_parent_fd(destination)
        locks: List[int] = []
        source_fd = target_fd = -1
        temporary = ""
        temporary_token = ""
        try:
            for lock_path in sorted((source, destination)):
                locks.append(self._acquire_write_lock(lock_path))
            self._verify_parent_fd(source_parent, source)
            self._verify_parent_fd(target_parent, destination)
            try:
                source_attributes = os.stat(source_name, dir_fd=source_parent, follow_symlinks=False)
            except FileNotFoundError as error:
                raise WorkspaceError("WORKSPACE_PATH_NOT_FOUND", "待复制文件不存在。") from error
            if stat_module.S_ISLNK(source_attributes.st_mode):
                raise WorkspaceError("WORKSPACE_SYMLINK_WRITE_BLOCKED", "为避免复制符号链接目标，已拒绝操作。")
            if not stat_module.S_ISREG(source_attributes.st_mode):
                raise WorkspaceError("WORKSPACE_NOT_FILE", "当前版本只支持复制普通文件。")
            self._validate_entry_snapshot(params, source_parent, source_name, source_attributes)
            try:
                os.stat(target_name, dir_fd=target_parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise WorkspaceError("WORKSPACE_FILE_EXISTS", "复制目标已存在。")
            read_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            source_fd = os.open(source_name, read_flags, dir_fd=source_parent)
            self._verify_fd_inside(source_fd)
            opened_attributes = os.fstat(source_fd)
            if ((opened_attributes.st_dev, opened_attributes.st_ino,
                 opened_attributes.st_size, opened_attributes.st_mtime_ns,
                 opened_attributes.st_ctime_ns)
                    != (source_attributes.st_dev, source_attributes.st_ino,
                        source_attributes.st_size, source_attributes.st_mtime_ns,
                        source_attributes.st_ctime_ns)):
                raise WorkspaceError(
                    "WORKSPACE_FILE_CHANGED",
                    "远程文件已在复制期间发生变化，请刷新后重试。",
                )
            source_identity = (
                opened_attributes.st_dev, opened_attributes.st_ino,
                opened_attributes.st_size, opened_attributes.st_mtime_ns,
                opened_attributes.st_ctime_ns,
            )
            expected_revision = params.get("expectedRevision")
            copied_digest = hashlib.sha256() if expected_revision is not None else None

            def verify_source_unchanged() -> None:
                current = os.fstat(source_fd)
                current_identity = (
                    current.st_dev, current.st_ino, current.st_size,
                    current.st_mtime_ns, current.st_ctime_ns,
                )
                try:
                    current_entry = os.stat(
                        source_name, dir_fd=source_parent, follow_symlinks=False,
                    )
                except FileNotFoundError as error:
                    raise WorkspaceError(
                        "WORKSPACE_FILE_CHANGED",
                        "远程文件已在复制期间发生变化，请刷新后重试。",
                    ) from error
                entry_identity = (
                    current_entry.st_dev, current_entry.st_ino,
                    current_entry.st_size, current_entry.st_mtime_ns,
                    current_entry.st_ctime_ns,
                )
                if current_identity != source_identity or entry_identity != source_identity:
                    raise WorkspaceError(
                        "WORKSPACE_FILE_CHANGED",
                        "远程文件已在复制期间发生变化，请刷新后重试。",
                    )
            mode = stat_module.S_IMODE(source_attributes.st_mode) & 0o777
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            for _ in range(16):
                temporary = f".hetero-workspace-{secrets.token_hex(12)}.tmp"
                try:
                    target_fd = os.open(temporary, create_flags, mode, dir_fd=target_parent)
                    temporary_token = self._track_temporary(target_parent, temporary)
                    break
                except FileExistsError:
                    temporary = ""
            if target_fd < 0:
                raise WorkspaceError("WORKSPACE_WRITE_FAILED", "无法建立安全的复制临时文件。")
            os.fchmod(target_fd, mode)
            while True:
                self._raise_if_cancelled()
                chunk = os.read(source_fd, 256 * 1024)
                if not chunk:
                    break
                if copied_digest is not None:
                    copied_digest.update(chunk)
                offset = 0
                while offset < len(chunk):
                    offset += os.write(target_fd, chunk[offset:])
            os.fsync(target_fd)
            verify_source_unchanged()
            if (copied_digest is not None
                    and "sha256:" + copied_digest.hexdigest() != expected_revision):
                raise WorkspaceError(
                    "WORKSPACE_FILE_CHANGED",
                    "远程文件已在复制期间发生变化，请刷新后重试。",
                )
            os.close(target_fd)
            target_fd = -1
            self._verify_parent_fd(target_parent, destination)
            verify_source_unchanged()
            self._raise_if_cancelled()
            try:
                os.link(temporary, target_name, src_dir_fd=target_parent,
                        dst_dir_fd=target_parent, follow_symlinks=False)
            except FileExistsError as error:
                raise WorkspaceError("WORKSPACE_FILE_EXISTS", "复制目标已存在。") from error
            os.unlink(temporary, dir_fd=target_parent)
            temporary = ""
            try:
                os.fsync(target_parent)
            except OSError:
                pass
            copied = os.stat(target_name, dir_fd=target_parent, follow_symlinks=False)
            return {"path": destination, "sourcePath": source, "copied": True, **metadata(copied)}
        finally:
            if source_fd >= 0:
                os.close(source_fd)
            if target_fd >= 0:
                os.close(target_fd)
            if temporary_token:
                self._release_temporary(temporary_token, remove=bool(temporary))
            elif temporary:
                try:
                    os.unlink(temporary, dir_fd=target_parent)
                except OSError:
                    pass
            for descriptor in reversed(locks):
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                finally:
                    os.close(descriptor)
            os.close(source_parent)
            os.close(target_parent)

    def writeText(self, params: Dict[str, Any]) -> Dict[str, Any]:
        self._raise_if_cancelled()
        relative = self._relative(params.get("path"), allow_root=False)
        data = encode_text_document(params.get("content"), params.get("bom", False), params.get("lineEnding", "lf"))
        if len(data) > self.max_text_bytes:
            raise WorkspaceError("WORKSPACE_FILE_TOO_LARGE", f"远程文本文件超过 {self.max_text_bytes} 字节限制。")
        create = params.get("create", False)
        if not isinstance(create, bool):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "writeText.create 必须是布尔值。")
        parent_fd, basename = self._open_parent_fd(relative)
        lock_fd = -1
        descriptor = -1
        temporary = ""
        temporary_token = ""
        try:
            lock_fd = self._acquire_write_lock(relative)
            self._raise_if_cancelled()
            self._verify_parent_fd(parent_fd, relative)
            try:
                target_attributes = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
                exists = True
            except FileNotFoundError:
                target_attributes = None
                exists = False
            if exists and target_attributes is not None and stat_module.S_ISLNK(target_attributes.st_mode):
                raise WorkspaceError("WORKSPACE_SYMLINK_WRITE_BLOCKED", "为避免覆盖符号链接目标，已拒绝保存。")
            if exists and target_attributes is not None and not stat_module.S_ISREG(target_attributes.st_mode):
                raise WorkspaceError("WORKSPACE_NOT_FILE", "所选 Workspace 路径不是普通文件。")
            if create and exists:
                raise WorkspaceError("WORKSPACE_FILE_EXISTS", "目标文件已存在，无法按新文件创建。")
            if not create and not exists:
                raise WorkspaceError("WORKSPACE_CREATE_REQUIRED", "目标文件不存在；创建新文件必须指定 create: true。")

            baseline: Optional[str] = None
            mode = 0o600
            if exists:
                current, current_stat = self._read_bytes_at(parent_fd, basename)
                baseline = revision(current)
                mode = stat_module.S_IMODE(current_stat.st_mode) & 0o777
                if "expectedRevision" not in params:
                    raise WorkspaceError("WORKSPACE_REVISION_REQUIRED", "覆盖已有文件必须提供 expectedRevision。")
                if params.get("expectedRevision") != baseline:
                    raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件已发生变化，请重新加载后再保存。")
                if "bom" not in params or "lineEnding" not in params:
                    raise WorkspaceError(
                        "WORKSPACE_TEXT_METADATA_REQUIRED",
                        "覆盖已有文件必须显式传回读取时的 bom 与 lineEnding。",
                    )
            elif params.get("expectedRevision") is not None:
                raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件已被删除或尚不存在。")

            self._raise_if_cancelled()
            temporary_flags = (
                os.O_WRONLY | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            )
            for _ in range(16):
                temporary = f".hetero-workspace-{secrets.token_hex(12)}.tmp"
                try:
                    descriptor = os.open(temporary, temporary_flags, mode, dir_fd=parent_fd)
                    temporary_token = self._track_temporary(parent_fd, temporary)
                    break
                except FileExistsError:
                    temporary = ""
            if descriptor < 0:
                raise WorkspaceError("WORKSPACE_WRITE_FAILED", "无法建立安全的临时文件。")
            self._verify_fd_inside(descriptor)
            if exists:
                os.fchmod(descriptor, mode)
            output = os.fdopen(descriptor, "wb", closefd=True)
            descriptor = -1  # fdopen owns the descriptor even when write/fsync fails.
            with output:
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._raise_if_cancelled()

            try:
                latest_attributes = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
                latest_exists = True
            except FileNotFoundError:
                latest_attributes = None
                latest_exists = False
            if latest_exists != exists or (
                latest_attributes is not None and stat_module.S_ISLNK(latest_attributes.st_mode)
            ):
                raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件已在保存期间发生变化。")
            if latest_exists:
                latest, _ = self._read_bytes_at(parent_fd, basename)
                if revision(latest) != baseline:
                    raise WorkspaceError("WORKSPACE_FILE_CHANGED", "远程文件已在保存期间发生变化。")
            self._verify_parent_fd(parent_fd, relative)
            self._raise_if_cancelled()
            try:
                if create:
                    os.link(
                        temporary, basename,
                        src_dir_fd=parent_fd, dst_dir_fd=parent_fd, follow_symlinks=False,
                    )
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                        temporary = ""
                    except OSError:
                        # The target hard-link is already committed. Cleanup is
                        # retried in finally without turning success into a lie.
                        pass
                else:
                    os.replace(temporary, basename, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                    temporary = ""
            except OSError as error:
                if create and error.errno == errno.EEXIST:
                    raise WorkspaceError("WORKSPACE_FILE_EXISTS", "目标文件已存在，无法按新文件创建。") from error
                raise WorkspaceError(
                    "WORKSPACE_ATOMIC_RENAME_FAILED",
                    f"远程服务器无法原子替换文件：{error}",
                ) from error
            try:
                os.fsync(parent_fd)
            except OSError:
                pass
            attributes = os.stat(basename, dir_fd=parent_fd, follow_symlinks=False)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary_token:
                tracked = self._release_temporary(temporary_token, remove=bool(temporary))
                temporary_token = ""
                if temporary and not tracked:
                    try:
                        os.unlink(temporary, dir_fd=parent_fd)
                    except OSError:
                        pass
            elif temporary:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except OSError:
                    pass
            if lock_fd >= 0:
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_UN)
                finally:
                    os.close(lock_fd)
            os.close(parent_fd)
        return {
            "path": relative, "saved": True, "encoding": "utf8",
            "bom": params.get("bom", False), "lineEnding": params.get("lineEnding", "lf"),
            **metadata(attributes), "revision": revision(data),
        }

    def search(self, params: Dict[str, Any]) -> Dict[str, Any]:
        query = params.get("query")
        if not isinstance(query, str) or not query or len(query.encode("utf-8")) > 4096:
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "search.query 必须是非空短文本。")
        relative, start = self._existing(params.get("path", ""))
        case_sensitive = bool(params.get("caseSensitive", False))
        use_regex = params.get("regex", False)
        if not isinstance(use_regex, bool):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "search.regex 必须是布尔值。")
        flags = 0 if case_sensitive else re.IGNORECASE
        expression = None
        if use_regex:
            try:
                expression = re.compile(query, flags)
            except re.error as error:
                raise WorkspaceError("WORKSPACE_INVALID_REGEX", f"搜索正则表达式无效：{error}") from error
        needle = query if case_sensitive else query.casefold()
        max_results = bounded_integer(params.get("maxResults"), 200, 1, MAX_SEARCH_RESULTS)
        max_files = bounded_integer(params.get("maxFiles"), 5000, 1, MAX_SEARCH_FILES)
        max_file_bytes = bounded_integer(params.get("maxFileBytes"), self.max_text_bytes, 1024, self.max_text_bytes)
        time_budget_ms = bounded_integer(params.get("timeBudgetMs"), 10000, 100, 30000)
        includes = params.get("include", [])
        if "glob" in params:
            glob = params.get("glob")
            if isinstance(glob, str):
                includes = [glob]
            elif isinstance(glob, list):
                includes = glob
            else:
                raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "search.glob 必须是字符串或字符串数组。")
        excludes = params.get("exclude", [])
        if not isinstance(includes, list) or not all(isinstance(value, str) for value in includes[:64]):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "search.include 必须是 glob 字符串数组。")
        if not isinstance(excludes, list) or not all(isinstance(value, str) for value in excludes[:64]):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "search.exclude 必须是 glob 字符串数组。")
        includes = includes[:64]
        excludes = excludes[:64]
        started = time.monotonic()
        results: List[Dict[str, Any]] = []
        regex_candidates: List[str] = []
        scanned = 0
        skipped = 0
        reason: Optional[str] = None

        def candidates() -> Iterable[Tuple[str, str]]:
            if os.path.isfile(start):
                yield relative, start
                return
            for directory, names, files in os.walk(start, followlinks=False):
                names[:] = [name for name in names if name not in DEFAULT_SKIPPED_DIRECTORIES
                            and not os.path.islink(os.path.join(directory, name))]
                for name in files:
                    filename = os.path.join(directory, name)
                    if os.path.islink(filename):
                        continue
                    candidate = os.path.relpath(filename, self.root).replace(os.sep, "/")
                    yield candidate, filename

        for candidate, filename in candidates():
            if self._cancelled():
                reason = "cancelled"
                break
            if (time.monotonic() - started) * 1000 >= time_budget_ms:
                reason = "time_budget"
                break
            if scanned >= max_files:
                reason = "file_limit"
                break
            if includes and not any(workspace_glob_matches(candidate, pattern) for pattern in includes):
                continue
            if excludes and any(workspace_glob_matches(candidate, pattern) for pattern in excludes):
                continue
            scanned += 1
            if use_regex:
                regex_candidates.append(candidate)
                continue
            try:
                data, attributes = self._read_bytes(filename, max_file_bytes)
                if b"\0" in data:
                    skipped += 1
                    continue
                text = data.decode("utf-8", errors="strict")
            except (WorkspaceError, UnicodeDecodeError):
                skipped += 1
                continue
            file_revision = revision(data)
            file_metadata = metadata(attributes)
            for number, line in enumerate(text.splitlines(), start=1):
                if expression is not None:
                    match = expression.search(line)
                    offset = match.start() if match else -1
                else:
                    haystack = line if case_sensitive else line.casefold()
                    offset = haystack.find(needle)
                if offset < 0:
                    continue
                results.append({
                    "path": candidate, "line": number, "column": offset + 1,
                    "preview": line[:500], "revision": file_revision,
                    "size": file_metadata["size"], "mtimeMs": file_metadata["mtimeMs"],
                })
                if len(results) >= max_results:
                    reason = "result_limit"
                    break
            if reason == "result_limit":
                break
        if use_regex and regex_candidates and reason not in ("cancelled", "time_budget"):
            remaining = time_budget_ms / 1000 - (time.monotonic() - started)
            if remaining <= 0:
                reason = "time_budget"
            else:
                payload = json.dumps({
                    "root": self.root,
                    "files": regex_candidates,
                    "query": query,
                    "caseSensitive": case_sensitive,
                    "maxFileBytes": max_file_bytes,
                    "maxResults": max_results,
                }, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
                helper = subprocess.Popen(
                    [sys.executable, "-I", "-c", REGEX_SEARCH_HELPER],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                request_id = getattr(self._request_context, "request_id", "unknown")
                with self._process_lock:
                    self._processes[request_id] = helper
                input_payload: Optional[bytes] = payload
                try:
                    while True:
                        if self._cancelled():
                            self._kill_process(helper)
                            raise WorkspaceError("WORKSPACE_RPC_CANCELLED", "正则搜索已取消。")
                        remaining = time_budget_ms / 1000 - (time.monotonic() - started)
                        if remaining <= 0:
                            self._kill_process(helper)
                            reason = "time_budget"
                            break
                        try:
                            stdout, stderr = helper.communicate(
                                input=input_payload, timeout=min(0.1, remaining)
                            )
                            input_payload = None
                            if helper.returncode != 0:
                                detail = stderr.decode("utf-8", errors="replace")[-1000:]
                                raise WorkspaceError(
                                    "WORKSPACE_SEARCH_FAILED",
                                    f"隔离正则搜索失败：{detail or f'exit {helper.returncode}'}",
                                )
                            try:
                                isolated = json.loads(stdout.decode("ascii"))
                            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                                raise WorkspaceError(
                                    "WORKSPACE_SEARCH_FAILED", "隔离正则搜索返回无效结果。"
                                ) from error
                            results = isolated.get("matches", [])
                            scanned = int(isolated.get("scanned", 0))
                            skipped = int(isolated.get("skipped", 0))
                            if isolated.get("limited"):
                                reason = "result_limit"
                            break
                        except subprocess.TimeoutExpired:
                            input_payload = None
                            continue
                finally:
                    if helper.poll() is None:
                        self._kill_process(helper)
                    try:
                        helper.wait(timeout=1)
                    except subprocess.TimeoutExpired:
                        pass
                    with self._process_lock:
                        self._processes.pop(request_id, None)
        return {
            "query": query, "path": relative, "matches": results,
            "truncated": reason is not None, "reason": reason,
            "stats": {"scannedFiles": scanned, "skippedFiles": skipped,
                      "durationMs": int((time.monotonic() - started) * 1000)},
        }

    def _kill_process(self, process: subprocess.Popen[bytes]) -> None:
        # The shell leader may exit while a background child still owns the
        # same process group and stdout/stderr pipes. Always target the saved
        # PGID; ESRCH is the normal already-cleaned case.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, ProcessLookupError, PermissionError):
            if process.poll() is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass

    def exec(self, params: Dict[str, Any]) -> Dict[str, Any]:
        command = params.get("command")
        if not isinstance(command, str) or not command or len(command.encode("utf-8")) > MAX_EXEC_COMMAND_BYTES:
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "exec.command 必须是非空 shell 命令。")
        cwd_relative, cwd = self._existing(params.get("cwd", ""))
        if not os.path.isdir(cwd):
            raise WorkspaceError("WORKSPACE_NOT_DIRECTORY", "exec.cwd 不是 Workspace 内的目录。")
        timeout_ms = bounded_integer(params.get("timeoutMs"), 30000, 100, 30 * 60 * 1000)
        output_limit = bounded_integer(params.get("maxOutputBytes"), 4 * 1024 * 1024, 1024, MAX_EXEC_OUTPUT_BYTES)
        if self._cancelled():
            raise WorkspaceError("WORKSPACE_RPC_CLOSING", "远端文件服务正在关闭。")
        started = time.monotonic()
        process = subprocess.Popen(
            command, shell=True, executable="/bin/bash", cwd=cwd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
        )
        request_id = getattr(self._request_context, "request_id", "unknown")
        with self._process_lock:
            self._processes[request_id] = process
        output = {"stdout": bytearray(), "stderr": bytearray()}
        selector = selectors.DefaultSelector()
        try:
            assert process.stdout is not None and process.stderr is not None
            os.set_blocking(process.stdout.fileno(), False)
            os.set_blocking(process.stderr.fileno(), False)
            selector.register(process.stdout, selectors.EVENT_READ, "stdout")
            selector.register(process.stderr, selectors.EVENT_READ, "stderr")
            deadline = started + timeout_ms / 1000
            while selector.get_map():
                if self._cancelled():
                    self._kill_process(process)
                    raise WorkspaceError("WORKSPACE_RPC_CANCELLED", "远端命令已取消。")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self._kill_process(process)
                    raise WorkspaceError("WORKSPACE_EXEC_TIMEOUT", f"远端命令超过 {timeout_ms} ms。")
                events = selector.select(min(0.1, remaining))
                if not events and process.poll() is not None:
                    # Pipes may still carry buffered data; a zero-time select
                    # on the next pass drains it before completion.
                    events = selector.select(0)
                    if not events:
                        break
                for key, _ in events:
                    chunk = os.read(key.fileobj.fileno(), 64 * 1024)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    output[key.data].extend(chunk)
                    if len(output["stdout"]) + len(output["stderr"]) > output_limit:
                        self._kill_process(process)
                        raise WorkspaceError("WORKSPACE_EXEC_OUTPUT_LIMIT", f"远端命令输出超过 {output_limit} 字节限制。")
            try:
                exit_code = process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired as error:
                self._kill_process(process)
                raise WorkspaceError("WORKSPACE_EXEC_TIMEOUT", f"远端命令超过 {timeout_ms} ms。") from error
        finally:
            selector.close()
            if process.poll() is None:
                self._kill_process(process)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    pass
            with self._process_lock:
                self._processes.pop(request_id, None)
        return {
            "stdout": bytes(output["stdout"]).decode("utf-8", errors="replace"),
            "stderr": bytes(output["stderr"]).decode("utf-8", errors="replace"),
            "exitCode": exit_code, "signal": None,
            "durationMs": int((time.monotonic() - started) * 1000), "cwd": cwd_relative,
        }

    def dispatch(self, method: str, params: Dict[str, Any], request_id: str,
                 cancel_event: threading.Event) -> Dict[str, Any]:
        handlers = {
            "ping": self.ping,
            "stat": self.stat, "fs.stat": self.stat,
            "list": self.list, "fs.list": self.list,
            "prefetch": self.prefetch, "fs.prefetch": self.prefetch,
            "readText": self.readText, "fs.readText": self.readText,
            "readChunk": self.readChunk, "fs.readChunk": self.readChunk,
            "writeText": self.writeText, "fs.writeText": self.writeText,
            "rename": self.rename, "fs.rename": self.rename,
            "copy": self.copy, "fs.copy": self.copy,
            "search": self.search, "fs.search": self.search,
            "exec": self.exec, "proc.exec": self.exec,
        }
        handler = handlers.get(method)
        if handler is None:
            raise WorkspaceError("WORKSPACE_METHOD_NOT_FOUND", f"不支持的 Workspace 方法：{method}")
        if not isinstance(params, dict):
            raise WorkspaceError("WORKSPACE_INVALID_REQUEST", "params 必须是对象。")
        validate_json_unicode(params)
        self._request_context.request_id = request_id
        self._request_context.cancel_event = cancel_event
        try:
            return handler(params)
        finally:
            self._request_context.request_id = None
            self._request_context.cancel_event = None


class FrameServer:
    def __init__(self, service: WorkspaceService, input_stream: BinaryIO, output_stream: BinaryIO, workers: int):
        self.service = service
        self.input = input_stream
        self.output = output_stream
        self.output_lock = threading.Lock()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="workspace-rpc")
        self.futures: set[concurrent.futures.Future[Any]] = set()
        self.futures_lock = threading.Lock()
        self.cancellations: Dict[str, threading.Event] = {}
        self.cancellations_lock = threading.Lock()
        self.service.attach_cancellations(self.cancellations, self.cancellations_lock)

    def _read_exact(self, length: int) -> bytes:
        chunks: List[bytes] = []
        remaining = length
        while remaining:
            chunk = self.input.read(remaining)
            if not chunk:
                raise EOFError("unexpected EOF inside request frame")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read_request(self) -> Optional[Dict[str, Any]]:
        header = self.input.readline(MAX_HEADER_BYTES + 1)
        if not header:
            return None
        if len(header) > MAX_HEADER_BYTES or not header.endswith(b"\n") or not header.startswith(REQUEST_HEADER):
            raise WorkspaceError("WORKSPACE_RPC_INVALID_FRAME", "收到无效的 Workspace 请求帧。")
        raw_length = header[len(REQUEST_HEADER):-1]
        if not raw_length.isdigit():
            raise WorkspaceError("WORKSPACE_RPC_INVALID_FRAME", "Workspace 请求帧长度无效。")
        length = int(raw_length)
        if length > MAX_REQUEST_BYTES:
            raise WorkspaceError("WORKSPACE_RPC_REQUEST_TOO_LARGE", "Workspace 请求帧过大。")
        try:
            request = json.loads(self._read_exact(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise WorkspaceError("WORKSPACE_RPC_INVALID_FRAME", "Workspace 请求不是有效 JSON。") from error
        if not isinstance(request, dict) or request.get("schema_version") != SCHEMA_VERSION:
            raise WorkspaceError("WORKSPACE_RPC_INVALID_REQUEST", "Workspace 请求协议版本无效。")
        request_id = request.get("id")
        method = request.get("method")
        if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9._:-]{1,128}", request_id):
            raise WorkspaceError("WORKSPACE_RPC_INVALID_REQUEST", "Workspace 请求 ID 无效。")
        if not isinstance(method, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9.]{0,63}", method):
            raise WorkspaceError("WORKSPACE_RPC_INVALID_REQUEST", "Workspace 请求方法无效。")
        return request

    def write_response(self, response: Dict[str, Any]) -> None:
        try:
            body = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        except UnicodeEncodeError:
            body = json.dumps(response, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(body) > MAX_RESPONSE_BYTES:
            body = json.dumps({
                "schema_version": SCHEMA_VERSION, "id": str(response.get("id", "unknown")), "ok": False,
                "error": {"code": "WORKSPACE_RPC_RESPONSE_TOO_LARGE", "message": "Workspace 响应超过协议限制。"},
            }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        frame = RESPONSE_HEADER + str(len(body)).encode("ascii") + b"\n" + body
        with self.output_lock:
            self.output.write(frame)
            self.output.flush()

    def handle(self, request: Dict[str, Any]) -> None:
        request_id = request["id"]
        queued_at = request.pop("_queued_at", time.monotonic())
        queue_wait_ms = max(0, int((time.monotonic() - queued_at) * 1000))
        with self.cancellations_lock:
            cancel_event = self.cancellations.setdefault(request_id, threading.Event())
        try:
            result = self.service.dispatch(request["method"], request.get("params", {}), request_id, cancel_event)
            if request["method"] in ("exec", "proc.exec"):
                result["queueWaitMs"] = queue_wait_ms
            response = {"schema_version": SCHEMA_VERSION, "id": request_id, "ok": True, "result": result}
        except WorkspaceError as error:
            detail = {"code": error.code, "message": error.message}
            if error.details is not None:
                detail["details"] = error.details
            response = {"schema_version": SCHEMA_VERSION, "id": request_id, "ok": False, "error": detail}
        except Exception as error:  # Keep tracebacks off the protocol stream.
            print(f"workspace worker internal error: {type(error).__name__}: {error}", file=sys.stderr, flush=True)
            response = {
                "schema_version": SCHEMA_VERSION, "id": request_id, "ok": False,
                "error": {"code": "WORKSPACE_INTERNAL_ERROR", "message": "远端文件服务内部错误。"},
            }
        finally:
            with self.cancellations_lock:
                self.cancellations.pop(request_id, None)
        self.write_response(response)

    def handle_cancel(self, request: Dict[str, Any]) -> None:
        params = request.get("params", {})
        if not isinstance(params, dict):
            self.write_response({
                "schema_version": SCHEMA_VERSION, "id": request["id"], "ok": False,
                "error": {"code": "WORKSPACE_INVALID_REQUEST", "message": "rpc.cancel.params 必须是对象。"},
            })
            return
        target = params.get("requestId")
        cancelled = isinstance(target, str) and self.service.cancel(target)
        self.write_response({
            "schema_version": SCHEMA_VERSION, "id": request["id"], "ok": True,
            "result": {"requestId": target, "cancelled": cancelled},
        })

    def future_done(self, future: concurrent.futures.Future[Any]) -> None:
        with self.futures_lock:
            self.futures.discard(future)

    def run(self) -> None:
        self.output.write(READY_LINE)
        self.output.flush()
        try:
            while True:
                request = self.read_request()
                if request is None:
                    break
                if request["method"] == "rpc.cancel":
                    self.handle_cancel(request)
                    continue
                with self.cancellations_lock:
                    if request["id"] in self.cancellations:
                        self.write_response({
                            "schema_version": SCHEMA_VERSION, "id": request["id"], "ok": False,
                            "error": {"code": "WORKSPACE_RPC_DUPLICATE_ID", "message": "同一请求 ID 已在执行。"},
                        })
                        continue
                    self.cancellations[request["id"]] = threading.Event()
                with self.futures_lock:
                    if len(self.futures) >= MAX_PENDING_REQUESTS:
                        with self.cancellations_lock:
                            self.cancellations.pop(request["id"], None)
                        self.write_response({
                            "schema_version": SCHEMA_VERSION, "id": request["id"], "ok": False,
                            "error": {
                                "code": "WORKSPACE_RPC_BUSY",
                                "message": "远端文件服务待处理请求过多，请稍后重试。",
                            },
                        })
                        continue
                    request["_queued_at"] = time.monotonic()
                    future = self.pool.submit(self.handle, request)
                    self.futures.add(future)
                future.add_done_callback(self.future_done)
        finally:
            self.service.close()
            self.pool.shutdown(wait=True, cancel_futures=True)


def parse_arguments(argv: List[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Heterogeneous Workbench workspace worker")
    parser.add_argument("--stdio-server", action="store_true", help="serve framed RPC over stdin/stdout")
    parser.add_argument("--workspace-root", required=True)
    parser.add_argument("--max-text-bytes", type=int, default=1024 * 1024)
    parser.add_argument("--workers", type=int, default=8)
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    arguments = parse_arguments(sys.argv[1:] if argv is None else argv)
    if not arguments.stdio_server:
        raise SystemExit("--stdio-server is required")
    wire_output = sys.stdout.buffer
    # Any accidental print from future helper code becomes bounded SSH stderr,
    # never an ambiguous protocol fragment on stdout.
    sys.stdout = sys.stderr
    service = WorkspaceService(arguments.workspace_root, arguments.max_text_bytes)

    # Start the shutdown observer before installing handlers. Python signal
    # callbacks run on the main thread and must not acquire application locks or
    # create threads: a signal can interrupt that same thread while a lock is
    # held. The handler writes one byte to this private pipe; the existing
    # daemon performs all blocking cleanup outside signal context.
    shutdown_read_fd, shutdown_write_fd = os.pipe()
    os.set_blocking(shutdown_write_fd, False)
    shutdown_signal = [int(getattr(signal, "SIGTERM", 15))]
    shutdown_notified = [False]

    def shutdown_watchdog() -> None:
        try:
            requested = os.read(shutdown_read_fd, 1)
        except OSError:
            return
        if not requested:
            return
        service.close()
        # Normal SSH teardown closes stdin too, allowing FrameServer and write
        # handlers to finish. Keep a bounded grace period, then remove any temp
        # file still tracked by a blocked handler before forcing process exit.
        time.sleep(1.0)
        deadline = time.monotonic() + 0.2
        while not service.cleanup_temporary_files(blocking=False) and time.monotonic() < deadline:
            time.sleep(0.01)
        os._exit(128 + shutdown_signal[0])

    shutdown_thread = threading.Thread(
        target=shutdown_watchdog, name="workspace-shutdown", daemon=True,
    )
    shutdown_thread.start()

    def stop_service(signum: int, _frame: Any) -> None:
        if shutdown_notified[0]:
            return
        shutdown_notified[0] = True
        shutdown_signal[0] = signum
        try:
            os.write(shutdown_write_fd, b"x")
        except (BlockingIOError, OSError):
            # The pipe already contains a shutdown byte or is closing. The
            # prestarted watchdog will still enforce the deadline.
            pass

    for signal_name in ("SIGTERM", "SIGHUP"):
        value = getattr(signal, signal_name, None)
        if value is not None:
            signal.signal(value, stop_service)
    server = FrameServer(
        service, sys.stdin.buffer, wire_output,
        bounded_integer(arguments.workers, 8, 1, 16),
    )
    try:
        server.run()
    finally:
        try:
            os.close(shutdown_write_fd)
        except OSError:
            pass
        shutdown_thread.join(timeout=0.2)
        try:
            os.close(shutdown_read_fd)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

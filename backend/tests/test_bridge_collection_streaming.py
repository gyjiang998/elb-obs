"""Streaming collector contract tests; no device connection is made."""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from backend import app as platform_app
from backend.core import PlatformError

BRIDGE_PATH = Path(__file__).resolve().parents[2] / "device-bridge" / "hdc_device_bridge.py"
SPEC = importlib.util.spec_from_file_location("bridge_collection_streaming", BRIDGE_PATH)
assert SPEC is not None and SPEC.loader is not None
BRIDGE_MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BRIDGE_MODULE)


class StreamRequest:
    def __init__(self, chunks, *, digest: str, content_length: int | None = None, failure: BaseException | None = None):
        self.headers = {"X-Bridge-Id": "bridge_test", "X-Bridge-Secret": "secret", "X-Artifact-Sha256": digest}
        if content_length is not None:
            self.headers["Content-Length"] = str(content_length)
        self.chunks = chunks
        self.failure = failure
        self.read_count = 0

    async def body(self):
        raise AssertionError("An uploaded trace must not be buffered with request.body()")

    async def stream(self):
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk
        if self.failure:
            raise self.failure
        yield b""


class BridgeCollectionServerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.gateway = Mock()
        self.job_id = "devjob_" + "a" * 24
        self.patch_root = patch.object(platform_app, "STATE_ROOT", self.root)
        self.patch_gateway = patch.object(platform_app, "device_gateway", self.gateway)
        self.patch_root.start()
        self.patch_gateway.start()
        self.addCleanup(self.patch_root.stop)
        self.addCleanup(self.patch_gateway.stop)

    def upload(self, request):
        return asyncio.run(platform_app.upload_bridge_collected_artifact(self.job_id, request))

    def test_streams_more_than_old_32_mib_limit_with_atomic_visibility(self):
        block = b"x" * (1024 * 1024)
        digest = hashlib.sha256()
        for _ in range(33):
            digest.update(block)
        count = 0

        def chunks():
            nonlocal count
            for _ in range(33):
                # A reader must never observe a partially received final file.
                self.assertEqual(list(self.root.rglob("*.bin")), [])
                count += 1
                yield block

        request = StreamRequest(chunks(), digest=digest.hexdigest(), content_length=33 * len(block))
        result = self.upload(request)
        final = self.root / "device-gateway" / "bridge-collected" / f"{self.job_id}-{digest.hexdigest()[:16]}.bin"
        self.assertEqual(result, {"status": "stored", "job_id": self.job_id, "sha256": digest.hexdigest(), "bytes": 33 * len(block)})
        self.assertEqual(final.stat().st_size, result["bytes"])
        self.assertEqual(count, 33)
        self.assertFalse(list(final.parent.glob("*.upload")))
        self.gateway.authenticate_bridge.assert_called_once_with("bridge_test", "secret")
        self.gateway.verify_job_owner.assert_called_once_with(job_id=self.job_id, bridge_id="bridge_test")

    def test_rejects_declared_oversize_before_reading_request(self):
        request = StreamRequest([b"never-read"], digest="a" * 64, content_length=101)
        with patch.object(platform_app, "MAX_UPLOAD_BYTES", 100), self.assertRaises(PlatformError) as raised:
            self.upload(request)
        self.assertEqual(raised.exception.code, "BRIDGE_COLLECT_TOO_LARGE")
        self.assertEqual(request.read_count, 0)
        self.assertEqual(list(self.root.rglob("*.bin")), [])

    def test_chunked_oversize_disconnect_cancel_and_digest_failure_remove_partial_files(self):
        cases = [
            (StreamRequest([b"abc", b"def"], digest=hashlib.sha256(b"abcdef").hexdigest()), PlatformError, 5),
            (StreamRequest([b"abc"], digest="a" * 64), PlatformError, 100),
            (StreamRequest([b"abc"], digest=hashlib.sha256(b"abc").hexdigest(), content_length=4), PlatformError, 100),
            (StreamRequest([b"abc"], digest="a" * 64, failure=ConnectionError("disconnected")), ConnectionError, 100),
            (StreamRequest([b"abc"], digest="a" * 64, failure=asyncio.CancelledError()), asyncio.CancelledError, 100),
        ]
        for request, exception, limit in cases:
            with self.subTest(exception=exception, limit=limit), patch.object(platform_app, "MAX_UPLOAD_BYTES", limit), self.assertRaises(exception):
                self.upload(request)
            collected = self.root / "device-gateway" / "bridge-collected"
            self.assertEqual(list(collected.iterdir()), [])

    def test_unauthorized_upload_does_not_consume_stream_or_create_file(self):
        self.gateway.authenticate_bridge.side_effect = PlatformError("UNAUTHORIZED", "no", status_code=403)
        request = StreamRequest([b"abc"], digest="a" * 64)
        with self.assertRaises(PlatformError):
            self.upload(request)
        self.assertEqual(request.read_count, 0)
        self.assertEqual(list(self.root.iterdir()), [])


class BridgeCollectionClientTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.hdc = self.root / "hdc.exe"
        self.hdc.write_bytes(b"placeholder")
        self.bridge = BRIDGE_MODULE.HdcDeviceBridge({"bridge_id": "bridge_" + "b" * 24, "bridge_secret": "secret",
            "platform_http_url": "http://127.0.0.1", "platform_ws_url": "ws://127.0.0.1/v1/device-bridges/ws",
            "hdc_path": str(self.hdc), "spool_root": str(self.root / "spool")}, self.root / "bridge.json")
        self.job = {"job_id": "devjob_" + "b" * 24, "payload": {"device_serial": "test-phone", "remote_path": "/data/local/tmp/trace.ftrace", "timeout_seconds": 600}}

    def test_upload_streams_file_over_old_bridge_limit_to_real_http_receiver(self):
        received = {}
        job_id = self.job["job_id"]

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers["Content-Length"])
                digest = hashlib.sha256()
                read = 0
                while read < size:
                    chunk = self.rfile.read(min(64 * 1024, size - read))
                    if not chunk:
                        break
                    read += len(chunk)
                    digest.update(chunk)
                received.update(bytes=read, sha256=digest.hexdigest(), declared_hash=self.headers["X-Artifact-Sha256"], transfer_encoding=self.headers.get("Transfer-Encoding"))
                body = json.dumps({"status": "stored", "job_id": job_id, "bytes": read, "sha256": digest.hexdigest()}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.bridge.platform_http_url = f"http://127.0.0.1:{server.server_port}"
        size = 129 * 1024 * 1024 + 123

        async def receive(*args, **kwargs):
            with Path(args[-1]).open("wb") as handle:
                handle.truncate(size)  # Sparse fixture, bounded test memory.
            return {"return_code": 0}

        with patch.object(self.bridge, "_run_hdc", new=receive), patch.object(Path, "read_bytes", side_effect=AssertionError("full-file buffering forbidden")):
            result = asyncio.run(self.bridge._collect(self.job))
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(result["bytes"], size)
        self.assertEqual(received["bytes"], size)
        self.assertEqual(received["sha256"], result["sha256"])
        self.assertEqual(received["declared_hash"], result["sha256"])
        self.assertIsNone(received["transfer_encoding"])
        self.assertEqual(result["transfer_spool_cleanup"], "completed")
        self.assertFalse((self.bridge.spool_root / self.job["job_id"] / "collected.bin").exists())

    def test_rejects_file_over_limit_before_network_upload(self):
        async def receive(*args, **kwargs):
            Path(args[-1]).write_bytes(b"123456")
            return {"return_code": 0}

        with patch.object(self.bridge, "_run_hdc", new=receive), patch.object(BRIDGE_MODULE, "MAX_COLLECT_BYTES", 5), patch.object(BRIDGE_MODULE, "_direct_urlopen") as upload:
            result = asyncio.run(self.bridge._collect(self.job))
        self.assertEqual(result["status"], "failed")
        upload.assert_not_called()
        self.assertFalse((self.bridge.spool_root / self.job["job_id"] / "collected.bin").exists())


if __name__ == "__main__":
    unittest.main()

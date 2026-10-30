import io
import tempfile
from pathlib import Path
import unittest
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import socket
import threading
import time
from unittest.mock import Mock
from camera_http import serve_video, serve_file, TRANSFER_TIMEOUT


class Reply:
    def __init__(self, requested=None, rename=None):
        self.headers = {"Range": requested} if requested else {}
        self.output = {}
        self.wfile = io.BytesIO()
        self.rename = rename

    def send_response(self, status):
        self.status = status

    def send_header(self, name, value):
        self.output[name] = value

    def end_headers(self):
        if self.rename:
            self.rename()


class VideoHTTPTests(unittest.TestCase):
    def test_ranges_head_and_concurrent_trash_rename(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "video.mp4"
            path.write_bytes(b"0123456789")
            for requested, status, expected in [(None, 200, b"0123456789"), ("bytes=2-5", 206, b"2345"),
                                                 ("bytes=-3", 206, b"789"), ("bytes=9-", 206, b"9"),
                                                 ("bytes=10-", 416, b""), ("bytes=3-1", 416, b""),
                                                 ("bytes=-0", 416, b""),
                                                 ("bytes=0-1,3-4", 416, b"")]:
                reply = Reply(requested)
                serve_video(reply, path)
                self.assertEqual((reply.status, reply.wfile.getvalue()), (status, expected))
                self.assertEqual(int(reply.output["Content-Length"]), len(expected))
            reply = Reply("bytes=2-5")
            serve_video(reply, path, head=True)
            self.assertEqual(reply.wfile.getvalue(), b"")
            self.assertEqual(reply.output["Content-Length"], "4")
            reply = Reply(rename=lambda: path.rename(path.with_suffix(".trash")))
            serve_video(reply, path)
            self.assertEqual(reply.wfile.getvalue(), b"0123456789")

    def test_missing_file_sends_no_headers(self):
        reply = Reply()
        with self.assertRaises(FileNotFoundError):
            serve_video(reply, Path(tempfile.gettempdir()) / "missing-camera-range-fixture.mp4")
        self.assertFalse(hasattr(reply, "status"))

    def test_if_range_validators_and_generic_download_resume(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.zip'
            path.write_bytes(b'0123456789')
            initial = Reply()
            serve_file(initial, path, attachment='camera-events.zip')
            self.assertEqual(initial.output['Content-Type'], 'application/zip')
            self.assertIn('attachment', initial.output['Content-Disposition'])
            for validator in (initial.output['ETag'], initial.output['Last-Modified']):
                reply = Reply('bytes=4-')
                reply.headers['If-Range'] = validator
                serve_file(reply, path)
                self.assertEqual((reply.status, reply.wfile.getvalue()), (206, b'456789'))
            for validator in ('"stale"', 'W/' + initial.output['ETag'], 'invalid date'):
                reply = Reply('bytes=4-')
                reply.headers['If-Range'] = validator
                serve_file(reply, path)
                self.assertEqual((reply.status, reply.wfile.getvalue()), (200, b'0123456789'))
            path.write_bytes(b'new file contents')
            reply = Reply('bytes=4-')
            reply.headers['If-Range'] = initial.output['ETag']
            serve_file(reply, path)
            self.assertEqual(reply.status, 200)
            self.assertNotEqual(reply.output['ETag'], initial.output['ETag'])

    def test_timeout_after_headers_is_not_propagated_as_second_response(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'video.mp4'
            path.write_bytes(b'payload')
            for failure in (TimeoutError('stalled'), BrokenPipeError(), ConnectionResetError()):
                reply = Reply()
                reply.wfile = Mock()
                reply.wfile.write.side_effect = failure
                reply.connection = Mock(spec=['gettimeout', 'settimeout'])
                reply.connection.gettimeout.return_value = 20
                serve_video(reply, path)
                self.assertEqual(reply.status, 200)
                self.assertTrue(reply.close_connection)
                self.assertEqual(reply.connection.settimeout.call_args_list[0].args, (TRANSFER_TIMEOUT,))
                self.assertEqual(reply.connection.settimeout.call_args_list[-1].args, (20,))

    def test_native_transfer_survives_pause_and_restarts_with_exact_range(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'events.zip'
            payload = bytes(range(256)) * 8192
            path.write_bytes(payload)
            class Handler(BaseHTTPRequestHandler):
                def setup(self):
                    super().setup()
                    self.connection.settimeout(.15)
                    self.connection.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 8192)
                def do_GET(self):
                    serve_file(self, path)
                def log_message(self, *args):
                    pass
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=10)
            conn.connect()
            conn.sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32768)
            conn.request('GET', '/')
            response = conn.getresponse()
            etag = response.getheader('ETag')
            time.sleep(.5)  # Exceeds the API timeout while the send buffer is full.
            result = response.read()
            conn.close()
            self.assertEqual(hashlib.sha256(result).digest(), hashlib.sha256(payload).digest())
            # Simulate losing a connection mid-download, then resume on a new one.
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            conn.request('GET', '/')
            response = conn.getresponse()
            prefix = response.read(12345)
            conn.close()
            conn = http.client.HTTPConnection('127.0.0.1', server.server_port, timeout=5)
            conn.request('GET', '/', headers={'Range': 'bytes=12345-', 'If-Range': etag})
            response = conn.getresponse()
            self.assertEqual(response.status, 206)
            self.assertEqual(prefix + response.read(), payload)
            conn.close()

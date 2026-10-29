import io
import tempfile
from pathlib import Path
import unittest
from camera_http import serve_video


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

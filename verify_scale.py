"""Read/write only a temporary 10,000-item catalog, never production media."""
import json
from pathlib import Path
import tempfile
import time
from camera_media import MediaLibrary

with tempfile.TemporaryDirectory(prefix="camera-index-benchmark-") as directory:
    root = Path(directory)
    for index in range(10000):
        (root / f"event-20261029T000000_{index:06d}Z.jpg").write_bytes(b"fixture")
    library = MediaLibrary(root, "snapshot")
    began = time.monotonic()
    first = library.list("limit=48")
    cold = time.monotonic() - began
    began = time.monotonic()
    warm = library.list("limit=48")
    elapsed = time.monotonic() - began
    assert first["total"] == warm["total"] == 10000
    assert len(warm["items"]) == 48 and warm["next_before"]
    print(json.dumps({"items": 10000, "cold_seconds": round(cold, 4), "warm_seconds": round(elapsed, 4)}))

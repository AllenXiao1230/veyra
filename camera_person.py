"""Local COCO-person annotation using a checksum-pinned OpenCV Zoo YOLOX model."""
import hashlib
from pathlib import Path
import time

import cv2
import numpy as np

MODEL_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"


class PersonAnnotator:
    def __init__(self, model, threshold=0.5):
        self.ready = False
        self.reason = "AI 模型尚未載入"
        self.threshold = threshold
        self.inference_ms = None
        self.net = None
        self.last_retry = 0.0
        self.path = Path(model)
        grids, strides = [], []
        for stride in (8, 16, 32):
            ys, xs = np.mgrid[:640 // stride, :640 // stride]
            grids.append(np.column_stack((xs.ravel(), ys.ravel())))
            strides.append(np.full((xs.size, 1), stride, dtype=np.float32))
        self.grid = np.concatenate(grids).astype(np.float32)
        self.stride = np.concatenate(strides)
        self.load()

    def load(self):
        self.last_retry = time.monotonic()
        try:
            if hashlib.sha256(self.path.read_bytes()).hexdigest() != MODEL_SHA256:
                raise ValueError("AI 模型校驗不符")
            self.net = cv2.dnn.readNetFromONNX(str(self.path))
            self.net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
            self.net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
            self.ready = True
            self.reason = "人形 AI 已就緒；動作皆會記錄"
        except (OSError, ValueError, cv2.error) as error:
            self.net = None
            self.ready = False
            self.reason = str(error)[-200:]

    def detect(self, frame):
        if not self.ready:
            if time.monotonic() - self.last_retry > 60:
                self.load()
            if not self.ready:
                return False, None, 0
        began = time.monotonic()
        try:
            height, width = frame.shape[:2]
            ratio = min(640 / width, 640 / height)
            rw, rh = max(1, int(width * ratio)), max(1, int(height * ratio))
            canvas = np.full((640, 640, 3), 114, dtype=np.float32)
            canvas[:rh, :rw] = cv2.resize(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB), (rw, rh))
            self.net.setInput(np.transpose(canvas, (2, 0, 1))[None])
            predictions = self.net.forward().reshape(-1, 85)
            if predictions.shape[0] != self.grid.shape[0] or not np.isfinite(predictions).all():
                raise ValueError("AI 模型輸出格式不符")
            class_scores = predictions[:, 4:5] * predictions[:, 5:]
            # COCO person must be the winning class, not merely a nonzero score.
            ids = np.flatnonzero((class_scores[:, 0] >= self.threshold) & (class_scores.argmax(axis=1) == 0))
            if not len(ids):
                return False, None, 0
            chosen = predictions[ids, :4].copy()
            chosen[:, :2] = (chosen[:, :2] + self.grid[ids]) * self.stride[ids]
            chosen[:, 2:] = np.exp(np.clip(chosen[:, 2:], -12, 12)) * self.stride[ids]
            boxes = np.column_stack((chosen[:, :2] - chosen[:, 2:] / 2, chosen[:, 2:]))
            scores = class_scores[ids, 0]
            keep = np.asarray(cv2.dnn.NMSBoxes(boxes.tolist(), scores.tolist(), self.threshold, 0.45)).reshape(-1)
            return (True, float(scores[keep].max()), len(keep)) if len(keep) else (False, None, 0)
        except (ValueError, cv2.error) as error:
            self.ready = False
            self.last_retry = time.monotonic()
            self.reason = "AI 分類暫停：" + str(error)[-180:]
            return False, None, 0
        finally:
            self.inference_ms = round((time.monotonic() - began) * 1000, 1)

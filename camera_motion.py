"""Validated, atomically persisted motion thresholds; independent of AI labels."""
import json
import math
import threading
from camera_state import atomic_json


class MotionPolicy:
    def __init__(self, settings):
        self.path = settings.data_dir / 'motion-settings.json'
        self.lock = threading.RLock()
        self.version = 0
        self.value = {'area_percent': round(settings.motion_area_min / (settings.capture_width ** 2 * 9 / 16) * 100, 3),
                      'difference': 18, 'confirm_frames': settings.motion_min_frames, 'light_compensation': True}
        try:
            self.value = self.validate(json.loads(self.path.read_text()))
        except FileNotFoundError:
            pass
        # Invalid saved settings must be visible rather than silently replaced.

    @staticmethod
    def validate(data):
        if not isinstance(data, dict) or set(data) != {'area_percent', 'difference', 'confirm_frames', 'light_compensation'}:
            raise ValueError('動作設定欄位不正確')
        for name, low, high in (('area_percent', .05, 50), ('difference', 8, 80), ('confirm_frames', 1, 12)):
            value = data[name]
            if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError('動作閾值超出允許範圍')
        if any(int(data[k]) != data[k] for k in ('difference', 'confirm_frames')) or not isinstance(data['light_compensation'], bool):
            raise ValueError('連續影格與像素差需為整數；亮度補償需為開關')
        return {**data, 'area_percent': round(data['area_percent'], 3), 'difference': int(data['difference']), 'confirm_frames': int(data['confirm_frames'])}

    def get(self):
        with self.lock:
            return dict(self.value)

    def save(self, data):
        value = self.validate(data)
        with self.lock:
            if value != self.value:
                atomic_json(self.path, value)
                self.value = value
                self.version += 1
            return dict(self.value)

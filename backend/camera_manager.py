"""Camera manager.

One process-wide manager, like the robot serial manager: cameras can be
connected, and one of them is active. The preview window and the capture
block both read that active camera.
"""

import json
import platform
import subprocess
import threading
import time

import cv2
import numpy as np


_CONVERT = {
    'BGR': None,
    'RGB': cv2.COLOR_BGR2RGB,
    'GRAY': cv2.COLOR_BGR2GRAY,
    'HSV': cv2.COLOR_BGR2HSV,
}


def _encode_png(frame):
    ok, buf = cv2.imencode('.png', frame)
    if not ok:
        raise RuntimeError('Could not encode image')
    import base64
    return base64.b64encode(buf.tobytes()).decode('ascii')


def _decode_png(data):
    import base64
    raw = base64.b64decode(data)
    arr = np.frombuffer(raw, dtype=np.uint8)
    frame = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
    if frame is None:
        raise RuntimeError('Could not decode image')
    return frame


def _platform_names():
    system = platform.system()
    try:
        if system == 'Darwin':
            out = subprocess.check_output(
                ['system_profiler', 'SPCameraDataType', '-json'],
                stderr=subprocess.DEVNULL, timeout=12,
            )
            data = json.loads(out.decode('utf-8', errors='replace'))
            names = []
            for cam in data.get('SPCameraDataType') or []:
                name = cam.get('_name')
                if name:
                    names.append(str(name))
            return names
        if system == 'Windows':
            from pygrabber.dshow_graph import FilterGraph
            return [str(n) for n in FilterGraph().get_input_devices() if n]
        if system == 'Linux':
            import glob
            import os
            names = []
            paths = sorted(glob.glob('/sys/class/video4linux/video*'))
            for path in paths:
                base = os.path.basename(path)
                try:
                    idx = int(base.replace('video', ''))
                except ValueError:
                    idx = 0
                if idx >= 64:
                    continue
                try:
                    with open(os.path.join(path, 'name'), 'r', encoding='utf-8', errors='replace') as f:
                        names.append(f.read().strip() or base)
                except OSError:
                    names.append(base)
            return names
    except Exception:
        return []
    return []


def _open_capture(index):
    system = platform.system()
    backends = []
    if system == 'Windows':
        backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF]
    elif system == 'Darwin':
        backends = [cv2.CAP_AVFOUNDATION]
    for backend in backends:
        cap = cv2.VideoCapture(int(index), backend)
        if cap is not None and cap.isOpened():
            return cap
        if cap is not None:
            cap.release()
    cap = cv2.VideoCapture(int(index))
    if cap is not None and cap.isOpened():
        return cap
    if cap is not None:
        cap.release()
    return None


class _Camera:
    def __init__(self, index, name):
        self.index = index
        self.name = name
        self.cap = None
        self.connected = False
        self.last_frame = None
        self.last_jpeg = None
        self.width = 0
        self.height = 0


class CameraManager:
    _instance = None

    @classmethod
    def get_instance(cls):
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self):
        self._cams = {}
        self._active = None
        self._io = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._names = []
        self._names_at = 0.0

    def list_cameras(self):
        now = time.monotonic()
        if not self._names or now - self._names_at > 20:
            self._names = _platform_names()
            self._names_at = now
        count = max(len(self._names), 3)
        cameras = []
        for index in range(count):
            name = self._names[index] if index < len(self._names) else 'Camera %d' % index
            cam = self._cams.get(index)
            cameras.append({
                'id': index,
                'name': name,
                'connected': bool(cam and cam.connected),
                'active': self._active == index,
            })
        return {'cameras': cameras, 'active': self._active}

    def connect(self, index):
        index = int(index)
        info = self.list_cameras()
        name = 'Camera %d' % index
        for cam in info['cameras']:
            if cam['id'] == index:
                name = cam['name']
                break
        cap = _open_capture(index)
        if cap is None:
            return {'success': False, 'error': 'Could not open camera %d' % index}
        with self._io:
            old = self._cams.get(index)
            if old and old.cap is not None:
                old.cap.release()
            cam = _Camera(index, name)
            cam.cap = cap
            cam.connected = True
            self._cams[index] = cam
            self._active = index
        self._ensure_reader()
        return {'success': True, 'id': index, 'name': name}

    def disconnect(self, index=None):
        with self._io:
            if index is None:
                index = self._active
            if index is None:
                return {'success': True}
            index = int(index)
            cam = self._cams.get(index)
            if cam and cam.cap is not None:
                cam.cap.release()
                cam.cap = None
                cam.connected = False
            if self._active == index:
                self._active = None
                for other, conn in self._cams.items():
                    if conn.connected:
                        self._active = other
                        break
        return {'success': True, 'active': self._active}

    def latest_jpeg(self):
        with self._io:
            cam = self._cams.get(self._active) if self._active is not None else None
            if not cam or not cam.connected:
                return None
            return cam.last_jpeg

    def capture(self):
        with self._io:
            cam = self._cams.get(self._active) if self._active is not None else None
            if not cam or not cam.connected:
                raise RuntimeError('No camera is connected')
            frame = None if cam.last_frame is None else cam.last_frame.copy()
            if frame is None and cam.cap is not None:
                ok, frame = cam.cap.read()
                if not ok:
                    frame = None
        if frame is None:
            raise RuntimeError('Camera has no frame yet')
        height, width = frame.shape[:2]
        return {
            'success': True,
            'image': _encode_png(frame),
            'width': int(width),
            'height': int(height),
        }

    def resize(self, image, width, height):
        frame = _decode_png(image)
        width = max(1, min(4096, int(width)))
        height = max(1, min(4096, int(height)))
        resized = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        return {'success': True, 'image': _encode_png(resized), 'width': width, 'height': height}

    def convert(self, image, mode):
        mode = str(mode or 'BGR').upper()
        if mode not in _CONVERT:
            raise RuntimeError('Unknown color mode ' + mode)
        frame = _decode_png(image)
        code = _CONVERT[mode]
        if code is not None:
            frame = cv2.cvtColor(frame, code)
        return {'success': True, 'image': _encode_png(frame), 'mode': mode}

    def _ensure_reader(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, name='camera-reader', daemon=True)
        self._thread.start()

    def _read_loop(self):
        while not self._stop.is_set():
            with self._io:
                cam = self._cams.get(self._active) if self._active is not None else None
                cap = cam.cap if cam and cam.connected else None
                if cap is None:
                    frame = None
                else:
                    ok, frame = cap.read()
                    if not ok:
                        frame = None
                if frame is not None and cam is not None:
                    cam.last_frame = frame
                    cam.height, cam.width = frame.shape[:2]
            if frame is None:
                time.sleep(0.08)
                continue
            ok, buf = cv2.imencode('.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
            if ok:
                with self._io:
                    if cam is self._cams.get(cam.index):
                        cam.last_jpeg = buf.tobytes()
            time.sleep(0.03)

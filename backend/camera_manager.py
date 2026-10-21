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
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            return cap
        if cap is not None:
            cap.release()
    cap = cv2.VideoCapture(int(index))
    if cap is not None and cap.isOpened():
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
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
        self.holders = set()
        self.busy = False
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
        self._previews = {}

    def list_cameras(self):
        now = time.monotonic()
        if not self._names or now - self._names_at > 20:
            self._names = _platform_names()
            self._names_at = now
        # Only cameras the OS reports. Padding to 3 invented a dead "Camera 2".
        count = len(self._names)
        cameras = []
        for index in range(count):
            name = self._names[index]
            cam = self._cams.get(index)
            holders = cam.holders if cam and cam.connected else set()
            cameras.append({
                'id': index,
                'name': name,
                'in_use': bool(holders),
                'window': 'window' in holders,
                'program': any(str(h).startswith('program') for h in holders),
            })
        return {'cameras': cameras}

    def acquire(self, index, client):
        """Register ``client`` as a user of this camera. Open the device once."""
        index = int(index)
        client = str(client or 'window')
        with self._io:
            existing = self._cams.get(index)
            if existing and existing.connected and existing.cap is not None:
                existing.holders.add(client)
                self._active = index
                return {'success': True, 'id': index, 'name': existing.name}
        info = self.list_cameras()
        name = 'Camera %d' % index
        for item in info['cameras']:
            if item['id'] == index:
                name = item['name']
                break
        cap = _open_capture(index)
        if cap is None:
            return {'success': False, 'error': 'Could not open camera %d' % index}
        extra = None
        with self._io:
            existing = self._cams.get(index)
            if existing and existing.connected and existing.cap is not None:
                existing.holders.add(client)
                self._active = index
                name = existing.name
                extra = cap
            else:
                cam = _Camera(index, name)
                cam.cap = cap
                cam.connected = True
                cam.holders = set([client])
                self._cams[index] = cam
                self._active = index
        if extra is not None:
            extra.release()
        self._ensure_reader()
        return {'success': True, 'id': index, 'name': name}

    def release(self, index, client):
        """Drop one client. Close the device only when nobody remains."""
        if index is None:
            return {'success': True}
        index = int(index)
        client = str(client or 'window')
        with self._io:
            cam = self._cams.get(index)
            if not cam:
                return {'success': True}
            cam.holders.discard(client)
            if cam.holders:
                return {'success': True}
            cam.connected = False
            cap = cam.cap
            cam.cap = None
            cam.last_frame = None
            cam.last_jpeg = None
            if self._active == index:
                self._active = None
                for other, conn in self._cams.items():
                    if conn.connected and conn.cap is not None:
                        self._active = other
                        break
        deadline = time.monotonic() + 2.0
        while getattr(cam, 'busy', False) and time.monotonic() < deadline:
            time.sleep(0.01)
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        return {'success': True}

    def latest_jpeg(self, index=None):
        with self._io:
            if index is None:
                cam = self._cams.get(self._active) if self._active is not None else None
            else:
                cam = self._cams.get(int(index))
            if not cam or not cam.connected:
                return None
            return cam.last_jpeg

    def capture(self, index=None):
        if index is not None and index != '':
            opened = self.acquire(int(index), 'program-capture')
            if not opened.get('success'):
                raise RuntimeError(opened.get('error') or 'Could not open camera')
        frame = None
        for _ in range(40):
            with self._io:
                cam = self._cams.get(self._active) if self._active is not None else None
                if cam and cam.connected:
                    if cam.last_frame is not None:
                        frame = cam.last_frame.copy()
                    elif cam.cap is not None:
                        ok, grabbed = cam.cap.read()
                        if ok:
                            frame = grabbed
            if frame is not None:
                break
            time.sleep(0.03)
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

    def set_preview(self, slot, image):
        self._previews[int(slot)] = image or ''
        self._preview_rev = getattr(self, '_preview_rev', 0) + 1

    def set_previews(self, frames):
        for item in frames or []:
            if not item:
                continue
            self.set_preview(item.get('slot') or 1, item.get('image') or '')

    def previews(self):
        return {
            'rev': getattr(self, '_preview_rev', 0),
            'frames': [
                {'slot': slot, 'image': image}
                for slot, image in sorted(self._previews.items())
                if image
            ],
        }

    def _ensure_reader(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._read_loop, name='camera-reader', daemon=True)
        self._thread.start()

    def _read_loop(self):
        while not self._stop.is_set():
            with self._io:
                cams = [c for c in self._cams.values() if c.connected and c.cap is not None]
            if not cams:
                time.sleep(0.05)
                continue
            for cam in cams:
                with self._io:
                    if not cam.connected or cam.cap is None:
                        continue
                    cap = cam.cap
                    cam.busy = True
                try:
                    ok, frame = cap.read()
                except Exception:
                    ok, frame = False, None
                finally:
                    with self._io:
                        cam.busy = False
                if not ok or frame is None:
                    continue
                try:
                    encoded, buf = cv2.imencode(
                        '.jpg', frame, [int(cv2.IMWRITE_JPEG_QUALITY), 80])
                    jpeg = buf.tobytes() if encoded else None
                except Exception:
                    jpeg = None
                with self._io:
                    if cam.connected and cam.cap is cap:
                        cam.last_frame = frame
                        cam.height, cam.width = frame.shape[:2]
                        if jpeg is not None:
                            cam.last_jpeg = jpeg
            time.sleep(0.001)

"""Camera manager.

One process-wide manager, like the robot serial manager: cameras can be
connected, and one of them is active. The preview window and the capture
block both read that active camera.
"""

import json
import platform
import subprocess
import sys
import threading
import time

import cv2
import numpy as np


def _lifecycle(event, **fields):
    """One line per open, release, or enumeration. Not used on each frame."""
    parts = [time.strftime('%H:%M:%S'), event]
    for key in sorted(fields):
        parts.append('%s=%s' % (key, fields[key]))
    stream = getattr(sys, '__stderr__', None)
    if stream is None:
        return
    print('[camera] ' + ' '.join(parts), file=stream, flush=True)


_CANCEL_LIMIT = 256


def _is_window_client(client):
    """Camera-window holder. Program clients start with ``program``, not ``window``."""
    name = str(client)
    return name == 'window' or name.startswith('window-')


def _cancelled_acquire():
    return {'success': False, 'error': 'Connection cancelled', 'cancelled': True}


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
    """Open one device. Callers must release() it. Listing cameras does not call this."""
    system = platform.system()
    backends = []
    if system == 'Windows':
        backends = [cv2.CAP_DSHOW, cv2.CAP_MSMF]
    elif system == 'Darwin':
        backends = [cv2.CAP_AVFOUNDATION]
    _lifecycle('open-begin', index=index)
    for backend in backends:
        cap = cv2.VideoCapture(int(index), backend)
        if cap is not None and cap.isOpened():
            try:
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            except Exception:
                pass
            _lifecycle('open', index=index, backend=backend)
            return cap
        if cap is not None:
            cap.release()
            _lifecycle('release', index=index, reason='backend-rejected', backend=backend)
    cap = cv2.VideoCapture(int(index))
    if cap is not None and cap.isOpened():
        try:
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        _lifecycle('open', index=index, backend='default')
        return cap
    if cap is not None:
        cap.release()
        _lifecycle('release', index=index, reason='open-failed')
    _lifecycle('open-failed', index=index)
    return None


def _release_capture(cap, index, reason):
    if cap is None:
        return
    try:
        cap.release()
    except Exception:
        pass
    _lifecycle('release', index=index, reason=reason)


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
        self.drop_cap = None


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
        self._idle = threading.Condition(self._io)
        self._stop = threading.Event()
        self._thread = None
        self._names = None
        self._names_at = 0.0
        self._previews = {}
        # Client ids whose disconnect arrived before acquire committed.
        # In-flight ids are never discarded, so a slow open cannot revive them.
        self._cancelled = set()
        self._cancel_order = []
        self._inflight = set()

    def list_cameras(self, refresh=False):
        """Return the cached device list.

        ``refresh=True`` asks the OS for names. That is discovery, not a
        capture. An ordinary list call never opens a camera and never
        repeats discovery on its own.
        """
        if refresh:
            self._names = _platform_names() or []
            self._names_at = time.monotonic()
            _lifecycle('enumerate', count=len(self._names))
        names = self._names or []
        # Only cameras the OS reports. Padding to 3 invented a dead "Camera 2".
        count = len(names)
        cameras = []
        for index in range(count):
            name = names[index]
            cam = self._cams.get(index)
            holders = cam.holders if cam and cam.connected else set()
            cameras.append({
                'id': index,
                'name': name,
                'in_use': bool(holders),
                'window': any(_is_window_client(holder) for holder in holders),
                'program': any(str(holder).startswith('program') for holder in holders),
            })
        return {'cameras': cameras}

    def acquire(self, index, client):
        """Register ``client`` as a user of this camera. Open the device once.

        A disconnect for this same client that arrives before the holder is
        recorded cancels the attempt, including while the device is opening.
        """
        index = int(index)
        client = str(client or 'window')
        with self._io:
            self._inflight.add(client)
            if self._take_cancel_locked(client):
                self._inflight.discard(client)
                return _cancelled_acquire()
            existing = self._cams.get(index)
            if existing and existing.connected and existing.cap is not None:
                existing.holders.add(client)
                self._active = index
                self._inflight.discard(client)
                _lifecycle('acquire', index=index, client=client, holders=len(existing.holders))
                return {'success': True, 'id': index, 'name': existing.name}
        info = self.list_cameras(refresh=True)
        name = 'Camera %d' % index
        for item in info['cameras']:
            if item['id'] == index:
                name = item['name']
                break
        cap = _open_capture(index)
        extra = None
        cancelled = False
        with self._io:
            self._inflight.discard(client)
            if self._take_cancel_locked(client):
                cancelled = True
                extra = cap
            elif cap is None:
                _lifecycle('acquire-failed', index=index, client=client)
                return {'success': False, 'error': 'Could not open camera %d' % index}
            else:
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
            _release_capture(extra, index, 'cancelled' if cancelled else 'duplicate-open')
        if cancelled:
            return _cancelled_acquire()
        _lifecycle('acquire', index=index, client=client, holders=len(self._cams[index].holders))
        self._ensure_reader()
        return {'success': True, 'id': index, 'name': name}

    def release(self, index, client):
        """Drop one client. Close the device only when nobody remains.

        If the reader is inside ``read()``, it releases the device when that
        call returns. This method does not call ``release()`` on a capture
        that another thread is reading.
        """
        if index is None:
            return {'success': True}
        index = int(index)
        client = str(client or 'window')
        to_release = None
        with self._idle:
            cam = self._cams.get(index)
            if not cam or client not in cam.holders:
                # The connect may still be opening. Remember the id so acquire
                # does not commit a holder after this disconnect.
                self._remember_cancel_locked(client)
                return {'success': True}
            cam.holders.discard(client)
            _lifecycle('release-client', index=index, client=client, holders=len(cam.holders))
            if cam.holders:
                return {'success': True}
            cam.connected = False
            cap = cam.cap
            cam.cap = None
            cam.last_frame = None
            cam.last_jpeg = None
            if cam.busy and cap is not None:
                cam.drop_cap = cap
            else:
                to_release = cap
            if self._active == index:
                self._active = None
                for other, conn in self._cams.items():
                    if conn.connected and conn.cap is not None:
                        self._active = other
                        break
            self._idle.notify_all()
        _release_capture(to_release, index, 'last-client')
        return {'success': True}

    def _remember_cancel_locked(self, client):
        if client in self._cancelled:
            return
        self._cancelled.add(client)
        self._cancel_order.append(client)
        skipped = 0
        while len(self._cancelled) > _CANCEL_LIMIT and self._cancel_order and skipped < len(self._cancel_order):
            old = self._cancel_order.pop(0)
            if old in self._inflight:
                self._cancel_order.append(old)
                skipped += 1
                continue
            self._cancelled.discard(old)
            skipped = 0

    def _take_cancel_locked(self, client):
        if client not in self._cancelled:
            return False
        self._cancelled.discard(client)
        try:
            self._cancel_order.remove(client)
        except ValueError:
            pass
        return True

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
        client = None
        opened_index = None
        if index is not None and index != '':
            opened_index = int(index)
            client = 'program-capture-%s' % time.monotonic_ns()
            opened = self.acquire(opened_index, client)
            if not opened.get('success'):
                raise RuntimeError(opened.get('error') or 'Could not open camera')
        try:
            frame = None
            for _ in range(40):
                with self._io:
                    cam = self._cams.get(self._active) if self._active is not None else None
                    if cam and cam.connected and cam.last_frame is not None:
                        frame = cam.last_frame.copy()
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
        finally:
            if client is not None:
                self.release(opened_index, client)

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
        with self._io:
            if self._thread and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._read_loop, name='camera-reader', daemon=True)
            self._thread.start()
            _lifecycle('reader-start')

    def _read_loop(self):
        me = threading.current_thread()
        try:
            while not self._stop.is_set():
                with self._io:
                    cams = [
                        c for c in self._cams.values()
                        if c.connected and c.cap is not None
                    ]
                if not cams:
                    with self._io:
                        if any(c.connected and c.cap is not None for c in self._cams.values()):
                            continue
                        if self._thread is me:
                            self._thread = None
                        _lifecycle('reader-stop', reason='idle')
                        return
                for cam in cams:
                    with self._io:
                        if not cam.connected or cam.cap is None:
                            continue
                        cap = cam.cap
                        cam.busy = True
                    drop = None
                    try:
                        ok, frame = cap.read()
                    except Exception:
                        ok, frame = False, None
                    finally:
                        with self._idle:
                            cam.busy = False
                            if cam.drop_cap is cap:
                                drop = cap
                                cam.drop_cap = None
                            self._idle.notify_all()
                    if drop is not None:
                        _release_capture(drop, cam.index, 'reader')
                        continue
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
        finally:
            with self._io:
                if self._thread is me:
                    self._thread = None

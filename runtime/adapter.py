"""Blockly-side client for the Camera backend.

This file runs in the StudioX Python process, not in the camera environment.
It opens a holder by HTTP and decodes JPEG frames with whatever OpenCV that
process already has. It must never construct ``cv2.VideoCapture``.

``grab`` reports whether this session still holds the device. ``retrieve``
returns the same image as ``read``. ``get`` returns 0 and ``set`` returns
False: camera properties are not forwarded, and ``set`` does not pretend
that a property was applied.
"""

import time
import urllib.parse
import uuid

# How long one read may wait for a frame the camera has not produced yet.
# Stop is checked before the request and again when it returns.
_FRAME_WAIT_MS = 500


def create_session(ctx):
    return CameraExecutionAdapter(ctx)


class CameraExecutionAdapter:
    def __init__(self, ctx):
        self.ctx = ctx
        self._captures = []

    def open_camera(self, index):
        self.ctx.check_aborted()
        device_id = _device_id(index)
        cap = ManagedCameraCapture(self.ctx, device_id)
        if not cap.isOpened():
            message = cap.last_error or 'Camera is unavailable'
            self.ctx.report_error(message)
            raise RuntimeError(message)
        self._captures.append(cap)
        return cap

    def close(self):
        for cap in reversed(list(self._captures)):
            cap.release()
        self._captures.clear()


class ManagedCameraCapture:
    def __init__(self, ctx, index):
        self.ctx = ctx
        self.index = index
        self.client = 'program-%s-%s' % (ctx.session_id, uuid.uuid4().hex[:8])
        self._held = False
        self._seq = None
        self.last_error = ''
        acquired = False
        try:
            response = self.ctx.request('POST', '/connect', json={
                'id': self.index,
                'client': self.client,
            }, timeout=20)
            payload = response.json()
            acquired = bool(isinstance(payload, dict) and payload.get('success'))
            if not acquired:
                self.last_error = _payload_error(payload) or 'Could not open the camera'
        except Exception as exc:
            acquired = False
            self.last_error = _error_text(exc)
        finally:
            self._held = bool(acquired)
            if not acquired:
                self._disconnect()

    def read(self):
        """Return ``(ok, frame)``. Wait briefly for the first JPEG after open."""
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline:
            self.ctx.check_aborted()
            frame = self._fetch()
            if frame is not None:
                return True, frame
            time.sleep(0.05)
        if self._held:
            self.ctx.report_error(self.last_error or 'Camera has no frame yet')
        return False, None

    def isOpened(self):
        return bool(self._held)

    def release(self):
        if not self._held:
            return
        self._held = False
        self._disconnect()

    def grab(self):
        """Placeholder. True only means this session still holds the device."""
        return self.isOpened()

    def retrieve(self, *args, **kwargs):
        """Same image as ``read``. There is no separate hardware retrieve."""
        return self.read()

    def get(self, *args, **kwargs):
        """Placeholder. Camera properties are not forwarded."""
        return 0

    def set(self, *args, **kwargs):
        """Placeholder. Returns False because the property was not applied."""
        return False

    def _frame_path(self):
        params = [('id', str(self.index)), ('wait_ms', str(_FRAME_WAIT_MS))]
        if self._seq is not None:
            params.append(('after', str(self._seq)))
        return '/frame?' + urllib.parse.urlencode(params)

    def _fetch(self):
        self.ctx.check_aborted()
        self.last_error = ''
        try:
            response = self.ctx.request('GET', self._frame_path(), timeout=2)
        except Exception as exc:
            http = getattr(exc, 'response', None)
            if http is not None and getattr(http, 'status', None) == 404:
                self.last_error = 'Camera has no frame yet'
                return None
            self.last_error = 'Camera frame request failed'
            return None
        self.ctx.check_aborted()
        if 'image' not in _content_type(response.headers):
            self.last_error = 'Camera has no frame yet'
            return None
        frame = _decode_jpeg(response.body)
        if frame is None:
            self.last_error = 'Preview could not decode a camera frame'
            return None
        raw_seq = _header(response.headers, 'x-frame-seq')
        if raw_seq is not None and raw_seq != '':
            try:
                self._seq = int(raw_seq)
            except ValueError:
                pass
        return frame

    def _disconnect(self):
        try:
            self.ctx.request('POST', '/disconnect', json={
                'id': self.index,
                'client': self.client,
            }, ignore_abort=True)
        except Exception:
            return


def _device_id(index):
    if isinstance(index, bool) or index is None or index == '':
        raise RuntimeError('Choose a camera before running.')
    if isinstance(index, int):
        device_id = index
    elif isinstance(index, str) and index.strip().lstrip('-').isdigit():
        device_id = int(index.strip())
    else:
        raise RuntimeError('Camera %r is not a device id.' % (index,))
    if device_id < 0:
        raise RuntimeError('Camera %r is not a device id.' % (index,))
    return device_id


def _decode_jpeg(raw):
    try:
        import cv2
        import numpy as np
    except Exception as exc:
        raise RuntimeError(
            'Camera frames need OpenCV in the StudioX Python environment (%s).' % exc
        ) from exc
    if not raw:
        return None
    arr = np.frombuffer(raw, dtype=np.uint8)
    try:
        frame = cv2.imdecode(arr.copy(), cv2.IMREAD_COLOR)
    except Exception:
        return None
    if frame is None or getattr(frame, 'size', 0) == 0:
        return None
    return frame


def _header(headers, name):
    wanted = str(name).lower()
    for key, value in dict(headers or {}).items():
        if str(key).lower() == wanted:
            return str(value)
    return None


def _content_type(headers):
    value = _header(headers, 'content-type')
    return value.lower() if value else ''


def _payload_error(payload):
    if isinstance(payload, dict) and payload.get('error'):
        return str(payload['error'])
    return ''


def _error_text(exc):
    http = getattr(exc, 'response', None)
    if http is not None:
        try:
            found = _payload_error(http.json())
        except Exception:
            found = ''
        if found:
            return found
    text = str(exc).strip()
    return text or 'Could not open the camera'

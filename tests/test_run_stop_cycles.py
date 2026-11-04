"""Exercise the runtime and backend together across rapid program restarts."""

import json as json_module
import os
import sys
import threading
import unittest
from urllib.parse import parse_qs, urlsplit
from unittest import mock

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import camera_manager
from test_runtime_adapter import Cancelled, Response, _load


class FrameCapture:
    def __init__(self):
        self.released = False
        self.release_count = 0
        self.in_read = False
        self.reads = 0
        self.waiting = threading.Event()
        self.gate = threading.Event()

    def read(self):
        self.in_read = True
        try:
            self.reads += 1
            if self.reads > 1:
                self.waiting.set()
                self.gate.wait(2)
            return True, np.zeros((3, 4, 3), dtype=np.uint8)
        finally:
            self.in_read = False

    def release(self):
        if self.in_read:
            raise AssertionError('capture released during a read')
        self.release_count += 1
        self.released = True


class BackendContext:
    def __init__(self, manager, session_id):
        self.manager = manager
        self.session_id = session_id
        self.cleanups = []
        self.aborted = False

    def check_aborted(self):
        if self.aborted:
            raise Cancelled()

    def add_cleanup(self, callback):
        self.cleanups.append(callback)

    def request(self, method, relative_path, json=None, timeout=None, ignore_abort=False):
        if not ignore_abort:
            self.check_aborted()
        route = urlsplit(relative_path)
        headers = {'Content-Type': 'application/json'}
        status = 200
        if route.path == '/connect':
            data = self.manager.acquire(json['id'], json['client'])
        elif route.path == '/disconnect':
            data = self.manager.release(json['id'], json['client'])
        elif route.path == '/frame':
            params = parse_qs(route.query)
            after = params.get('after', [None])[0]
            jpeg, seq = self.manager.wait_jpeg(
                int(params['id'][0]), after=int(after) if after else None, timeout=0.5)
            headers = {'Content-Type': 'image/jpeg', 'X-Frame-Seq': str(seq)}
            return Response(200, headers, jpeg) if jpeg else Response(404, {}, b'')
        else:
            raise AssertionError(relative_path)
        if not ignore_abort:
            self.check_aborted()
        return Response(status, headers, json_module.dumps(data).encode())

    def report_error(self, message):
        raise AssertionError(message)

    def stop(self, adapter):
        self.aborted = True
        adapter.close()
        for cleanup in reversed(self.cleanups):
            cleanup()


class RunStopCycleTests(unittest.TestCase):
    def test_twenty_rapid_run_stop_cycles_produce_frames_without_overlapping_opens(self):
        manager = camera_manager.CameraManager()
        adapter_module = _load()
        captures = []
        contexts = []
        timers = []
        def open_capture(_index):
            self.assertTrue(all(cap.released for cap in captures), 'device reopened before old close')
            cap = FrameCapture()
            captures.append(cap)
            return cap
        with mock.patch.object(camera_manager, '_platform_names', return_value=['Test camera']), \
                mock.patch.object(camera_manager, '_open_capture', side_effect=open_capture), \
                mock.patch.object(camera_manager, '_lifecycle'):
            try:
                for cycle in range(20):
                    ctx = BackendContext(manager, 'run-%d' % cycle)
                    adapter = adapter_module.create_session(ctx)
                    contexts.append((ctx, adapter))
                    cap = adapter.open_camera(0)
                    ok, image = cap.read()
                    self.assertTrue(ok, 'no frame on cycle %d' % cycle)
                    self.assertEqual(image.shape, (3, 4, 3))
                    native = captures[-1]
                    self.assertTrue(native.waiting.wait(1))
                    ctx.stop(adapter)
                    self.assertEqual(manager._cams[0].holders, set())
                    self.assertFalse(native.released, 'read is still active at Stop')
                    # The next Run starts before the old read is allowed to return.
                    timer = threading.Timer(0.03, native.gate.set)
                    timers.append(timer)
                    timer.start()
            finally:
                for native in captures:
                    native.gate.set()
                for ctx, adapter in contexts:
                    ctx.stop(adapter)
                for timer in timers:
                    timer.join(1)
                manager._stop.set()
                if manager._thread:
                    manager._thread.join(2)
            self.assertEqual(len(captures), 20)
            self.assertTrue(all(native.release_count == 1 for native in captures))
            self.assertFalse(any(cam.holders for cam in manager._cams.values()))


if __name__ == '__main__':
    unittest.main()

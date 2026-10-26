"""read() waits for a new frame instead of polling the cached JPEG."""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import camera_manager
from camera_manager import CameraManager, _Camera


class FrameWaitTests(unittest.TestCase):
    def setUp(self):
        CameraManager._instance = None
        self.mgr = CameraManager.get_instance()

    def tearDown(self):
        self.mgr._stop.set()
        thread = self.mgr._thread
        if thread is not None:
            thread.join(timeout=2)
        CameraManager._instance = None

    def _open_record(self, index=0, seq=1, jpeg=b'frame'):
        cam = _Camera(index, 'Fake')
        cam.connected = True
        cam.frame_seq = seq
        cam.last_jpeg = jpeg
        self.mgr._cams[index] = cam
        self.mgr._active = index
        return cam

    def test_wait_follows_the_producer_instead_of_the_cache(self):
        cam = self._open_record(seq=0, jpeg=None)
        stop = threading.Event()

        def publish():
            seq = 0
            while not stop.is_set():
                time.sleep(1 / 30)
                seq += 1
                with self.mgr._idle:
                    cam.connected = True
                    cam.last_jpeg = b'frame-%d' % seq
                    cam.frame_seq = seq
                    self.mgr._idle.notify_all()

        thread = threading.Thread(target=publish, daemon=True)
        thread.start()
        try:
            time.sleep(0.05)
            got = []
            seq = 0
            deadline = time.monotonic() + 0.50
            while time.monotonic() < deadline:
                _jpeg, seq = self.mgr.wait_jpeg(0, after=seq, timeout=0.5)
                got.append(seq)
        finally:
            stop.set()
            thread.join(timeout=1)
        self.assertGreaterEqual(len(got), 8)
        self.assertLessEqual(len(got), 30)
        self.assertEqual(got, sorted(set(got)))

    def test_timeout_returns_the_current_frame_and_disconnect_unblocks(self):
        cam = self._open_record(seq=3, jpeg=b'abc')
        started = time.monotonic()
        jpeg, seq = self.mgr.wait_jpeg(0, after=3, timeout=0.15)
        elapsed = time.monotonic() - started
        self.assertEqual((jpeg, seq), (b'abc', 3))
        self.assertGreaterEqual(elapsed, 0.10)
        self.assertLess(elapsed, 0.5)

        def drop():
            time.sleep(0.05)
            with self.mgr._idle:
                cam.connected = False
                cam.last_jpeg = None
                self.mgr._idle.notify_all()

        threading.Thread(target=drop, daemon=True).start()
        started = time.monotonic()
        jpeg, seq = self.mgr.wait_jpeg(0, after=3, timeout=2)
        self.assertIsNone(jpeg)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_cached_get_does_not_wait(self):
        self._open_record(seq=2, jpeg=b'now')
        started = time.monotonic()
        jpeg, seq = self.mgr.wait_jpeg(0, after=None, timeout=2)
        self.assertEqual((jpeg, seq), (b'now', 2))
        self.assertLess(time.monotonic() - started, 0.1)
        self.assertEqual(self.mgr.latest_jpeg(0), b'now')

    def test_reader_publishes_a_sequence(self):
        import numpy as np

        class OneFrame:
            def __init__(self):
                self.released = False
                self.in_read = False
                self.entered = threading.Event()
                self.gate = threading.Event()
                self._sent = False

            def isOpened(self):
                return not self.released

            def read(self):
                self.in_read = True
                self.entered.set()
                if not self._sent:
                    self._sent = True
                    self.in_read = False
                    image = np.zeros((2, 2, 3), dtype=np.uint8)
                    image[:, :] = (4, 5, 6)
                    return True, image
                self.gate.wait(2)
                self.in_read = False
                return False, None

            def release(self):
                self.released = True

        cap = OneFrame()

        def fake_open(_index):
            return cap

        with mock.patch.object(camera_manager, '_open_capture', fake_open), \
                mock.patch.object(camera_manager, '_platform_names', return_value=['FaceTime']):
            opened = self.mgr.acquire(0, 'program-test')
            self.assertTrue(opened.get('success'), opened)
            try:
                jpeg, seq = self.mgr.wait_jpeg(0, after=None, timeout=1)
                self.assertIsNotNone(jpeg)
                self.assertGreaterEqual(seq, 1)
                self.assertTrue(jpeg.startswith(b'\xff\xd8'))
            finally:
                self.mgr.release(0, 'program-test')
                cap.gate.set()
                thread = self.mgr._thread
                if thread is not None:
                    thread.join(timeout=2)


class FrameRouteTests(unittest.TestCase):
    def setUp(self):
        CameraManager._instance = None
        self.mgr = CameraManager.get_instance()
        from flask import Flask
        import main as camera_main
        app = Flask(__name__)
        app.register_blueprint(camera_main.blueprint)
        self.client = app.test_client()

    def tearDown(self):
        self.mgr._stop.set()
        CameraManager._instance = None

    def test_plain_get_is_immediate_and_wait_blocks_for_a_new_seq(self):
        cam = _Camera(0, 'Fake')
        cam.connected = True
        cam.frame_seq = 4
        cam.last_jpeg = b'jpeg-4'
        self.mgr._cams[0] = cam
        immediate = self.client.get('/frame?id=0')
        self.assertEqual(immediate.status_code, 200)
        self.assertEqual(immediate.data, b'jpeg-4')
        self.assertEqual(immediate.headers['X-Frame-Seq'], '4')

        def publish():
            time.sleep(0.05)
            with self.mgr._idle:
                cam.last_jpeg = b'jpeg-5'
                cam.frame_seq = 5
                self.mgr._idle.notify_all()

        threading.Thread(target=publish, daemon=True).start()
        started = time.monotonic()
        waited = self.client.get('/frame?id=0&after=4&wait_ms=1000')
        self.assertEqual(waited.status_code, 200)
        self.assertEqual(waited.data, b'jpeg-5')
        self.assertEqual(waited.headers['X-Frame-Seq'], '5')
        self.assertLess(time.monotonic() - started, 0.8)
        missing = self.client.get('/frame?id=3')
        self.assertEqual(missing.status_code, 404)


if __name__ == '__main__':
    unittest.main()

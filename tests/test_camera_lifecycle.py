"""Camera manager opens a device only for a real client, then leaves it closed."""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import camera_manager


class FakeCap:
    def __init__(self):
        self.released = False
        self.release_count = 0
        self.release_during_read = False
        self.in_read = False
        self.entered = threading.Event()
        self.gate = None

    def isOpened(self):
        return not self.released

    def set(self, *_args, **_kwargs):
        return True

    def read(self):
        self.in_read = True
        self.entered.set()
        if self.gate is not None:
            self.gate.wait(2)
        self.in_read = False
        return False, None

    def release(self):
        self.release_during_read = self.in_read
        self.release_count += 1
        self.released = True


class CameraLifecycleTests(unittest.TestCase):
    def setUp(self):
        camera_manager.CameraManager._instance = None
        self.mgr = camera_manager.CameraManager.get_instance()
        self.opens = []

        def fake_open(_index):
            cap = FakeCap()
            self.opens.append(cap)
            return cap

        self.open_patch = mock.patch.object(camera_manager, '_open_capture', fake_open)
        self.name_patch = mock.patch.object(
            camera_manager, '_platform_names', return_value=['FaceTime'])
        self.open_patch.start()
        self.names = self.name_patch.start()

    def tearDown(self):
        self.mgr._stop.set()
        for cap in self.opens:
            if cap.gate is not None:
                cap.gate.set()
        thread = self.mgr._thread
        if thread is not None:
            thread.join(timeout=2)
        self.open_patch.stop()
        self.name_patch.stop()
        camera_manager.CameraManager._instance = None

    def test_idle_list_does_not_open_or_enumerate(self):
        for _ in range(8):
            listed = self.mgr.list_cameras()
        self.names.assert_not_called()
        self.assertEqual(self.opens, [])
        self.assertEqual(listed['cameras'], [])

    def test_refresh_enumerates_without_opening(self):
        listed = self.mgr.list_cameras(refresh=True)
        self.mgr.list_cameras()
        self.mgr.list_cameras()
        self.assertEqual(self.names.call_count, 1)
        self.assertEqual(len(listed['cameras']), 1)
        self.assertEqual(self.opens, [])

    def _wait_released(self, cap, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline and not cap.released:
            time.sleep(0.01)
        self.assertTrue(cap.released)
        self.assertEqual(cap.release_count, 1)

    def test_connect_then_disconnect_opens_once(self):
        self.mgr.list_cameras(refresh=True)
        opened = self.mgr.acquire(0, 'window')
        self.assertTrue(opened['success'])
        self.assertEqual(len(self.opens), 1)
        self.mgr.release(0, 'window')
        self._wait_released(self.opens[0])
        for _ in range(5):
            self.mgr.list_cameras()
        self.assertEqual(len(self.opens), 1)
        self.assertFalse(self.mgr._cams[0].connected)

    def test_shared_clients_close_only_when_both_release(self):
        self.mgr.acquire(0, 'window')
        self.mgr.acquire(0, 'program-a')
        self.assertEqual(len(self.opens), 1)
        self.mgr.release(0, 'window')
        self.assertFalse(self.opens[0].released)
        self.assertTrue(self.mgr._cams[0].connected)
        self.mgr.release(0, 'program-a')
        self._wait_released(self.opens[0])

    def test_repeated_release_is_safe(self):
        self.mgr.acquire(0, 'window')
        self.mgr.release(0, 'window')
        self._wait_released(self.opens[0])
        self.mgr.release(0, 'window')
        self.mgr.release(0, 'missing')
        self.assertEqual(self.opens[0].release_count, 1)

    def test_failed_open_leaves_no_client(self):
        with mock.patch.object(camera_manager, '_open_capture', return_value=None):
            result = self.mgr.acquire(0, 'window')
        self.assertFalse(result['success'])
        self.assertNotIn(0, self.mgr._cams)

    def test_release_does_not_close_during_read(self):
        gate = threading.Event()
        real_open = camera_manager._open_capture

        def blocking_open(_index):
            cap = real_open(_index)
            cap.gate = gate
            return cap

        with mock.patch.object(camera_manager, '_open_capture', blocking_open):
            self.mgr.acquire(0, 'window')
            cap = self.opens[0]
            self.assertTrue(cap.entered.wait(2))
            self.mgr.release(0, 'window')
            self.assertEqual(cap.release_count, 0)
            self.assertTrue(cap.in_read)
            gate.set()
            deadline = time.time() + 2
            while time.time() < deadline and cap.release_count == 0:
                time.sleep(0.01)
        self.assertEqual(cap.release_count, 1)
        self.assertFalse(cap.release_during_read)

    def test_five_connect_cycles_do_not_leak(self):
        for _ in range(5):
            self.mgr.acquire(0, 'window')
            self.mgr.acquire(0, 'program-run')
            self.mgr.release(0, 'program-run')
            self.assertFalse(self.opens[-1].released)
            self.mgr.release(0, 'window')
            self._wait_released(self.opens[-1])
        self.assertEqual(len(self.opens), 5)
        self.assertTrue(all(cap.release_count == 1 for cap in self.opens))
        self.assertFalse(any(cam.holders for cam in self.mgr._cams.values()))


if __name__ == '__main__':
    unittest.main()

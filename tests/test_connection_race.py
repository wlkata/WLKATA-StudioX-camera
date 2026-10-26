"""Connect and disconnect ordering for unique Camera window clients."""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import camera_manager
from test_camera_lifecycle import FakeCap


class ConnectionRaceTests(unittest.TestCase):
    def setUp(self):
        camera_manager.CameraManager._instance = None
        self.mgr = camera_manager.CameraManager.get_instance()
        self.opens = []

        def fake_open(_index):
            cap = FakeCap()
            self.opens.append(cap)
            return cap

        self._fake_open = fake_open
        self.open_patch = mock.patch.object(camera_manager, '_open_capture', fake_open)
        self.name_patch = mock.patch.object(
            camera_manager, '_platform_names', return_value=['FaceTime', 'USB'])
        self.open_patch.start()
        self.name_patch.start()

    def tearDown(self):
        self.mgr._stop.set()
        thread = self.mgr._thread
        if thread is not None:
            thread.join(timeout=2)
        self.open_patch.stop()
        self.name_patch.stop()
        camera_manager.CameraManager._instance = None

    def _holders(self, index=0):
        cam = self.mgr._cams.get(index)
        if not cam or not cam.connected:
            return set()
        return set(cam.holders)

    def _wait_released(self, cap):
        deadline = time.time() + 2
        while time.time() < deadline and not cap.released:
            time.sleep(0.01)
        self.assertTrue(cap.released)

    def test_connect_then_disconnect(self):
        opened = self.mgr.acquire(0, 'window-a')
        self.assertTrue(opened['success'])
        self.assertEqual(self._holders(), {'window-a'})
        self.mgr.release(0, 'window-a')
        self.assertEqual(self._holders(), set())
        self._wait_released(self.opens[0])
        self.assertEqual(self.opens[0].release_count, 1)

    def test_disconnect_before_acquire_commits(self):
        self.mgr.release(0, 'window-early')
        result = self.mgr.acquire(0, 'window-early')
        self.assertTrue(result.get('cancelled'))
        self.assertEqual(self.opens, [])
        self.assertEqual(self._holders(), set())
        self.assertNotIn('window-early', self.mgr._cancelled)

    def test_close_while_open_is_in_progress(self):
        started = threading.Event()
        proceed = threading.Event()

        def slow_open(_index):
            cap = FakeCap()
            self.opens.append(cap)
            started.set()
            self.assertTrue(proceed.wait(2))
            return cap

        result = {}

        def run():
            with mock.patch.object(camera_manager, '_open_capture', slow_open):
                result['value'] = self.mgr.acquire(0, 'window-close')

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2))
        self.mgr.release(0, 'window-close')
        proceed.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result['value'].get('cancelled'))
        self.assertTrue(self.opens[0].released)
        self.assertNotIn('window-close', self._holders())
        self.assertNotIn('window-close', self.mgr._cancelled)

    def test_old_disconnect_does_not_drop_the_new_client(self):
        self.mgr.acquire(0, 'window-a')
        self.mgr.acquire(0, 'window-b')
        self.assertEqual(len(self.opens), 1)
        self.mgr.release(0, 'window-a')
        self.assertEqual(self._holders(), {'window-b'})
        self.assertFalse(self.opens[0].released)
        self.mgr.release(0, 'window-b')
        self._wait_released(self.opens[0])

    def test_window_disconnect_leaves_the_program_holder(self):
        self.mgr.acquire(0, 'window-ui')
        self.mgr.acquire(0, 'program-session-1')
        self.assertEqual(len(self.opens), 1)
        self.mgr.release(0, 'window-ui')
        self.assertEqual(self._holders(), {'program-session-1'})
        self.assertFalse(self.opens[0].released)
        self.mgr.release(0, 'program-session-1')
        self._wait_released(self.opens[0])

    def test_duplicate_release_does_not_affect_another_client(self):
        self.mgr.acquire(0, 'window-a')
        self.mgr.acquire(0, 'program-session-1')
        self.mgr.release(0, 'window-a')
        self.mgr.release(0, 'window-a')
        self.mgr.release(0, 'window-a')
        self.assertEqual(self._holders(), {'program-session-1'})
        self.assertEqual(self.opens[0].release_count, 0)

    def test_failed_open_leaves_no_holder(self):
        with mock.patch.object(camera_manager, '_open_capture', return_value=None):
            result = self.mgr.acquire(0, 'window-fail')
        self.assertFalse(result['success'])
        self.assertFalse(result.get('cancelled'))
        self.assertEqual(self._holders(), set())
        self.assertNotIn('window-fail', self.mgr._inflight)

    def test_list_reports_window_ids_separately_from_program_ids(self):
        self.mgr.acquire(0, 'window-new')
        self.mgr.acquire(1, 'program-only')
        listed = self.mgr.list_cameras()
        by_id = {item['id']: item for item in listed['cameras']}
        self.assertTrue(by_id[0]['window'])
        self.assertFalse(by_id[0]['program'])
        self.assertFalse(by_id[1]['window'])
        self.assertTrue(by_id[1]['program'])
        self.mgr.acquire(0, 'program-shared')
        self.mgr.acquire(0, 'window')
        listed = self.mgr.list_cameras()
        shared = {item['id']: item for item in listed['cameras']}[0]
        self.assertTrue(shared['window'])
        self.assertTrue(shared['program'])
        self.mgr.release(0, 'window-new')
        self.mgr.release(0, 'window')
        listed = self.mgr.list_cameras()
        after = {item['id']: item for item in listed['cameras']}[0]
        self.assertFalse(after['window'])
        self.assertTrue(after['program'])
        self.assertFalse(camera_manager._is_window_client('program-window-1'))

    def test_cancel_limit_does_not_forget_an_open_in_progress(self):
        started = threading.Event()
        proceed = threading.Event()

        def slow_open(_index):
            cap = FakeCap()
            self.opens.append(cap)
            started.set()
            self.assertTrue(proceed.wait(2))
            return cap

        def run():
            with mock.patch.object(camera_manager, '_open_capture', slow_open):
                self.mgr.acquire(0, 'window-live')

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(started.wait(2))
        self.mgr.release(0, 'window-live')
        for index in range(300):
            self.mgr.release(0, 'window-old-%d' % index)
        self.assertIn('window-live', self.mgr._cancelled)
        self.assertLessEqual(len(self.mgr._cancelled), camera_manager._CANCEL_LIMIT + 1)
        proceed.set()
        thread.join(2)
        self.assertNotIn('window-live', self._holders())
        self.assertTrue(self.opens[0].released)


if __name__ == '__main__':
    unittest.main()

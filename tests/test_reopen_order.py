"""Do not reopen a device while its previous read or release is unfinished."""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))
import camera_manager
from test_camera_lifecycle import FakeCap


def wait_until(predicate, timeout=1):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.002)
    return False


class ReopenOrderTests(unittest.TestCase):
    def setUp(self):
        self.mgr = camera_manager.CameraManager()
        self.opens = []
        self.gates = []
        self.workers = []
        self.names = mock.patch.object(camera_manager, '_platform_names', return_value=['Test camera'])
        self.names.start()
        self.addCleanup(self.names.stop)
        self.logs = mock.patch.object(camera_manager, '_lifecycle')
        self.logs.start()
        self.addCleanup(self.logs.stop)

    def tearDown(self):
        for gate in self.gates:
            gate.set()
        for worker in self.workers:
            worker.join(2)
            self.assertFalse(worker.is_alive())
        for cam in list(self.mgr._cams.values()):
            for client in list(cam.holders):
                self.mgr.release(cam.index, client)
        self.mgr._stop.set()
        if self.mgr._thread:
            self.mgr._thread.join(2)

    def spawn(self, operation):
        result = {}
        def run():
            try:
                result['value'] = operation()
            except BaseException as exc:
                result['error'] = exc
        worker = threading.Thread(target=run)
        self.workers.append(worker)
        worker.start()
        return worker, result

    def gate(self):
        gate = threading.Event()
        self.gates.append(gate)
        return gate

    def test_new_run_waits_until_old_read_and_release_finish(self):
        old_gate = self.gate()
        def open_capture(_index):
            self.assertTrue(all(cap.released for cap in self.opens), 'overlapping device opens')
            cap = FakeCap()
            if not self.opens:
                cap.gate = old_gate
            self.opens.append(cap)
            return cap
        with mock.patch.object(camera_manager, '_open_capture', side_effect=open_capture):
            self.mgr.acquire(0, 'program-old')
            self.assertTrue(self.opens[0].entered.wait(1))
            self.mgr.release(0, 'program-old')
            worker, result = self.spawn(lambda: self.mgr.acquire(0, 'program-new'))
            self.assertTrue(wait_until(lambda: 'program-new' in self.mgr._inflight))
            self.assertEqual(len(self.opens), 1)
            self.assertTrue(worker.is_alive())
            old_gate.set()
            worker.join(1)
            self.assertNotIn('error', result)
            self.assertTrue(result['value']['success'])
            self.assertEqual(len(self.opens), 2)
            self.assertFalse(self.opens[0].release_during_read)

    def test_new_run_waits_for_slow_native_release(self):
        closing = threading.Event()
        close_gate = self.gate()
        class SlowClose(FakeCap):
            def release(self):
                closing.set()
                close_gate.wait(2)
                super().release()
        def open_capture(_index):
            self.assertTrue(all(cap.released for cap in self.opens), 'open before release completed')
            cap = SlowClose() if not self.opens else FakeCap()
            self.opens.append(cap)
            return cap
        with mock.patch.object(camera_manager, '_open_capture', side_effect=open_capture), \
                mock.patch.object(self.mgr, '_ensure_reader'):
            self.mgr.acquire(0, 'program-old')
            self.spawn(lambda: self.mgr.release(0, 'program-old'))
            self.assertTrue(closing.wait(1))
            worker, result = self.spawn(lambda: self.mgr.acquire(0, 'program-new'))
            self.assertTrue(wait_until(lambda: 'program-new' in self.mgr._inflight))
            self.assertEqual(len(self.opens), 1)
            close_gate.set()
            worker.join(1)
            self.assertNotIn('error', result)
            self.assertTrue(result['value']['success'])

    def test_stuck_read_reports_busy_without_opening_another_capture(self):
        read_gate = self.gate()
        cap = FakeCap()
        cap.gate = read_gate
        with mock.patch.object(camera_manager, '_open_capture', return_value=cap) as opened:
            self.mgr.acquire(0, 'program-old')
            self.assertTrue(cap.entered.wait(1))
            self.mgr.release(0, 'program-old')
            with mock.patch.object(camera_manager, '_ACQUIRE_WAIT_SECONDS', 0.03):
                result = self.mgr.acquire(0, 'program-new')
            self.assertFalse(result['success'])
            self.assertIn('busy', result['error'])
            self.assertEqual(opened.call_count, 1)
            self.assertNotIn('program-new', self.mgr._inflight)
            read_gate.set()
            self.assertTrue(wait_until(lambda: cap.released))
            self.assertTrue(wait_until(lambda: 0 not in self.mgr._closing))

    def test_concurrent_connects_share_one_open(self):
        open_gate = self.gate()
        opening = threading.Event()
        cap = FakeCap()
        def open_capture(_index):
            opening.set()
            open_gate.wait(2)
            self.opens.append(cap)
            return cap
        with mock.patch.object(camera_manager, '_open_capture', side_effect=open_capture) as opened:
            first, first_result = self.spawn(lambda: self.mgr.acquire(0, 'program-first'))
            self.assertTrue(opening.wait(1))
            second, second_result = self.spawn(lambda: self.mgr.acquire(0, 'window-second'))
            self.assertTrue(wait_until(lambda: 'window-second' in self.mgr._inflight))
            self.assertEqual(opened.call_count, 1)
            open_gate.set()
            first.join(1)
            second.join(1)
            self.assertTrue(first_result['value']['success'], first_result)
            self.assertTrue(second_result['value']['success'], second_result)
            self.assertEqual(opened.call_count, 1)
            self.assertEqual(self.mgr._cams[0].holders, {'program-first', 'window-second'})

    def test_disconnect_cancels_a_connect_waiting_for_the_old_read(self):
        read_gate = self.gate()
        cap = FakeCap()
        cap.gate = read_gate
        with mock.patch.object(camera_manager, '_open_capture', return_value=cap) as opened:
            self.mgr.acquire(0, 'program-old')
            self.assertTrue(cap.entered.wait(1))
            self.mgr.release(0, 'program-old')
            worker, result = self.spawn(lambda: self.mgr.acquire(0, 'program-cancelled'))
            self.assertTrue(wait_until(lambda: 'program-cancelled' in self.mgr._inflight))
            self.mgr.release(0, 'program-cancelled')
            worker.join(0.3)
            self.assertFalse(worker.is_alive(), 'disconnect must wake a waiting connect')
            self.assertTrue(result['value']['cancelled'])
            self.assertEqual(opened.call_count, 1)

    def test_open_exception_does_not_leave_the_device_reserved(self):
        with mock.patch.object(camera_manager, '_open_capture', side_effect=RuntimeError('Driver failed')):
            with self.assertRaisesRegex(RuntimeError, 'Driver failed'):
                self.mgr.acquire(0, 'program-failed')
        self.assertEqual(self.mgr._opening, set())
        self.assertEqual(self.mgr._inflight, set())
        cap = FakeCap()
        self.opens.append(cap)
        with mock.patch.object(camera_manager, '_open_capture', return_value=cap):
            self.assertTrue(self.mgr.acquire(0, 'program-retry')['success'])


if __name__ == '__main__':
    unittest.main()

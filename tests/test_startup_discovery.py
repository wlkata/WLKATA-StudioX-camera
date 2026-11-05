"""Startup enumeration is shared and never opens a physical camera."""

import os
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'backend'))

import camera_manager
import main as camera_main
from flask import Flask


class StartupDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.manager = camera_manager.CameraManager()
        self.names = mock.patch.object(
            camera_manager, '_platform_names', return_value=['USB Camera']).start()
        self.open_capture = mock.patch.object(
            camera_manager, '_open_capture',
            side_effect=AssertionError('Discovery must never open hardware')).start()
        self.manager_patch = mock.patch.object(
            camera_main, '_manager', return_value=self.manager)
        self.manager_patch.start()
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.addCleanup(mock.patch.stopall)
        self.addCleanup(self.finish_discovery)

    def finish_discovery(self):
        self.gate.set()
        current = self.manager._discovery
        if current is not None:
            try:
                current.result(timeout=2)
            except Exception:
                pass
        self.open_capture.assert_not_called()

    def slow_names(self):
        self.entered.set()
        if not self.gate.wait(2):
            raise RuntimeError('Test did not release discovery')
        return ['USB Camera']

    def app(self):
        app = Flask(__name__)
        app.register_blueprint(camera_main.blueprint)
        # The host invokes this hook after registration. Tests keep its Future
        # pending to exercise requests arriving before discovery completes.
        camera_main.on_start()
        return app

    def test_startup_hook_starts_discovery_without_a_request(self):
        self.names.side_effect = self.slow_names
        self.app()
        self.assertTrue(self.entered.wait(1))
        self.assertFalse(self.manager._discovery.done())
        self.gate.set()
        self.assertEqual(self.manager._discovery.result(timeout=1), ['USB Camera'])
        self.assertEqual(self.names.call_count, 1)
        self.assertFalse(self.manager._cams)

    def test_blueprint_registration_alone_does_not_start_discovery(self):
        app = Flask(__name__)
        app.register_blueprint(camera_main.blueprint)
        self.names.assert_not_called()
        self.assertIsNone(self.manager._discovery)

    def test_early_http_list_waits_for_startup_scan(self):
        self.names.side_effect = self.slow_names
        app = self.app()
        self.assertTrue(self.entered.wait(1))
        waiting = threading.Event()
        def list_request():
            waiting.set()
            with app.test_client() as client:
                return client.get('/cameras')
        with ThreadPoolExecutor(max_workers=1) as pool:
            request = pool.submit(list_request)
            try:
                self.assertTrue(waiting.wait(1))
                self.assertFalse(request.done())
            finally:
                self.gate.set()
            response = request.result(timeout=1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()['cameras'][0]['name'], 'USB Camera')
        self.assertFalse(response.get_json()['cameras'][0]['in_use'])
        self.assertEqual(self.names.call_count, 1)

    def test_refresh_and_startup_callers_share_the_inflight_scan(self):
        self.names.side_effect = self.slow_names
        initial = self.manager.start_discovery()
        self.assertTrue(self.entered.wait(1))
        with ThreadPoolExecutor(max_workers=6) as pool:
            calls = [pool.submit(self.manager.start_discovery, refresh=True)
                     for _ in range(6)]
            self.assertTrue(all(call.result(timeout=1) is initial for call in calls))
        self.gate.set()
        self.assertEqual(self.manager.list_cameras()['cameras'][0]['id'], 0)
        self.assertIs(self.manager.start_discovery(), initial)
        self.assertEqual(self.names.call_count, 1)

    def test_completed_empty_scan_is_cached_until_explicit_refresh(self):
        self.names.return_value = []
        self.app()
        self.assertEqual(self.manager.list_cameras()['cameras'], [])
        self.names.return_value = ['New USB Camera']
        for _ in range(3):
            self.manager.start_discovery()
            self.assertEqual(self.manager.list_cameras()['cameras'], [])
        self.assertEqual(self.names.call_count, 1)
        listed = self.manager.list_cameras(refresh=True)
        self.assertEqual(listed['cameras'][0]['name'], 'New USB Camera')
        self.assertEqual(self.names.call_count, 2)

    def test_subsequent_gets_use_the_completed_cache(self):
        app = self.app()
        with app.test_client() as client:
            for _ in range(4):
                response = client.get('/cameras')
                self.assertEqual(response.get_json()['cameras'][0]['name'], 'USB Camera')
        self.assertEqual(self.names.call_count, 1)

    def test_http_refresh_joins_pending_startup(self):
        self.names.side_effect = self.slow_names
        app = self.app()
        self.assertTrue(self.entered.wait(1))
        with mock.patch.object(camera_manager, '_DISCOVERY_WAIT_SECONDS', 0.01):
            with app.test_client() as client:
                response = client.get('/cameras?refresh=1')
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()['success'])
        self.assertIn('still running', response.get_json()['error'])
        self.assertNotIn('cameras', response.get_json())
        self.assertEqual(self.names.call_count, 1)
        self.gate.set()
        with app.test_client() as client:
            recovered = client.get('/cameras').get_json()
        self.assertTrue(recovered['success'])
        self.assertEqual(recovered['cameras'][0]['name'], 'USB Camera')
        self.assertEqual(self.names.call_count, 1)

    def test_failed_scan_is_an_error_and_refresh_can_retry(self):
        self.names.side_effect = RuntimeError('Enumeration failed')
        app = self.app()
        with app.test_client() as client:
            failed = client.get('/cameras')
            self.assertEqual(failed.status_code, 503)
            self.assertEqual(failed.get_json(), {
                'success': False, 'error': 'Enumeration failed'})
            self.names.side_effect = None
            recovered = client.get('/cameras?refresh=1')
        self.assertEqual(recovered.status_code, 200)
        self.assertEqual(recovered.get_json()['cameras'][0]['name'], 'USB Camera')
        self.assertEqual(self.names.call_count, 2)


if __name__ == '__main__':
    unittest.main()

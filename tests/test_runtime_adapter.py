"""Camera runtime adapter talks HTTP and never opens cv2.VideoCapture."""

import importlib.util
import json
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADAPTER_PATH = os.path.join(ROOT, 'runtime', 'adapter.py')


class Cancelled(BaseException):
    def __init__(self, reason='stop'):
        self.reason = reason
        super().__init__(reason)


class Response:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    def json(self):
        return json.loads(self.body.decode('utf-8'))


class HttpFailure(Exception):
    def __init__(self, response):
        self.response = response
        super().__init__('HTTP %s' % response.status)


class FakeCtx:
    def __init__(self):
        self.session_id = 'sess-9'
        self.extension_id = 'camera'
        self.calls = []
        self.errors = []
        self.aborted = False
        self._queue = []

    def check_aborted(self):
        if self.aborted:
            raise Cancelled('stop')

    def push(self, status, headers, body):
        self._queue.append(Response(status, headers, body))

    def request(self, method, relative_path, json=None, timeout=None, ignore_abort=False):
        if not ignore_abort:
            self.check_aborted()
        response = self._queue.pop(0) if self._queue else Response(
            200, {'Content-Type': 'application/json'}, b'{"success": true}',
        )
        self.calls.append((method, relative_path, json, ignore_abort))
        if not ignore_abort:
            self.check_aborted()
        if response.status >= 400:
            raise HttpFailure(response)
        return response

    def report_error(self, message):
        self.errors.append(message)


def _load():
    spec = importlib.util.spec_from_file_location('camera_runtime_adapter_test', ADAPTER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CameraAdapterTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def test_source_does_not_construct_a_capture(self):
        with open(ADAPTER_PATH, encoding='utf-8') as handle:
            text = handle.read()
        self.assertNotIn('VideoCapture(', text)
        self.assertNotIn('5080', text)
        self.assertIn('ignore_abort=True', text)

    def test_empty_and_invalid_indexes_do_not_connect(self):
        ctx = FakeCtx()
        adapter = self.mod.create_session(ctx)
        for value in ('', None, 'front', -1, True):
            with self.assertRaises(RuntimeError):
                adapter.open_camera(value)
        self.assertEqual(ctx.calls, [])

    def test_connect_uses_the_session_and_failed_open_disconnects(self):
        ctx = FakeCtx()
        ctx.push(200, {'Content-Type': 'application/json'}, b'{"success": true}')
        adapter = self.mod.create_session(ctx)
        cap = adapter.open_camera(0)
        method, path, payload, _ignore = ctx.calls[0]
        self.assertEqual((method, path), ('POST', '/connect'))
        self.assertEqual(payload['id'], 0)
        self.assertTrue(payload['client'].startswith('program-sess-9-'))
        self.assertNotIn('window', payload['client'])
        self.assertTrue(cap.isOpened())
        self.assertFalse(cap.set(1, 2))
        self.assertEqual(cap.get(1), 0)
        self.assertTrue(cap.grab())
        cap.release()
        cap.release()
        disconnects = [call for call in ctx.calls if call[1] == '/disconnect']
        self.assertEqual(len(disconnects), 1)
        self.assertEqual(disconnects[0][2]['client'], payload['client'])
        self.assertTrue(disconnects[0][3])

        ctx.calls.clear()
        ctx.push(200, {'Content-Type': 'application/json'}, b'{"success": false, "error": "busy"}')
        with self.assertRaises(RuntimeError) as caught:
            adapter.open_camera('1')
        self.assertIn('busy', str(caught.exception))
        self.assertEqual(ctx.errors, ['busy'])
        self.assertEqual(ctx.calls[0][2]['id'], 1)
        self.assertEqual(ctx.calls[1][1], '/disconnect')

    def test_stop_after_connect_still_disconnects_that_client(self):
        ctx = FakeCtx()

        def connect_then_stop(method, relative_path, payload, ignore_abort):
            if relative_path == '/connect':
                ctx.aborted = True
            return Response(200, {'Content-Type': 'application/json'}, b'{"success": true}')

        ctx.request = _recording_request(ctx, connect_then_stop)
        adapter = self.mod.create_session(ctx)
        with self.assertRaises(Cancelled):
            adapter.open_camera(0)
        paths = [call[1] for call in ctx.calls]
        self.assertEqual(paths, ['/connect', '/disconnect'])
        self.assertTrue(ctx.calls[1][3])

    def test_read_retries_a_missing_frame_then_decodes_jpeg(self):
        try:
            import cv2
            import numpy as np
        except Exception:
            self.skipTest('OpenCV is not installed in this Python')
        image = np.zeros((4, 4, 3), dtype=np.uint8)
        image[:, :] = (10, 20, 30)
        ok, encoded = cv2.imencode('.jpg', image)
        self.assertTrue(ok)
        ctx = FakeCtx()
        ctx.push(200, {'Content-Type': 'application/json'}, b'{"success": true}')
        ctx.push(404, {'Content-Type': 'application/json'}, b'{"error": "No camera frame"}')
        ctx.push(200, {'Content-Type': 'image/jpeg'}, bytes(encoded))
        adapter = self.mod.create_session(ctx)
        cap = adapter.open_camera(2)
        ok, frame = cap.read()
        self.assertTrue(ok)
        self.assertEqual(frame.shape, (4, 4, 3))
        self.assertLess(int(abs(int(frame[0, 0, 0]) - 10)), 8)
        frame_calls = [call for call in ctx.calls if call[1].startswith('/frame')]
        self.assertEqual(len(frame_calls), 2)
        self.assertEqual(frame_calls[0][1], '/frame?id=2')
        adapter.close()
        adapter.close()
        self.assertEqual([call[1] for call in ctx.calls].count('/disconnect'), 1)

    def test_blocks_open_through_the_adapter(self):
        with open(os.path.join(ROOT, 'extension.json'), encoding='utf-8') as handle:
            manifest = json.load(handle)
        with open(os.path.join(ROOT, 'blocks', 'blocks.json'), encoding='utf-8') as handle:
            blocks = json.load(handle)
        self.assertEqual(manifest['contributes']['runtime']['python'], 'runtime/adapter.py')
        setup = next(block for block in blocks if block['type'] == 'camera_setup')
        self.assertNotIn('pythonImports', setup)
        self.assertIn(
            "__studiox_extensions['camera'].open_camera({CAMERA})",
            setup['studiox']['python'],
        )
        self.assertIn('cv2.VideoCapture({CAMERA})', setup['portable']['python'])
        self.assertIn('{CAP}.release()', setup['portable']['cleanup'])
        preview = next(block for block in blocks if block['type'] == 'camera_preview')
        self.assertEqual(preview.get('codegen'), 'debug')


def _recording_request(ctx, handler):
    def request(method, relative_path, json=None, timeout=None, ignore_abort=False):
        if not ignore_abort:
            ctx.check_aborted()
        response = handler(method, relative_path, json, ignore_abort)
        ctx.calls.append((method, relative_path, json, ignore_abort))
        if not ignore_abort:
            ctx.check_aborted()
        if response.status >= 400:
            raise HttpFailure(response)
        return response
    return request


if __name__ == '__main__':
    unittest.main()

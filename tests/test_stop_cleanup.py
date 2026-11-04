"""Cancellation at any adapter boundary must leave no camera holder."""

import dis
import inspect
import sys
import unittest

from test_runtime_adapter import Cancelled, FakeCtx, _load


class TrackingCtx(FakeCtx):
    def __init__(self):
        super().__init__()
        self.holders = set()
        self.lose_disconnect_ack = False

    def request(self, method, relative_path, json=None, timeout=None, ignore_abort=False):
        response = super().request(method, relative_path, json, timeout, ignore_abort)
        payload = response.json()
        if payload.get('success'):
            if relative_path == '/connect':
                self.holders.add(json['client'])
            elif relative_path == '/disconnect':
                self.holders.discard(json['client'])
                if self.lose_disconnect_ack:
                    self.lose_disconnect_ack = False
                    raise OSError('Disconnect acknowledgement lost')
        return response


def interrupt_at(function, line):
    def trace(frame, event, _arg):
        if event == 'line' and frame.f_code is function.__code__ and frame.f_lineno == line:
            raise Cancelled()
        return trace
    return trace


def source_line(function, text):
    lines, start = inspect.getsourcelines(function)
    return next(start + i for i, line in enumerate(lines) if text in line)


class StopCleanupTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load()

    def _interrupt(self, function, line, operation):
        sys.settrace(interrupt_at(function, line))
        try:
            operation()
        except Cancelled:
            pass
        finally:
            sys.settrace(None)

    def test_stop_between_connect_and_adapter_registration(self):
        ctx = TrackingCtx()
        adapter = self.mod.create_session(ctx)
        self._interrupt(
            self.mod.CameraExecutionAdapter.open_camera,
            source_line(self.mod.CameraExecutionAdapter.open_camera, 'self._captures.append(cap)'),
            lambda: adapter.open_camera(0),
        )
        self.assertEqual(len(ctx.holders), 1, 'the backend acquired a holder before Stop')
        self.assertEqual(adapter._captures, [], 'the adapter never received that capture')
        ctx.close_all(adapter)
        self.assertEqual(ctx.holders, set())
        self.assertEqual([call[1] for call in ctx.calls].count('/disconnect'), 1)

    def test_stop_after_local_release_does_not_lose_backend_cleanup(self):
        ctx = TrackingCtx()
        adapter = self.mod.create_session(ctx)
        cap = adapter.open_camera(0)
        self._interrupt(
            self.mod.ManagedCameraCapture.release,
            source_line(self.mod.ManagedCameraCapture.release, 'self._disconnect()'),
            cap.release,
        )
        self.assertFalse(cap.isOpened())
        self.assertEqual(len(ctx.holders), 1)
        ctx.close_all(adapter)
        self.assertEqual(ctx.holders, set())

    def test_stop_at_every_constructor_open_and_release_line(self):
        # This uses the same BaseException/trace behavior as StudioX Stop.
        functions = [
            self.mod.ManagedCameraCapture.__init__,
            self.mod.CameraExecutionAdapter.open_camera,
            self.mod.ManagedCameraCapture.release,
        ]
        for function in functions:
            for line in sorted({line for _, line in dis.findlinestarts(function.__code__)}):
                with self.subTest(function=function.__qualname__, line=line):
                    ctx = TrackingCtx()
                    adapter = self.mod.create_session(ctx)
                    if function is self.mod.ManagedCameraCapture.release:
                        cap = adapter.open_camera(0)
                        operation = cap.release
                    else:
                        operation = lambda: adapter.open_camera(0)
                    self._interrupt(function, line, operation)
                    ctx.close_all(adapter)
                    self.assertEqual(ctx.holders, set())

    def test_failed_disconnect_is_retried_during_session_cleanup(self):
        ctx = TrackingCtx()
        adapter = self.mod.create_session(ctx)
        cap = adapter.open_camera(0)
        ctx.push(503, {'Content-Type': 'application/json'}, b'{"error":"Temporary failure"}')
        cap.release()
        self.assertFalse(cap.isOpened())
        self.assertEqual(len(ctx.holders), 1)
        ctx.close_all(adapter)
        self.assertEqual(ctx.holders, set())
        self.assertEqual([call[1] for call in ctx.calls].count('/disconnect'), 2)

    def test_lost_disconnect_ack_is_retried_idempotently(self):
        ctx = TrackingCtx()
        adapter = self.mod.create_session(ctx)
        cap = adapter.open_camera(0)
        ctx.lose_disconnect_ack = True
        cap.release()
        self.assertEqual(ctx.holders, set(), 'backend disconnected despite the lost response')
        ctx.close_all(adapter)
        ctx.close_all(adapter)
        disconnects = [call for call in ctx.calls if call[1] == '/disconnect']
        self.assertEqual(len(disconnects), 2)
        self.assertEqual(disconnects[0][2], disconnects[1][2])

    def test_unsuccessful_disconnect_response_keeps_cleanup_pending(self):
        ctx = TrackingCtx()
        adapter = self.mod.create_session(ctx)
        cap = adapter.open_camera(0)
        ctx.push(200, {'Content-Type': 'application/json'}, b'{"success":false}')
        cap.release()
        self.assertEqual(len(ctx.holders), 1)
        ctx.close_all(adapter)
        self.assertEqual(ctx.holders, set())


if __name__ == '__main__':
    unittest.main()

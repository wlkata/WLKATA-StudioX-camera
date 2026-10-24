"""Camera extension HTTP API."""

import time

from flask import Blueprint, Response, jsonify, request

from camera_manager import CameraManager

blueprint = Blueprint('camera', __name__)


def _manager():
    return CameraManager.get_instance()


@blueprint.route('/cameras')
def cameras():
    refresh = request.args.get('refresh') in ('1', 'true', 'yes')
    info = _manager().list_cameras(refresh=refresh)
    return jsonify({'success': True, **info})


@blueprint.route('/connect', methods=['POST'])
def connect():
    data = request.get_json(silent=True) or {}
    if data.get('id') is None:
        return jsonify({'success': False, 'error': 'id is required'}), 400
    try:
        result = _manager().acquire(data.get('id'), data.get('client') or 'window')
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500
    status = 200 if result.get('success') else 400
    return jsonify(result), status


@blueprint.route('/disconnect', methods=['POST'])
def disconnect():
    data = request.get_json(silent=True) or {}
    try:
        result = _manager().release(data.get('id'), data.get('client') or 'window')
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500
    return jsonify(result)


def _camera_index():
    raw = request.args.get('id')
    if raw is None or raw == '':
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@blueprint.route('/stream')
def stream():
    """MJPEG of one shared capture."""
    index = _camera_index()

    def generate():
        for _ in range(80):
            if _manager().latest_jpeg(index):
                break
            time.sleep(0.02)
        while True:
            jpeg = _manager().latest_jpeg(index)
            if jpeg:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n')
            time.sleep(1.0 / 30)

    return Response(generate(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@blueprint.route('/frame')
def frame():
    jpeg = _manager().latest_jpeg(_camera_index())
    if not jpeg:
        return jsonify({'success': False, 'error': 'No camera frame'}), 404
    return Response(jpeg, mimetype='image/jpeg')


@blueprint.route('/capture', methods=['POST'])
def capture():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_manager().capture(data.get('id')))
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400


@blueprint.route('/preview', methods=['POST'])
def preview():
    data = request.get_json(silent=True) or {}
    try:
        if data.get('frames') is not None:
            _manager().set_previews(data.get('frames'))
        else:
            _manager().set_preview(data.get('slot') or 1, data.get('image') or '')
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400
    return jsonify({'success': True})


@blueprint.route('/previews')
def previews():
    payload = _manager().previews()
    return jsonify({'success': True, 'rev': payload['rev'], 'frames': payload['frames']})


@blueprint.route('/resize', methods=['POST'])
def resize():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_manager().resize(
            data.get('image'), data.get('width'), data.get('height')))
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400


@blueprint.route('/convert', methods=['POST'])
def convert():
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(_manager().convert(data.get('image'), data.get('mode')))
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400

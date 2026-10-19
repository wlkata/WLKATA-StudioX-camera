"""Camera extension HTTP API."""

import time

from flask import Blueprint, Response, jsonify, request

from camera_manager import CameraManager

blueprint = Blueprint('camera', __name__)


def _manager():
    return CameraManager.get_instance()


@blueprint.route('/cameras')
def cameras():
    info = _manager().list_cameras()
    return jsonify({'success': True, **info})


@blueprint.route('/connect', methods=['POST'])
def connect():
    data = request.get_json(silent=True) or {}
    if data.get('id') is None:
        return jsonify({'success': False, 'error': 'id is required'}), 400
    try:
        result = _manager().connect(data.get('id'))
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500
    status = 200 if result.get('success') else 400
    return jsonify(result), status


@blueprint.route('/disconnect', methods=['POST'])
def disconnect():
    data = request.get_json(silent=True) or {}
    try:
        result = _manager().disconnect(data.get('id'))
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 500
    return jsonify(result)


@blueprint.route('/stream')
def stream():
    """MJPEG stream of the active camera. One long request, like cv-pick."""
    def generate():
        for _ in range(80):
            if _manager().latest_jpeg():
                break
            time.sleep(0.02)
        while True:
            jpeg = _manager().latest_jpeg()
            if jpeg:
                yield (b'--frame\r\n'
                       b'Content-Type: image/jpeg\r\n\r\n' + jpeg + b'\r\n')
            time.sleep(1.0 / 30)

    return Response(generate(),
                    mimetype='multipart/x-mixed-replace; boundary=frame')


@blueprint.route('/frame')
def frame():
    jpeg = _manager().latest_jpeg()
    if not jpeg:
        return jsonify({'success': False, 'error': 'No camera frame'}), 404
    return Response(jpeg, mimetype='image/jpeg')


@blueprint.route('/capture', methods=['POST'])
def capture():
    try:
        return jsonify(_manager().capture())
    except Exception as exc:
        return jsonify({'success': False, 'error': str(exc)}), 400


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

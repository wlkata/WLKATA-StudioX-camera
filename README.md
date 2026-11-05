# Camera

StudioX extension for a shared camera. It needs StudioX extension API version 1.

Three pieces stay separate:

* **Camera window** (`frontend/`). Window menu → Camera. Connect and Disconnect are explicit. Closing the window or stopping a Blockly program does not connect a camera, and Disconnect drops only the `window` holder.
* **Camera backend** (`backend/`). This process owns the device. One capture stays open while any holder remains, and it closes only after a read that is already in progress returns. At app startup the host calls `on_start()`, which discovers device names in the background without opening a camera. The hook returns a Future; StudioX waits for discovery before starting dependent backends such as CV Pick. `GET /cameras` waits for that initial scan if necessary, then returns cached names. CV Pick can list cameras without opening Window → Camera. Concurrent discovery requests share one scan. `GET /cameras?refresh=1` asks the operating system for names and still does not open a capture.
* **Runtime adapter** (`runtime/adapter.py`). StudioX loads this into the Blockly process for each run. `open_camera` is an HTTP client of this backend. It never constructs `cv2.VideoCapture`. JPEG decode uses OpenCV from the StudioX process, not from the `camera` environment.

Install `requirements.txt` into an environment named `camera`. That environment is the backend. The adapter does not import it.

Blockly `camera_setup` generates `__studiox_extensions['camera'].open_camera(...)`. Capture is still `cap.read()[1]`. Resize and convert still call OpenCV. BGR is the image unchanged.

Program cleanup is registered before the connection request, so Stop can release
a camera even if it interrupts setup before the capture is returned. Disconnect
remains pending until the backend acknowledges it; a failed or interrupted
release can be retried by session cleanup.

Opening a device waits for its previous read and native release to finish.
Concurrent connects share one capture. If a previous operation stays blocked for
five seconds, connect returns a busy error rather than reporting a connection
that cannot produce frames.

## Tests

Run with the Camera environment's Python:

```bash
python -B -m unittest discover -s tests -p 'test_*.py'
node tests/test_window_attempts.js
```

Tests cover Stop at adapter execution boundaries, disconnect retries, concurrent
connects, blocked reads and releases, and twenty rapid Run/Stop cycles with real
JPEG encoding/decoding and simulated camera captures. Physical camera testing
is still needed for platform and driver behavior.

## Development and install

This repository is the source of truth. StudioX prefers `~/.wlkata-studiox/extensions/camera` over a checkout linked from the app. From the Blockly repo, copy a tested commit in and restart StudioX:

```bash
scripts/sync-camera-extension.sh /path/to/WLKATA-StudioX-camera
```

The manifest id is `camera`, the folder name must be `camera`, and `manifestVersion` is 2.

The startup hook requires a StudioX host with backend lifecycle support. Hooks run once per backend process; a restarted backend scans again. No sidebar tab or camera window needs to be opened.

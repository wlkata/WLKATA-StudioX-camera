# Camera

StudioX extension for a shared camera. It needs StudioX extension API version 1.

Three pieces stay separate:

* **Camera window** (`frontend/`). Window menu → Camera. Connect and Disconnect are explicit. Closing the window or stopping a Blockly program does not connect a camera, and Disconnect drops only the `window` holder.
* **Camera backend** (`backend/`). This process owns the device. One capture stays open while any holder remains, and it closes only after a read that is already in progress returns. `GET /cameras` returns cached names. `GET /cameras?refresh=1` asks the operating system for names and still does not open a capture.
* **Runtime adapter** (`runtime/adapter.py`). StudioX loads this into the Blockly process for each run. `open_camera` is an HTTP client of this backend. It never constructs `cv2.VideoCapture`. JPEG decode uses OpenCV from the StudioX process, not from the `camera` environment.

Install `requirements.txt` into an environment named `camera`. That environment is the backend. The adapter does not import it.

Blockly `camera_setup` generates `__studiox_extensions['camera'].open_camera(...)`. Capture is still `cap.read()[1]`. Resize and convert still call OpenCV. BGR is the image unchanged.

## Development and install

This repository is the source of truth. StudioX prefers `~/.wlkata-studiox/extensions/camera` over a checkout linked from the app. From the Blockly repo, copy a tested commit in and restart StudioX:

```bash
scripts/sync-camera-extension.sh /path/to/WLKATA-StudioX-camera
```

The manifest id is `camera`, the folder name must be `camera`, and `manifestVersion` is 2.

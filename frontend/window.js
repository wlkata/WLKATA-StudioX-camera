(function () {
  var port = window.STUDIOX_PORT || '5080';
  var base = 'http://127.0.0.1:' + port + '/ext/camera';
  var select = document.getElementById('cam-select');
  var connectBtn = document.getElementById('cam-connect');
  var disconnectBtn = document.getElementById('cam-disconnect');
  var statusEl = document.getElementById('cam-status');
  var frame = document.getElementById('cam-frame');
  var empty = document.getElementById('cam-empty');
  var attempt = null;
  var closing = false;
  var listRequest = null;

  function newClientId() {
    var suffix;
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      suffix = window.crypto.randomUUID();
    } else {
      suffix = String(Date.now()) + '-' + Math.random().toString(16).slice(2);
    }
    return 'window-' + suffix;
  }

  function setIdle() {
    if (select) select.disabled = false;
    if (connectBtn) connectBtn.disabled = false;
    if (disconnectBtn) disconnectBtn.disabled = true;
  }

  function setBusy() {
    if (select) select.disabled = true;
    if (connectBtn) connectBtn.disabled = true;
    if (disconnectBtn) disconnectBtn.disabled = false;
  }

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  function showFrame(on) {
    if (frame) frame.style.display = on ? 'block' : 'none';
    if (empty) empty.style.display = on ? 'none' : 'block';
  }

  function refreshList(refreshDevices) {
    if (listRequest) return listRequest;
    var query = refreshDevices ? '?refresh=1' : '';
    listRequest = fetch(base + '/cameras' + query)
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || !data.success) return;
        var selected = select.value;
        var names = {};
        select.innerHTML = '';
        (data.cameras || []).forEach(function (cam) {
          var opt = document.createElement('option');
          opt.value = String(cam.id);
          var note = cam.program ? ' (in use by program)' : '';
          opt.textContent = cam.name + note;
          select.appendChild(opt);
          names[String(cam.id)] = cam.name;
        });
        if (attempt && attempt.phase === 'connected') {
          select.value = String(attempt.cameraId);
          setBusy();
          setStatus('Showing ' + (names[String(attempt.cameraId)] || ('camera ' + attempt.cameraId)));
          var stream = base + '/stream?id=' + attempt.cameraId;
          if (frame && frame.getAttribute('data-stream') !== stream) {
            frame.setAttribute('data-stream', stream);
            frame.src = stream;
            showFrame(true);
          }
        } else if (attempt && attempt.phase === 'pending') {
          setBusy();
          setStatus('Connecting…');
        } else {
          if (selected) select.value = selected;
          setIdle();
          setStatus('No camera connected');
          if (frame) {
            frame.removeAttribute('src');
            frame.removeAttribute('data-stream');
          }
          showFrame(false);
        }
      })
      .catch(function () {
        setStatus('Camera backend is not running');
      })
      .then(function () {
        listRequest = null;
      });
    return listRequest;
  }

  function stopStream() {
    if (!frame) return;
    frame.removeAttribute('src');
    frame.removeAttribute('data-stream');
    showFrame(false);
  }

  function sendDisconnect(target, keepalive) {
    if (!target) return;
    var options = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: Number(target.cameraId), client: target.client })
    };
    if (keepalive) options.keepalive = true;
    try {
      fetch(base + '/disconnect', options);
    } catch (e) { /* ignore */ }
  }

  connectBtn.addEventListener('click', function () {
    if (closing || (attempt && attempt.phase === 'pending')) return;
    if (attempt && attempt.phase === 'connected') return;
    var cameraId = Number(select.value);
    if (!isFinite(cameraId)) return;
    var client = newClientId();
    attempt = { cameraId: cameraId, client: client, phase: 'pending' };
    setBusy();
    setStatus('Connecting…');
    fetch(base + '/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: cameraId, client: client })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (closing || !attempt || attempt.client !== client) return;
        if (!data || !data.success) {
          attempt = null;
          setIdle();
          setStatus((data && data.error) || 'Connect failed');
          return;
        }
        attempt.phase = 'connected';
        if (data.id != null) attempt.cameraId = Number(data.id);
        refreshList(true);
      })
      .catch(function () {
        if (closing || !attempt || attempt.client !== client) return;
        attempt = null;
        setIdle();
        setStatus('Connect failed');
      });
  });

  disconnectBtn.addEventListener('click', function () {
    var current = attempt;
    attempt = null;
    stopStream();
    setIdle();
    setStatus('No camera connected');
    if (!current) return;
    var options = {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: Number(current.cameraId), client: current.client })
    };
    fetch(base + '/disconnect', options).then(function () {
      if (!attempt) refreshList(true);
    }).catch(function () {});
  });

  // keepalive covers a normal close. A renderer crash can still skip this.
  window.addEventListener('beforeunload', function () {
    closing = true;
    sendDisconnect(attempt, true);
  });

  refreshList(true);
})();

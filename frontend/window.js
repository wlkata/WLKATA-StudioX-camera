(function () {
  var port = window.STUDIOX_PORT || '5080';
  var base = 'http://127.0.0.1:' + port + '/ext/camera';
  var select = document.getElementById('cam-select');
  var connectBtn = document.getElementById('cam-connect');
  var disconnectBtn = document.getElementById('cam-disconnect');
  var statusEl = document.getElementById('cam-status');
  var frame = document.getElementById('cam-frame');
  var empty = document.getElementById('cam-empty');
  var windowId = null;

  function setConnected(on) {
    if (select) select.disabled = on;
    if (connectBtn) connectBtn.disabled = on;
    if (disconnectBtn) disconnectBtn.disabled = !on;
  }

  function setStatus(text) {
    if (statusEl) statusEl.textContent = text;
  }

  function showFrame(on) {
    if (frame) frame.style.display = on ? 'block' : 'none';
    if (empty) empty.style.display = on ? 'none' : 'block';
  }

  function refreshList() {
    fetch(base + '/cameras')
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
        if (windowId !== null) {
          select.value = String(windowId);
          setConnected(true);
          setStatus('Showing ' + (names[String(windowId)] || ('camera ' + windowId)));
          var stream = base + '/stream?id=' + windowId;
          if (frame && frame.getAttribute('data-stream') !== stream) {
            frame.setAttribute('data-stream', stream);
            frame.src = stream;
            showFrame(true);
          }
        } else {
          if (selected) select.value = selected;
          setConnected(false);
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
      });
  }

  connectBtn.addEventListener('click', function () {
    fetch(base + '/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: Number(select.value), client: 'window' })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.success) {
          setStatus(data.error || 'Connect failed');
        } else {
          windowId = String(data.id);
        }
        refreshList();
      })
      .catch(function () { setStatus('Connect failed'); });
  });

  disconnectBtn.addEventListener('click', function () {
    fetch(base + '/disconnect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: Number(windowId), client: 'window' })
    }).then(function () {
      windowId = null;
      refreshList();
    });
  });

  window.addEventListener('beforeunload', function () {
    if (windowId === null) return;
    try {
      fetch(base + '/disconnect', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id: Number(windowId), client: 'window' }),
        keepalive: true
      });
    } catch (e) { /* ignore */ }
  });

  refreshList();
  setInterval(refreshList, 2000);
})();

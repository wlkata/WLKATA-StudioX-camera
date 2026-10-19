(function () {
  var port = window.STUDIOX_PORT || '5080';
  var base = 'http://127.0.0.1:' + port + '/ext/camera';
  var select = document.getElementById('cam-select');
  var statusEl = document.getElementById('cam-status');
  var frame = document.getElementById('cam-frame');
  var empty = document.getElementById('cam-empty');
  var activeId = null;

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
        select.innerHTML = '';
        (data.cameras || []).forEach(function (cam) {
          var opt = document.createElement('option');
          opt.value = String(cam.id);
          opt.textContent = cam.name + (cam.connected ? ' (connected)' : '');
          select.appendChild(opt);
        });
        activeId = data.active;
        if (activeId !== null && activeId !== undefined) {
          select.value = String(activeId);
        } else if (selected) {
          select.value = selected;
        }
        if (activeId === null || activeId === undefined) {
          setStatus('No camera connected');
          showFrame(false);
        } else {
          setStatus('Showing camera ' + activeId);
        }
      })
      .catch(function () {
        setStatus('Camera backend is not running');
      });
  }

  function refreshFrame() {
    if (activeId === null || activeId === undefined) {
      if (frame) frame.src = '';
      showFrame(false);
      return;
    }
    var stream = base + '/stream';
    if (frame && frame.getAttribute('data-stream') !== stream) {
      frame.setAttribute('data-stream', stream);
      frame.src = stream;
    }
    showFrame(true);
  }

  document.getElementById('cam-connect').addEventListener('click', function () {
    fetch(base + '/connect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: Number(select.value) })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data.success) setStatus(data.error || 'Connect failed');
        refreshList();
      })
      .catch(function () { setStatus('Connect failed'); });
  });

  document.getElementById('cam-disconnect').addEventListener('click', function () {
    fetch(base + '/disconnect', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: Number(select.value) })
    }).then(function () { refreshList(); });
  });

  refreshList();
  setInterval(refreshList, 2000);
  setInterval(refreshFrame, 150);
})();

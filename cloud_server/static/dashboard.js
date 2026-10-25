'use strict';

const $ = id => document.getElementById(id);
const viewTitles = {home: 'Tổng quan', users: 'Quản lý người dùng', history: 'Nhật ký nhận diện', system: 'Nhật ký kết nối', settings: 'Cài đặt hệ thống'};
let recognitionLogs = [];
let connectionEvents = [];
let movedContent = null;
let toastTimer;
let imageReturnFocus = null;
let samplesSummaryVersion = 0;
const syncState = {logs: null, cameras: null, connections: null};
const pending = new Set();

function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = String(text);
  if (className) element.className = className;
  return element;
}

function icon(name) {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.classList.add('icon');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS('http://www.w3.org/2000/svg', 'use');
  use.setAttribute('href', '#i-' + name);
  svg.append(use);
  return svg;
}

function normalize(value) {
  return String(value || '').toLocaleLowerCase('vi').normalize('NFD')
    .replace(/[\u0300-\u036f]/g, '').replace(/đ/g, 'd').trim();
}

function restoreContent() {
  if (!movedContent) return;
  movedContent.parent.append(movedContent.element);
  movedContent = null;
}

function showView(name, updateHash = true) {
  if (!Object.hasOwn(viewTitles, name)) name = 'home';
  if ($('workspace-dialog').open) {
    restoreContent();
    $('workspace-dialog').close();
  }
  document.querySelectorAll('main > .view').forEach(view => { view.hidden = view.id !== 'view-' + name; });
  document.querySelectorAll('.nav').forEach(button => {
    const active = button.dataset.view === name;
    button.classList.toggle('active', active);
    if (active) button.setAttribute('aria-current', 'page');
    else button.removeAttribute('aria-current');
  });
  $('breadcrumb-view').textContent = viewTitles[name];
  if (updateHash && location.hash !== '#' + name) history.replaceState(null, '', '#' + name);
}

function openQuick(name) {
  if (name === 'register') {
    $('register-alert').hidden = true;
    $('register-dialog').showModal();
    $('register-form').elements.name.focus();
    return;
  }
  if (!['users', 'history', 'settings'].includes(name)) return;
  const content = $(name + '-content');
  movedContent = {element: content, parent: content.parentElement};
  $('workspace-dialog-title').textContent = viewTitles[name];
  $('workspace-dialog-body').append(content);
  $('workspace-dialog').showModal();
  const search = content.querySelector('input[type="search"]');
  if (search) search.focus();
}

$('workspace-dialog').addEventListener('close', restoreContent);
document.querySelectorAll('dialog').forEach(dialog => {
  dialog.addEventListener('cancel', event => {
    if (dialog.dataset.busy === 'true') event.preventDefault();
  });
  // Only clicks outside the dialog rectangle dismiss the overlay.
  dialog.addEventListener('click', event => {
    if (event.target !== dialog || dialog.dataset.busy === 'true') return;
    const rect = dialog.getBoundingClientRect();
    if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
  });
});

document.addEventListener('click', event => {
  const closeButton = event.target.closest('[data-close-dialog]');
  if (closeButton) {
    const dialog = closeButton.closest('dialog');
    if (dialog.dataset.busy !== 'true') dialog.close();
    return;
  }
  const quick = event.target.closest('[data-quick]');
  if (quick) { openQuick(quick.dataset.quick); return; }
  const nav = event.target.closest('[data-view]');
  if (nav) { showView(nav.dataset.view); return; }
  const samplesButton = event.target.closest('[data-add-samples]');
  if (samplesButton) { openSamplesDialog(samplesButton); return; }
  const deleteButton = event.target.closest('[data-delete-user]');
  if (deleteButton) {
    $('delete-description').textContent = 'Bạn muốn xóa hồ sơ của “' + deleteButton.dataset.name + '”?';
    $('delete-form').action = '/api/users/delete/' + encodeURIComponent(deleteButton.dataset.deleteUser);
    $('delete-form').dataset.userId = deleteButton.dataset.deleteUser;
    $('delete-error').hidden = true;
    $('delete-dialog').showModal();
  }
});
showView(location.hash.slice(1), false);
window.addEventListener('hashchange', () => {
  const name = location.hash.slice(1);
  if (Object.hasOwn(viewTitles, name) || !name) showView(name, false);
});
document.querySelector('.skip-link').addEventListener('click', event => {
  event.preventDefault();
  $('main-content').focus();
  $('main-content').scrollIntoView();
});
$('image-dialog').addEventListener('close', () => {
  if (imageReturnFocus?.isConnected) imageReturnFocus.focus();
  else if ($('workspace-dialog').open) $('workspace-dialog').querySelector('input, button')?.focus();
  else $('main-content').focus({preventScroll: true});
  imageReturnFocus = null;
});

function safeImageUrl(value) {
  if (typeof value !== 'string' || !/^\/(?:captures|avatars)\/[^/\\]+$/.test(value)) return null;
  try {
    const url = new URL(value, location.origin);
    const filename = decodeURIComponent(url.pathname.split('/').pop());
    if (url.origin !== location.origin || url.search || url.hash || /[/\\\x00]/.test(filename) || ['.', '..'].includes(filename)) return null;
    if (!/^\/(?:captures|avatars)\/[^/]+$/.test(url.pathname)) return null;
    return url.pathname;
  } catch { return null; }
}

function imageEl(value, className, alt = 'Ảnh nhận diện') {
  const url = safeImageUrl(value);
  if (!url) return null;
  const image = node('img', undefined, className);
  image.src = url;
  image.alt = alt;
  image.loading = 'lazy';
  image.addEventListener('error', () => {
    const button = image.closest('.image-button');
    if (button) button.replaceWith(node('span', 'Ảnh không khả dụng', 'missing-image'));
    else { image.hidden = true; image.after(node('span', 'Ảnh không khả dụng', 'missing-image')); }
  }, {once: true});
  return image;
}

function imageButton(url, className, label) {
  const image = imageEl(url, className, label);
  if (!image) return null;
  const button = node('button', undefined, 'image-button');
  button.type = 'button';
  button.setAttribute('aria-label', 'Xem ' + label);
  button.append(image);
  button.addEventListener('click', () => {
    imageReturnFocus = button;
    $('image-dialog-title').textContent = label;
    $('image-dialog-body').replaceChildren(imageEl(url, '', label));
    $('image-dialog').showModal();
  });
  return button;
}

function statusPill(log) {
  if (log.status === 'UNLOCK') return node('span', 'Cho phép', 'pill');
  if (log.status === 'DENIED') return node('span', 'Từ chối', 'pill deny');
  return node('span', log.status || 'Chưa xác định', 'pill neutral');
}

function scoreText(value) { return value != null && Number.isFinite(Number(value)) ? Number(value).toFixed(2) : '—'; }
function dateText(log) { return log.created_at || '—'; }

function logRow(log, full) {
  const row = node('tr');
  row.dataset.logId = String(log.id ?? log.session_id ?? dateText(log));
  row.append(node('td', dateText(log)), node('td', log.user_name || 'Không xác định', 'person-name'));
  const decision = node('td');
  decision.append(statusPill(log));
  row.append(decision, node('td', scoreText(log.confidence), 'score'));
  if (full) {
    const photos = node('td');
    const thumbs = node('div', undefined, 'thumbs');
    for (const [key, label] of [['master_img', 'Ảnh Camera Master'], ['slave_img', 'Ảnh Camera Slave']]) {
      const image = imageButton(log[key], '', label);
      if (image) { image.dataset.imageKey = key; thumbs.append(image); }
    }
    if (!thumbs.childElementCount) thumbs.append(node('span', 'Chưa có ảnh', 'muted'));
    photos.append(thumbs);
    row.append(photos, node('td', log.message || '—', 'log-detail'));
  }
  return row;
}

function fillRows(id, logs, full, emptyText) {
  const body = $(id);
  const focused = body.contains(document.activeElement) ? document.activeElement : null;
  const focusLog = focused?.closest('tr')?.dataset.logId;
  const focusImage = focused?.dataset.imageKey;
  if (!logs.length) {
    const row = node('tr');
    const cell = node('td', emptyText || 'Chưa có lượt nhận diện nào.', 'empty');
    cell.colSpan = full ? 6 : 4;
    row.append(cell);
    body.replaceChildren(row);
  } else body.replaceChildren(...logs.map(log => logRow(log, full)));
  if (focused) {
    const replacement = [...body.querySelectorAll('.image-button')].find(button =>
      button.closest('tr').dataset.logId === focusLog && button.dataset.imageKey === focusImage);
    if (replacement) replacement.focus({preventScroll: true});
    else $('history-search').focus({preventScroll: true});
  }
}

function renderRecognitionHistory() {
  const query = normalize($('history-search').value);
  const status = $('history-status').value;
  const filtered = recognitionLogs.filter(log => normalize(log.user_name || 'Không xác định').includes(query) && (status === 'all' || log.status === status));
  fillRows('full-logs', filtered, true, query || status !== 'all' ? 'Không tìm thấy lượt nhận diện phù hợp. Thử tên hoặc kết quả khác.' : undefined);
  $('history-count').textContent = filtered.length + ' / ' + recognitionLogs.length + ' sự kiện';
}
$('history-search').addEventListener('input', renderRecognitionHistory);
$('history-status').addEventListener('change', renderRecognitionHistory);

function filterUsers() {
  const query = normalize($('user-search').value);
  const rows = [...document.querySelectorAll('.user-row')];
  let count = 0;
  rows.forEach(row => {
    row.hidden = !normalize(row.dataset.name + ' ' + row.dataset.code).includes(query);
    if (!row.hidden) count++;
  });
  $('user-result-count').textContent = count + ' / ' + rows.length + ' người dùng';
  $('user-empty').hidden = count > 0;
  $('user-empty').firstElementChild.textContent = query ? 'Không tìm thấy người dùng phù hợp.' : 'Chưa có người dùng. Thêm hồ sơ đầu tiên để bắt đầu.';
  document.querySelectorAll('[data-user-count]').forEach(element => { element.textContent = rows.length; });
}
$('user-search').addEventListener('input', filterUsers);
filterUsers();

function setCamera(name, value) {
  const frame = $(name + '-camera');
  const url = safeImageUrl(value);
  if (frame.dataset.image === (url || 'empty')) return;
  frame.dataset.image = url || 'empty';
  const button = imageButton(url, 'camera-img', 'Ảnh Camera ' + (name === 'master' ? 'Master' : 'Slave'));
  const label = $(name + '-label');
  label.textContent = button ? 'Có ảnh chụp' : 'Chưa có ảnh';
  label.className = button ? 'pill' : 'pill neutral';
  if (button) frame.replaceChildren(button);
  else {
    const placeholder = node('div', undefined, 'camera-placeholder');
    placeholder.append(icon('camera'), node('strong', 'Đang chờ ảnh nhận diện'), node('span', 'Ảnh sẽ xuất hiện sau khi thiết bị gửi dữ liệu'));
    frame.replaceChildren(placeholder);
  }
}

function latestResult(log) {
  const root = $('latest-result');
  if (!log) {
    root.className = 'result result-empty';
    root.replaceChildren(icon('scan'), node('strong', 'Chưa có lượt nhận diện'), node('p', 'Kết quả sẽ tự cập nhật khi có sự kiện mới.'));
    return;
  }
  root.className = 'result' + (log.status === 'DENIED' ? ' denied' : log.status === 'UNLOCK' ? '' : ' pending');
  const person = node('div', undefined, 'result-person');
  const photo = imageButton(log.master_img || log.slave_img, 'result-avatar', 'Ảnh nhận diện gần nhất');
  if (photo) person.append(photo);
  const detail = node('div');
  detail.append(node('strong', log.user_name || 'Không xác định'), node('small', 'Phiên nhận diện gần nhất'));
  person.append(detail);
  const meta = node('div', undefined, 'result-meta');
  for (const [label, value] of [['Điểm tương đồng', scoreText(log.confidence)], ['Thời gian', dateText(log)]]) {
    const row = node('div');
    row.append(node('span', label), node('strong', value));
    meta.append(row);
  }
  root.replaceChildren(statusPill(log), person, meta);
  if (log.message) root.append(node('p', log.message, 'result-message'));
}

async function requestJson(url, options = {}) {
  let response;
  try {
    response = await fetch(url, {
      credentials: 'same-origin', cache: 'no-store', signal: AbortSignal.timeout(options.method ? 30000 : 10000),
      ...options, headers: {Accept: 'application/json', ...options.headers}
    });
  } catch (error) {
    throw new Error(['TimeoutError', 'AbortError'].includes(error.name)
      ? 'Server phản hồi quá chậm. Vui lòng kiểm tra kết nối và thử lại.'
      : 'Không kết nối được Server. Vui lòng kiểm tra mạng.');
  }
  let data;
  try { data = await response.json(); } catch { throw new Error('Server trả về dữ liệu không hợp lệ.'); }
  if (response.status === 401) throw new Error('Phiên đăng nhập đã hết hạn. Vui lòng đăng nhập lại.');
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Không hoàn tất yêu cầu (HTTP ' + response.status + ').');
  return data;
}

function setSyncState(channel, error = null) {
  syncState[channel] = error || true;
  const errors = Object.values(syncState).filter(value => typeof value === 'string');
  const allReady = Object.values(syncState).every(value => value === true);
  const cls = errors.length ? 'pill warning' : allReady ? 'pill' : 'pill neutral';
  const text = errors.length ? 'Đồng bộ gián đoạn' : allReady ? 'Server đã kết nối' : 'Đang đồng bộ';
  for (const id of ['server-badge', 'server-device-status', 'server-health']) {
    $(id).textContent = text;
    $(id).className = cls;
  }
  $('sync-error').hidden = !errors.length;
  if (errors.length) $('sync-error').textContent = 'Dữ liệu có thể chưa được cập nhật. ' + [...new Set(errors)].join(' ') + ' Hệ thống sẽ tự thử lại.';
}

async function refresh() {
  if (pending.has('logs')) return;
  pending.add('logs');
  try {
    const data = await requestJson('/api/logs');
    if (!Array.isArray(data.logs)) throw new Error('Không đọc được nhật ký nhận diện.');
    $('stat-total').textContent = data.total ?? 0;
    $('stat-unlocked').textContent = data.unlocked ?? 0;
    $('stat-denied').textContent = data.denied ?? 0;
    if (JSON.stringify(recognitionLogs) !== JSON.stringify(data.logs) || !$('full-logs').dataset.loaded) {
      recognitionLogs = data.logs;
      renderRecognitionHistory();
      fillRows('recent-logs', recognitionLogs.slice(0, 5), false);
      latestResult(recognitionLogs[0]);
      setCamera('master', recognitionLogs[0]?.master_img);
      setCamera('slave', recognitionLogs[0]?.slave_img);
      $('full-logs').dataset.loaded = 'true';
    }
    $('last-sync').textContent = 'Đồng bộ lúc ' + new Date().toLocaleTimeString('vi-VN');
    setSyncState('logs');
  } catch (error) { setSyncState('logs', error.message); }
  finally { pending.delete('logs'); }
}

function updateCamera(name, camera) {
  const state = camera?.connection_status || 'UNKNOWN';
  const className = state === 'ONLINE' ? 'online' : state === 'OFFLINE' ? 'offline' : 'unknown';
  const text = state === 'ONLINE' ? 'Đang kết nối' : state === 'OFFLINE' ? 'Mất kết nối' : 'Chưa nhận Heartbeat';
  for (const id of [name + '-status', 'system-' + name]) {
    $(id).textContent = text;
    $(id).className = 'status-text ' + className;
  }
  const badge = $(name + '-device-status');
  badge.textContent = state === 'ONLINE' ? 'Online' : state === 'OFFLINE' ? 'Offline' : 'Chưa kết nối';
  badge.className = 'pill' + (state === 'ONLINE' ? '' : state === 'OFFLINE' ? ' deny' : ' neutral');
  $(name + '-last-seen').textContent = camera?.quality
    ? (camera.quality.ok ? 'Ảnh đạt chất lượng · nét: ' + camera.quality.sharpness
       : camera.quality.reasons.join('; '))
    : camera?.last_seen ? 'Gửi ảnh: ' + camera.last_seen : 'Chưa có dữ liệu gửi ảnh';
}

async function refreshCameraStatus() {
  if (pending.has('cameras')) return;
  pending.add('cameras');
  try {
    const data = await requestJson('/api/camera-status');
    if (!data.master || !data.slave) throw new Error('Không đọc được trạng thái thiết bị.');
    updateCamera('master', data.master);
    updateCamera('slave', data.slave);
    const count = ['master', 'slave'].filter(name => data[name].connection_status === 'ONLINE').length;
    $('device-count').textContent = count + ' / 2 online';
    $('device-count').className = 'pill' + (count === 2 ? '' : ' warning');
    setSyncState('cameras');
  } catch (error) {
    for (const name of ['master', 'slave']) {
      for (const id of [name + '-status', 'system-' + name]) {
        $(id).textContent = 'Không lấy được trạng thái';
        $(id).className = 'status-text unknown';
      }
      $(name + '-device-status').textContent = 'Chưa xác định';
      $(name + '-device-status').className = 'pill neutral';
      $(name + '-last-seen').textContent = 'Chưa đồng bộ được trạng thái';
    }
    $('device-count').textContent = '— / 2';
    $('device-count').className = 'pill neutral';
    setSyncState('cameras', error.message);
  } finally { pending.delete('cameras'); }
}

function renderConnectionHistory() {
  const selected = $('connection-filter').value;
  const events = connectionEvents.filter(item => selected === 'all' || item.camera_name === selected);
  $('connection-count').textContent = events.length + ' sự kiện';
  if (!events.length) {
    const row = node('tr');
    const cell = node('td', 'Chưa có sự kiện kết nối phù hợp.', 'empty');
    cell.colSpan = 4;
    row.append(cell);
    $('connection-rows').replaceChildren(row);
    return;
  }
  $('connection-rows').replaceChildren(...events.map(item => {
    const row = node('tr');
    const status = node('td');
    const state = String(item.event_type || 'UNKNOWN').toUpperCase();
    status.append(node('span', state, 'pill' + (state === 'ONLINE' ? '' : state === 'OFFLINE' ? ' deny' : ' neutral')));
    row.append(node('td', item.created_at || '—'), node('td', item.camera_name === 'master' ? 'ESP32-S3 Master' : item.camera_name === 'slave' ? 'ESP32-CAM Slave' : item.camera_name), status, node('td', item.detail || '—', 'log-detail'));
    return row;
  }));
}

async function refreshConnectionHistory() {
  if (pending.has('connections')) return;
  pending.add('connections');
  try {
    const data = await requestJson('/api/connection-logs');
    if (!Array.isArray(data.logs)) throw new Error('Không đọc được nhật ký kết nối.');
    if (JSON.stringify(connectionEvents) !== JSON.stringify(data.logs) || !$('connection-rows').dataset.loaded) {
      connectionEvents = data.logs;
      renderConnectionHistory();
      $('connection-rows').dataset.loaded = 'true';
    }
    $('connection-error').hidden = true;
    $('connection-last-refresh').textContent = 'Đồng bộ lúc ' + new Date().toLocaleTimeString('vi-VN');
    setSyncState('connections');
  } catch (error) {
    $('connection-error').hidden = false;
    $('connection-error').textContent = 'Không tải được nhật ký kết nối. ' + error.message;
    setSyncState('connections', error.message);
  } finally { pending.delete('connections'); }
}
$('connection-filter').addEventListener('change', renderConnectionHistory);
$('connection-refresh').addEventListener('click', refreshConnectionHistory);
$('history-refresh').addEventListener('click', refresh);

function toast(text) {
  clearTimeout(toastTimer);
  const dialogs = [...document.querySelectorAll('dialog[open]')];
  (dialogs.at(-1) || document.body).append($('toast'));
  $('toast').textContent = text;
  $('toast').hidden = false;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
}

function addUserRow(user) {
  const row = node('tr', undefined, 'user-row');
  row.dataset.userId = user.id;
  row.dataset.name = user.name;
  row.dataset.code = user.user_code;
  const personCell = node('td');
  const person = node('div', undefined, 'person-cell');
  const photo = imageEl(user.avatar_path, 'avatar', 'Ảnh của ' + user.name);
  if (photo) person.append(photo);
  const detail = node('div');
  detail.append(node('strong', user.name), node('small', 'Hồ sơ khuôn mặt'));
  person.append(detail);
  personCell.append(person);
  const code = node('td');
  code.append(node('span', user.user_code, 'code-label'));
  const role = node('td');
  role.append(node('span', user.role, 'role-pill'));
  const action = node('td', undefined, 'align-right');
  const button = node('button', undefined, 'btn btn-danger-soft');
  button.type = 'button';
  button.dataset.deleteUser = user.id;
  button.dataset.name = user.name;
  button.setAttribute('aria-label', 'Xóa người dùng ' + user.name);
  button.append(icon('trash'), document.createTextNode('Xóa'));
  const samples = node('button', 'Bổ sung ảnh', 'btn btn-secondary');
  samples.type = 'button';
  samples.dataset.addSamples = user.id;
  samples.dataset.name = user.name;
  action.append(samples, document.createTextNode(' '), button);
  row.append(personCell, code, role, action);
  $('user-rows').prepend(row);
  filterUsers();
}

function setFormBusy(dialogId, buttonId, busy) {
  const dialog = $(dialogId);
  dialog.dataset.busy = String(busy);
  $(buttonId).disabled = busy;
  dialog.querySelectorAll('[data-close-dialog]').forEach(button => { button.disabled = busy; });
  dialog.setAttribute('aria-busy', String(busy));
}

$('register-form').addEventListener('submit', async event => {
  event.preventDefault();
  if ($('register-dialog').dataset.busy === 'true') return;
  const form = event.currentTarget;
  const data = new FormData(form);
  data.set('name', data.get('name').trim());
  data.set('user_code', data.get('user_code').trim());
  if (!data.get('name') || !data.get('user_code')) {
    $('register-alert').hidden = false;
    $('register-alert').className = 'notice notice-warning';
    $('register-alert').textContent = 'Vui lòng nhập họ tên và mã người dùng.';
    return;
  }
  setFormBusy('register-dialog', 'register-submit', true);
  $('register-alert').hidden = false;
  $('register-alert').className = 'notice notice-info';
  $('register-alert').textContent = 'Đang kiểm tra khuôn mặt và lưu hồ sơ…';
  try {
    const result = await requestJson(form.action, {method: 'POST', body: data, signal: AbortSignal.timeout(120000)});
    if (!result.user) throw new Error('Không nhận được hồ sơ từ Server.');
    addUserRow(result.user);
    form.reset();
    $('register-alert').hidden = true;
    $('register-dialog').close();
    toast('Đã đăng ký người dùng “' + result.user.name + '”.');
  } catch (error) {
    $('register-alert').hidden = false;
    $('register-alert').className = 'notice notice-warning';
    $('register-alert').textContent = error.message;
  } finally { setFormBusy('register-dialog', 'register-submit', false); }
});

$('delete-form').addEventListener('submit', async event => {
  event.preventDefault();
  if ($('delete-dialog').dataset.busy === 'true') return;
  const form = event.currentTarget;
  setFormBusy('delete-dialog', 'delete-submit', true);
  $('delete-error').hidden = true;
  try {
    await requestJson(form.action, {method: 'POST', body: new FormData(form)});
    const row = [...document.querySelectorAll('.user-row')].find(item => item.dataset.userId === form.dataset.userId);
    // Return focus to a surviving control after removing the initiating row.
    $('delete-dialog').close();
    row?.remove();
    filterUsers();
    $('user-search').focus();
    toast('Đã xóa người dùng khỏi hệ thống.');
  } catch (error) {
    $('delete-error').hidden = false;
    $('delete-error').textContent = error.message;
  } finally { setFormBusy('delete-dialog', 'delete-submit', false); }
});

// Retain feedback for older clients that submit the existing redirect-based forms.
const params = new URLSearchParams(location.search);
if (params.has('register_error')) {
  openQuick('register');
  $('register-alert').hidden = false;
  $('register-alert').className = 'notice notice-warning';
  $('register-alert').textContent = params.get('register_error') === 'no_face' ? 'Không phát hiện khuôn mặt trong ảnh. Vui lòng chọn ảnh khác.' : 'Không lưu được hồ sơ. Vui lòng kiểm tra mã người dùng.';
} else if (params.has('register_success')) toast('Đăng ký người dùng thành công.');
if (params.has('register_error') || params.has('register_success')) {
  params.delete('register_error'); params.delete('register_success');
  history.replaceState(null, '', location.pathname + (params.size ? '?' + params.toString() : '') + location.hash);
}

function refreshAll() {
  if (document.hidden) return;
  refresh(); refreshCameraStatus(); refreshConnectionHistory();
}
refreshAll();
setInterval(refreshAll, 3000);
document.addEventListener('visibilitychange', () => { if (!document.hidden) refreshAll(); });

async function openSamplesDialog(button) {
  const dialog = $('samples-dialog');
  const forms = dialog?.querySelectorAll('form[data-sample-mode]');
  if (!dialog || forms.length !== 2) {
    toast('Không tải được form bổ sung ảnh. Vui lòng tải lại trang.');
    return;
  }
  if (dialog.dataset.busy === 'true') return;
  const userId = button.dataset.addSamples;
  const endpoint = '/api/users/' + encodeURIComponent(userId) + '/samples';
  const version = ++samplesSummaryVersion;
  dialog.dataset.userId = userId;
  dialog.dataset.description = 'Hồ sơ: ' + button.dataset.name;
  for (const form of forms) {
    form.reset();
    form.action = endpoint;
    $('samples-alert-' + form.dataset.sampleMode).hidden = true;
    $('samples-count-' + form.dataset.sampleMode).textContent = 'Đang tải số ảnh đã lưu…';
  }
  $('samples-description').textContent = dialog.dataset.description;
  $('samples-alert').hidden = true;
  if (!dialog.open) dialog.showModal();
  $('samples-form-unmasked').elements.photos.focus();
  // Show the picker immediately; loading the sample count must never block it.
  try {
    const summary = await requestJson(endpoint);
    if (!dialog.open || dialog.dataset.userId !== userId || version !== samplesSummaryVersion) return;
    renderSampleCounts(summary);
  } catch (error) {
    if (!dialog.open || dialog.dataset.userId !== userId || version !== samplesSummaryVersion) return;
    $('samples-alert').hidden = false;
    $('samples-alert').textContent = error.message;
  }
}

function renderSampleCounts(summary) {
  const masked = Number(summary.masked);
  const legacy = Number(summary.legacy || 0);
  const unmasked = Number(summary.unmasked ?? (summary.sample_count - masked - legacy));
  $('samples-count-unmasked').textContent = unmasked + ' ảnh không khẩu trang đã lưu';
  $('samples-count-masked').textContent = masked + ' ảnh có khẩu trang đã lưu';
  $('samples-description').textContent = $('samples-dialog').dataset.description
    + ' · Tổng ' + summary.sample_count + '/20 mẫu'
    + (legacy ? ' · ' + legacy + ' mẫu cũ chưa phân loại' : '');
}

function setSamplesBusy(busy) {
  const dialog = $('samples-dialog');
  dialog.dataset.busy = String(busy);
  dialog.setAttribute('aria-busy', String(busy));
  dialog.querySelectorAll('input, button').forEach(control => { control.disabled = busy; });
}

document.querySelectorAll('#samples-dialog form[data-sample-mode]').forEach(form => {
form.addEventListener('submit', async event => {
  event.preventDefault();
  if ($('samples-dialog').dataset.busy === 'true') return;
  // FormData must be scoped to this group and captured before disabling inputs.
  const data = new FormData(form);
  const mode = form.dataset.sampleMode;
  data.set('mode', mode);
  const alert = $('samples-alert-' + mode);
  setSamplesBusy(true);
  alert.hidden = false;
  alert.className = 'notice notice-info';
  alert.textContent = 'Đang kiểm tra và lưu ảnh ' + (mode === 'masked' ? 'có khẩu trang' : 'không khẩu trang') + '…';
  try {
    const result = await requestJson(form.action, {
      method: 'POST', body: data, signal: AbortSignal.timeout(120000)
    });
    ++samplesSummaryVersion;
    renderSampleCounts(result);
    $('samples-alert').hidden = true;
    // Only clear the uploaded group's selection. The other group's files stay put.
    form.reset();
    alert.textContent = 'Đã lưu ' + result.added + ' ảnh ' + (mode === 'masked' ? 'có khẩu trang.' : 'không khẩu trang.');
  } catch (error) {
    alert.className = 'notice notice-warning';
    alert.textContent = error.message;
  } finally {
    setSamplesBusy(false);
  }
});
});

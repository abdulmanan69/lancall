'use strict';

/*
 * Administration console. One page, two consoles:
 *   /platform                  every customer (platform administrator)
 *   /admin, /o/<name>/admin    one organisation's phones, calls and settings
 */

const $ = (id) => document.getElementById(id);
const PLATFORM_MODE = location.pathname === '/platform';
const ORG = PLATFORM_MODE ? null : (location.pathname.match(/^\/o\/([a-z0-9-]+)\/admin$/) || [])[1] || 'default';
const REFRESH_MS = 4000;

let scope = null; // 'platform' | 'org'
let data = null; // last overview from the server
let timer = null;

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === false || value == null) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? '' : value);
  }
  node.append(...children);
  return node;
}

function toast(text) {
  const node = el('div', { class: 'toast', text });
  $('toasts').append(node);
  setTimeout(() => node.remove(), 4500);
}

async function api(method, path, body) {
  const response = await fetch(path, {
    method,
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-Requested-With': 'lancall' },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch {
    /* empty body */
  }
  if (response.status === 401 && path !== '/api/admin/login') {
    showLogin();
    throw new Error('Signed out');
  }
  if (!response.ok) throw new Error(payload.error || `The server answered ${response.status}.`);
  return payload;
}

const dateTime = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
const when = (ms) => (ms ? dateTime.format(new Date(ms)) : 'Never');
const number = new Intl.NumberFormat();

function duration(seconds) {
  const s = Math.max(0, Math.round(seconds));
  const pad = (n) => String(n).padStart(2, '0');
  return s >= 3600 ? `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}` : `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

/* Cells carry their column name so the table can stack into cards on a narrow screen. */
function row(labels, cells) {
  return el('tr', {}, ...cells.map((cell, i) => {
    const node = el(i === 0 ? 'th' : 'td', { 'data-label': labels[i] || null, scope: i === 0 ? 'row' : null });
    node.append(...(Array.isArray(cell) ? cell : [cell]));
    return node;
  }));
}

/* An empty table shows its "nothing yet" line instead of bare column headings. */
function fill(bodyId, emptyId, rows) {
  $(bodyId).replaceChildren(...rows);
  $(bodyId).closest('.table-wrap').hidden = rows.length === 0;
  $(emptyId).hidden = rows.length > 0;
}

function tiles(target, items) {
  target.replaceChildren(...items.map(([label, value, note]) =>
    el('div', { class: 'tile' }, el('dt', { text: label }), el('dd', { class: 'num' }, String(value), note ? el('span', { class: 'muted small', text: ' ' + note }) : ''))));
}

function confirmAction(title, text, okLabel) {
  const dialog = $('confirm-dialog');
  $('confirm-title').textContent = title;
  $('confirm-text').textContent = text;
  $('confirm-ok').textContent = okLabel;
  dialog.returnValue = 'cancel';
  dialog.showModal();
  return new Promise((resolve) => dialog.addEventListener('close', () => resolve(dialog.returnValue === 'ok'), { once: true }));
}

async function act(work, done) {
  try {
    await work();
    if (done) toast(done);
    await refresh();
  } catch (error) {
    if (error.message !== 'Signed out') toast(error.message);
  }
}

// ------------------------------------------------------------- validation
// Fields are checked here and the problem is written into the page, so the
// browser's own pop-up bubbles never appear (the forms carry "novalidate").

function fieldProblem(input) {
  const label = (input.labels && input.labels[0] && input.labels[0].textContent.trim()) || 'This field';
  const v = input.validity;
  if (v.valueMissing) return `${label} is needed.`;
  if (v.tooShort) return `${label} needs at least ${input.minLength} characters.`;
  if (v.patternMismatch) return `${label}: ${input.dataset.hint || 'that is not a valid value.'}`;
  return `${label}: check this value.`;
}

function validate(form, errorNode) {
  const bad = [...form.elements].find((input) => input.willValidate && !input.validity.valid);
  if (!bad) return true;
  bad.setAttribute('aria-invalid', 'true');
  errorNode.textContent = fieldProblem(bad);
  bad.focus();
  return false;
}

document.addEventListener('input', (event) => event.target.removeAttribute && event.target.removeAttribute('aria-invalid'));

// ------------------------------------------------------------------ sign-in

function showLogin() {
  clearInterval(timer);
  $('view-console').hidden = true;
  $('view-login').hidden = false;
  $('login-hint').textContent = PLATFORM_MODE ? 'Sign in to manage all customers.' : 'Sign in to manage this phone system.';
  $('login-password').focus();
}

$('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('login-error').textContent = '';
  if (!validate(event.target, $('login-error'))) return;
  $('login-error').textContent = '';
  try {
    const result = await api('POST', '/api/admin/login', { password: $('login-password').value, org: ORG });
    if (PLATFORM_MODE && result.scope !== 'platform') throw new Error('That password is not for the platform console.');
    $('login-password').value = '';
    start();
  } catch (error) {
    $('login-error').textContent = error.message;
  }
});

$('sign-out').addEventListener('click', async () => {
  await api('POST', '/api/admin/logout').catch(() => {});
  showLogin();
});

async function start() {
  let session;
  try {
    session = await api('GET', '/api/admin/session' + (ORG ? `?org=${encodeURIComponent(ORG)}` : ''));
  } catch {
    session = { signed_in: false };
  }
  if (!session.signed_in || (PLATFORM_MODE && session.scope !== 'platform')) {
    showLogin();
    return;
  }
  scope = session.scope;
  $('view-login').hidden = true;
  $('view-console').hidden = false;
  $('org-console').hidden = PLATFORM_MODE;
  $('platform-console').hidden = !PLATFORM_MODE;
  $('link-platform').hidden = PLATFORM_MODE || scope !== 'platform';
  await refresh();
  loadHistory();
  clearInterval(timer);
  timer = setInterval(() => {
    if (!document.hidden && !document.querySelector('dialog[open]')) refresh();
  }, REFRESH_MS);
}

async function refresh() {
  try {
    data = await api('GET', PLATFORM_MODE ? '/api/platform' : `/api/orgs/${ORG}`);
    $('console-error').hidden = true;
  } catch (error) {
    if (error.message === 'Signed out') return;
    $('console-error').hidden = false;
    $('console-error').textContent = error.message;
    return;
  }
  if (PLATFORM_MODE) renderPlatform();
  else renderOrg();
}

// ------------------------------------------------------------ organisation

const STATUS = { online: 'Available', busy: 'On a call', dnd: 'Do not disturb', offline: 'Offline', disabled: 'Switched off' };
const RESULT = {
  hangup: 'Completed', declined: 'Declined', no_answer: 'No answer', cancelled: 'Caller gave up', connection_lost: 'Connection lost',
  transferred: 'Transferred', admin: 'Ended by administrator', server_shutdown: 'Server restarted',
};

let historyCount = -1;

function renderOrg() {
  const { org, phones, calls, stats, usage } = data;
  const month = usage[new Date().toISOString().slice(0, 7)] || usage[Object.keys(usage).sort().pop()] || { calls: 0, seconds: 0 };
  document.title = `${org.name} · Administration`;
  $('console-title').textContent = org.name;
  $('console-sub').textContent = `Administration · version ${data.version}`;
  $('link-app').hidden = false;
  $('link-app').href = org.path;
  $('org-suspended').hidden = !org.suspended;
  tiles($('org-tiles'), [
    ['Phones', number.format(stats.phones), org.seats ? `of ${number.format(org.seats)} seats` : ''],
    ['Online now', number.format(stats.online)],
    ['Calls now', number.format(stats.calls)],
    ['Calls this month', number.format(month.calls)],
    ['Minutes this month', number.format(Math.round(month.seconds / 60))],
  ]);

  const url = location.origin + org.path;
  $('join-url').textContent = url;
  $('join-url').href = org.path;
  $('join-code').textContent = org.pin;
  $('join-code-wrap').hidden = $('join-code-text').hidden = !org.pin;

  const liveLabels = ['From', 'To', 'State', 'Started', 'Supervisor', ''];
  fill('live-rows', 'live-empty', calls.map((call) => row(liveLabels, [
    `${call.a.name} (${call.a.ext})`,
    `${call.b.name} (${call.b.ext})`,
    call.state === 'active' ? 'Talking' : 'Ringing',
    when(call.answered || call.started),
    call.supervisor ? `${call.supervisor} · ${call.mode}` : '—',
    el('button', { type: 'button', class: 'btn small', onclick: () => endCall(call) }, 'End call'),
  ])));

  const phoneLabels = ['Name', 'Extension', 'Status', 'Role', 'Last seen', ''];
  fill('phone-rows', 'phones-empty', phones.map((phone) => row(phoneLabels, [
    phone.name,
    phone.ext,
    [el('span', { class: 'dot', 'data-state': phone.status === 'disabled' ? 'error' : phone.status }), ' ' + (STATUS[phone.status] || phone.status)],
    phone.role === 'supervisor' ? 'Supervisor' : 'User',
    phone.status === 'offline' || phone.status === 'disabled' ? when(phone.last_seen) : `Now · ${phone.ip || 'connected'}`,
    [
      el('button', { type: 'button', class: 'btn small', onclick: () => editPhone(phone) }, 'Edit'),
      ' ',
      el('button', { type: 'button', class: 'btn small', onclick: () => removePhone(phone) }, 'Remove'),
    ],
  ])));

  const months = Object.keys(usage).sort().reverse();
  fill('usage-rows', 'usage-empty', months.map((key) => row(['Month', 'Calls', 'Answered', 'Minutes'], [
    key, number.format(usage[key].calls), number.format(usage[key].answered), number.format(Math.round(usage[key].seconds / 60)),
  ])));

  // Leave the form alone while someone is typing in it.
  if (!$('settings-form').contains(document.activeElement)) {
    $('set-org-name').value = org.name;
    $('set-org-pin').value = org.pin;
    $('set-org-notice').checked = org.monitor_notice;
  }
  const ownPlatform = scope === 'platform' && org.slug === 'default';
  $('pw-current-row').hidden = !ownPlatform;
  $('pw-current').required = ownPlatform;
  $('pw-new-label').textContent = scope === 'platform' && !ownPlatform ? 'Set this customer’s administrator password' : 'New administrator password';
  $('history-csv').href = `/api/orgs/${org.slug}/history.csv`;

  const total = (usage && Object.values(usage).reduce((sum, m) => sum + m.calls, 0)) || 0;
  if (total !== historyCount) {
    historyCount = total;
    loadHistory();
  }
}

async function loadHistory() {
  if (PLATFORM_MODE) return;
  let calls = [];
  try {
    calls = (await api('GET', `/api/orgs/${ORG}/history?limit=300`)).calls;
  } catch {
    return;
  }
  const labels = ['When', 'From', 'To', 'Result', 'Length', 'Supervision'];
  fill('history-rows', 'history-empty', calls.map((call) => row(labels, [
    when(call.started * 1000),
    `${call.from_name || ''} (${call.from || '?'})`,
    `${call.to_name || ''} (${call.to || '?'})`,
    call.answered && call.reason === 'hangup' ? 'Completed' : RESULT[call.reason] || call.reason,
    call.answered ? duration(call.duration) : '—',
    (call.supervised || []).join(', ') || '—',
  ])));
}

let editing = null;

function editPhone(phone) {
  editing = phone;
  $('phone-name').value = phone.name;
  $('phone-ext').value = phone.ext;
  $('phone-role').value = phone.role;
  $('phone-disabled').checked = phone.disabled;
  $('phone-error').textContent = '';
  $('phone-dialog').showModal();
}

$('phone-form').addEventListener('submit', async (event) => {
  event.preventDefault(); // stay open until the server agrees: the extension may be taken
  $('phone-error').textContent = '';
  if (!validate(event.target, $('phone-error'))) return;
  const changes = {};
  if ($('phone-name').value.trim() !== editing.name) changes.name = $('phone-name').value.trim();
  if ($('phone-ext').value.trim() !== editing.ext) changes.ext = $('phone-ext').value.trim();
  if ($('phone-role').value !== editing.role) changes.role = $('phone-role').value;
  if ($('phone-disabled').checked !== editing.disabled) changes.disabled = $('phone-disabled').checked;
  try {
    if (Object.keys(changes).length) await api('PATCH', `/api/orgs/${ORG}/phones/${editing.ext}`, changes);
    $('phone-dialog').close();
    refresh();
  } catch (error) {
    $('phone-error').textContent = error.message;
  }
});

async function removePhone(phone) {
  const ok = await confirmAction(`Remove ${phone.name}?`, `Extension ${phone.ext} is freed and this phone is signed out. It can join again as a new phone.`, 'Remove phone');
  if (ok) act(() => api('DELETE', `/api/orgs/${ORG}/phones/${phone.ext}`), 'Phone removed');
}

async function endCall(call) {
  const ok = await confirmAction('End this call?', `${call.a.name} and ${call.b.name} are cut off.`, 'End call');
  if (ok) act(() => api('POST', `/api/orgs/${ORG}/calls/${call.call_id}/hangup`), 'Call ended');
}

$('settings-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('settings-msg').textContent = '';
  if (!validate(event.target, $('settings-msg'))) return;
  $('settings-msg').textContent = '';
  try {
    await api('PATCH', `/api/orgs/${ORG}`, { name: $('set-org-name').value.trim(), pin: $('set-org-pin').value.trim(), monitor_notice: $('set-org-notice').checked });
    document.activeElement.blur();
    toast('Settings saved');
    refresh();
  } catch (error) {
    $('settings-msg').textContent = error.message;
  }
});

$('password-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('password-msg').textContent = '';
  if (!validate(event.target, $('password-msg'))) return;
  $('password-msg').textContent = '';
  try {
    if (!$('pw-current-row').hidden) await api('POST', '/api/platform/password', { current: $('pw-current').value, new: $('pw-new').value });
    else await api('PATCH', `/api/orgs/${ORG}`, { admin_password: $('pw-new').value });
    $('pw-current').value = $('pw-new').value = '';
    toast('Password changed');
  } catch (error) {
    $('password-msg').textContent = error.message;
  }
});

// ---------------------------------------------------------------- platform

function renderPlatform() {
  document.title = 'Customers · Administration';
  $('console-title').textContent = 'Customers';
  $('console-sub').textContent = `Platform administration · version ${data.version}`;
  const orgs = data.orgs;
  const sum = (pick) => orgs.reduce((total, org) => total + pick(org), 0);
  tiles($('platform-tiles'), [
    ['Customers', number.format(orgs.length)],
    ['Phones', number.format(sum((o) => o.phones))],
    ['Online now', number.format(sum((o) => o.online))],
    ['Calls now', number.format(sum((o) => o.calls))],
    ['Minutes this month', number.format(Math.round(sum((o) => o.usage.seconds) / 60))],
  ]);
  $('relay-warning').hidden = data.relay;
  $('platform-pw-managed').hidden = !data.password_managed;
  $('platform-pw-form').hidden = data.password_managed;

  const labels = ['Customer', 'Address', 'Phones', 'Online', 'Calls this month', 'Minutes this month', 'Status', ''];
  $('org-rows').replaceChildren(...orgs.map((org) => {
    const builtIn = org.slug === 'default';
    const actions = [el('a', { class: 'btn small', href: builtIn ? '/admin' : `/o/${org.slug}/admin` }, 'Manage'), ' ',
      el('button', { type: 'button', class: 'btn small', onclick: () => editSeats(org) }, 'Seats')];
    if (!builtIn) {
      actions.push(' ', el('button', { type: 'button', class: 'btn small', onclick: () => toggleSuspend(org) }, org.suspended ? 'Resume' : 'Suspend'),
        ' ', el('button', { type: 'button', class: 'btn small', onclick: () => deleteOrg(org) }, 'Delete'));
    }
    return row(labels, [
      org.name,
      el('a', { href: org.path, text: org.path }),
      org.seats ? `${number.format(org.phones)} of ${number.format(org.seats)}` : number.format(org.phones),
      number.format(org.online),
      number.format(org.usage.calls),
      number.format(Math.round(org.usage.seconds / 60)),
      org.suspended ? 'Suspended' : 'Active',
      actions,
    ]);
  }));
}

$('org-add').addEventListener('click', () => {
  $('org-form').reset();
  $('org-error').textContent = '';
  $('org-slug-preview').textContent = `${location.origin}/o/…/`;
  $('org-dialog').showModal();
});

$('org-name').addEventListener('input', () => {
  if ($('org-slug').dataset.touched) return;
  $('org-slug').value = $('org-name').value.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-+|-+$/g, '').slice(0, 32);
  $('org-slug-preview').textContent = `${location.origin}/o/${$('org-slug').value || '…'}/`;
});
$('org-slug').addEventListener('input', () => {
  $('org-slug').dataset.touched = '1';
  $('org-slug-preview').textContent = `${location.origin}/o/${$('org-slug').value || '…'}/`;
});

$('org-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('org-error').textContent = '';
  if (!validate(event.target, $('org-error'))) return;
  try {
    await api('POST', '/api/platform/orgs', {
      name: $('org-name').value.trim(), slug: $('org-slug').value.trim(), seats: Number($('org-seats').value) || 0,
      pin: $('org-pin').value.trim(), admin_password: $('org-password').value,
    });
    delete $('org-slug').dataset.touched;
    $('org-dialog').close();
    toast('Customer added');
    refresh();
  } catch (error) {
    $('org-error').textContent = error.message;
  }
});

let seatsFor = null;

function editSeats(org) {
  seatsFor = org;
  $('seats-value').value = org.seats || '';
  $('seats-dialog').showModal();
}

$('seats-form').addEventListener('submit', (event) => {
  event.preventDefault();
  $('seats-error').textContent = '';
  if (!validate(event.target, $('seats-error'))) return;
  $('seats-dialog').close();
  act(() => api('PATCH', `/api/platform/orgs/${seatsFor.slug}`, { seats: Number($('seats-value').value) || 0 }), 'Seats updated');
});

async function toggleSuspend(org) {
  if (!org.suspended) {
    const ok = await confirmAction(`Suspend ${org.name}?`, 'Their calls end now and their phones cannot connect until you resume them. Nothing is deleted.', 'Suspend');
    if (!ok) return;
  }
  act(() => api('PATCH', `/api/platform/orgs/${org.slug}`, { suspended: !org.suspended }), org.suspended ? 'Customer resumed' : 'Customer suspended');
}

async function deleteOrg(org) {
  const ok = await confirmAction(`Delete ${org.name}?`, `All ${org.phones} of their phones stop working and their address is freed. Their data is moved to the server's "deleted" folder.`, 'Delete customer');
  if (ok) act(() => api('DELETE', `/api/platform/orgs/${org.slug}`), 'Customer deleted');
}

$('platform-pw-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  $('platform-pw-msg').textContent = '';
  if (!validate(event.target, $('platform-pw-msg'))) return;
  $('platform-pw-msg').textContent = '';
  try {
    await api('POST', '/api/platform/password', { current: $('ppw-current').value, new: $('ppw-new').value });
    event.target.reset();
    toast('Password changed');
  } catch (error) {
    $('platform-pw-msg').textContent = error.message;
  }
});

// ------------------------------------------------------------------ wiring

for (const button of document.querySelectorAll('[data-close]')) {
  button.addEventListener('click', () => button.closest('dialog').close());
}

// Light dismiss where <dialog closedby> is missing: press and release outside the box.
if (!('closedBy' in HTMLDialogElement.prototype)) {
  for (const dialog of document.querySelectorAll('dialog[closedby="any"]')) {
    const outside = (event) => {
      const r = dialog.getBoundingClientRect();
      return event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom;
    };
    let pressedOutside = false;
    dialog.addEventListener('pointerdown', (event) => {
      pressedOutside = outside(event);
    });
    dialog.addEventListener('pointerup', (event) => {
      if (pressedOutside && outside(event)) dialog.close();
    });
  }
}

document.addEventListener('visibilitychange', () => !document.hidden && data && refresh());

start();

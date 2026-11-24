'use strict';

/*
 * LAN Call client.
 *
 * The server decides who is ringing whom; this page owns the microphone, the
 * audio connection (WebRTC, phone to phone) and everything the user sees.
 */

const PROTOCOL_VERSION = 1;
const HISTORY_LIMIT = 100;
const MEDIA_SETUP_TIMEOUT = 20000; // answered but no audio path after this long: give up
const MEDIA_RECOVER_TIMEOUT = 30000; // audio path lost mid-call for this long: give up
const SVG_NS = 'http://www.w3.org/2000/svg';

const $ = (id) => document.getElementById(id);

// One server can host several organisations, each under /o/<name>/. They share
// a browser origin, so each keeps its saved identity under its own key.
const ORG = (location.pathname.match(/^\/o\/([a-z0-9-]+)\//) || [])[1] || '';
const BASE = ORG ? `/o/${ORG}` : '';
const STORE = 'lancall.' + (ORG ? ORG + '.' : '');

// ---------------------------------------------------------------- storage

const storage = {
  get(key, fallback) {
    try {
      const raw = localStorage.getItem(STORE + key);
      return raw === null ? fallback : JSON.parse(raw);
    } catch {
      return fallback;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(STORE + key, JSON.stringify(value));
    } catch {
      /* private mode or full storage: keep working from memory */
    }
  },
  remove(key) {
    try {
      localStorage.removeItem(STORE + key);
    } catch {
      /* ignore */
    }
  },
};

function randomToken(bytes) {
  const buf = new Uint8Array(bytes);
  crypto.getRandomValues(buf);
  return btoa(String.fromCharCode(...buf)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
}

// ------------------------------------------------------------------ state

const state = {
  identity: storage.get('identity', null), // { deviceId, secret }: proves this browser owns its extension
  me: storage.get('me', null), // { ext, name, dnd } as last confirmed by the server
  server: null,
  iceServers: [],
  directory: [],
  conn: 'connecting', // connecting | online | offline | replaced | blocked | refused
  refusedText: '',
  role: 'user', // user | supervisor
  liveCalls: [], // supervisors only: calls in progress
  sup: null, // supervisors only: the call being monitored
  call: null,
  history: storage.get('history', []),
  unseenMissed: storage.get('unseenMissed', 0),
  settings: Object.assign({ micId: '', speakerId: '', ringVolume: 0.6, keepAwake: false }, storage.get('settings', {})),
  tab: 'directory',
};

let pendingName = '';
let pendingPin = '';

// ------------------------------------------------------------- dom helpers

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

function icon(name) {
  const svg = document.createElementNS(SVG_NS, 'svg');
  svg.setAttribute('aria-hidden', 'true');
  const use = document.createElementNS(SVG_NS, 'use');
  use.setAttribute('href', '#' + name);
  svg.append(use);
  return svg;
}

function initials(name) {
  const parts = String(name || '?').trim().split(/\s+/).filter(Boolean);
  const letters = parts.length > 1 ? parts[0][0] + parts[parts.length - 1][0] : (parts[0] || '?').slice(0, 2);
  return letters.toUpperCase();
}

function toast(text, ms = 4500) {
  const node = el('div', { class: 'toast', text });
  $('toasts').append(node);
  setTimeout(() => node.remove(), ms);
}

function vibrate(pattern) {
  // Browsers refuse (and log an error) until the user has touched the page.
  const touched = !navigator.userActivation || navigator.userActivation.hasBeenActive;
  if (navigator.vibrate && touched) navigator.vibrate(pattern);
}

function showView(name) {
  for (const view of ['onboarding', 'main', 'call', 'sup']) $('view-' + view).hidden = view !== name;
}

// ------------------------------------------------------------------ tones

const TONES = {
  ring: { period: 3, volume: () => state.settings.ringVolume * 0.35, notes: [[0, 0.32, [784]], [0.4, 0.32, [659]], [0.8, 0.32, [784]], [1.2, 0.32, [659]]] },
  ringback: { period: 6, volume: () => 0.06, notes: [[0, 2, [440, 480]]] },
  busy: { volume: () => 0.07, notes: [[0, 0.5, [480, 620]], [1, 0.5, [480, 620]], [2, 0.5, [480, 620]]] },
  end: { volume: () => 0.07, notes: [[0, 0.12, [480]], [0.2, 0.12, [480]]] },
};
const DTMF = { 1: [697, 1209], 2: [697, 1336], 3: [697, 1477], 4: [770, 1209], 5: [770, 1336], 6: [770, 1477], 7: [852, 1209], 8: [852, 1336], 9: [852, 1477], 0: [941, 1336] };

const tones = {
  ctx: null,
  timer: null,
  live: new Set(),

  ensure() {
    if (!this.ctx) {
      const Ctor = window.AudioContext || window.webkitAudioContext;
      if (!Ctor) return null;
      this.ctx = new Ctor();
      this.ctx.onstatechange = () => renderBanner();
    }
    if (this.ctx.state === 'suspended') this.ctx.resume().catch(() => {});
    return this.ctx;
  },

  get unlocked() {
    return !!this.ctx && this.ctx.state === 'running';
  },

  beep(freqs, at, duration, volume) {
    const ctx = this.ctx;
    const gain = ctx.createGain();
    gain.gain.setValueAtTime(0, at);
    gain.gain.linearRampToValueAtTime(volume, at + 0.02);
    gain.gain.setValueAtTime(volume, at + duration - 0.03);
    gain.gain.linearRampToValueAtTime(0, at + duration);
    gain.connect(ctx.destination);
    for (const freq of freqs) {
      const osc = ctx.createOscillator();
      osc.frequency.value = freq;
      osc.connect(gain);
      osc.start(at);
      osc.stop(at + duration + 0.02);
      this.live.add(osc);
      osc.onended = () => this.live.delete(osc);
    }
  },

  play(kind) {
    this.stop();
    const ctx = this.ensure();
    if (!ctx) return;
    const tone = TONES[kind];
    const cycle = () => {
      const at = ctx.currentTime + 0.03;
      for (const [offset, duration, freqs] of tone.notes) this.beep(freqs, at + offset, duration, tone.volume());
    };
    cycle();
    if (tone.period) this.timer = setInterval(cycle, tone.period * 1000);
  },

  key(digit) {
    const ctx = this.ensure();
    if (ctx && DTMF[digit]) this.beep(DTMF[digit], ctx.currentTime + 0.01, 0.09, 0.05);
  },

  stop() {
    clearInterval(this.timer);
    this.timer = null;
    for (const osc of this.live) {
      try {
        osc.stop();
      } catch {
        /* already stopped */
      }
    }
    this.live.clear();
  },
};

// Browsers only let a page make sound after the user has touched it once.
for (const type of ['pointerdown', 'keydown']) {
  window.addEventListener(type, () => tones.ensure(), { capture: true, passive: true });
}

// ------------------------------------------------------------- microphone

function micErrorText(error) {
  if (!navigator.mediaDevices || !window.isSecureContext) {
    return 'The microphone is blocked because this page is not secure. Open the setup page from Settings.';
  }
  switch (error && error.name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return 'Microphone access is blocked. Allow it for this site in your browser settings.';
    case 'NotFoundError':
    case 'OverconstrainedError':
      return 'No microphone was found on this device.';
    case 'NotReadableError':
    case 'AbortError':
      return 'The microphone is in use by another app.';
    default:
      return 'Could not use the microphone.';
  }
}

async function getMic() {
  if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) throw new Error('insecure');
  const audio = { echoCancellation: true, noiseSuppression: true, autoGainControl: true };
  if (state.settings.micId) {
    try {
      return await navigator.mediaDevices.getUserMedia({ audio: { ...audio, deviceId: { exact: state.settings.micId } } });
    } catch (error) {
      if (error.name !== 'OverconstrainedError' && error.name !== 'NotFoundError') throw error;
      // The chosen microphone was unplugged: fall back to the system default.
    }
  }
  return navigator.mediaDevices.getUserMedia({ audio });
}

function stopStream(stream) {
  if (stream) for (const track of stream.getTracks()) track.stop();
}

// -------------------------------------------------------------- signalling

let ws = null;
let reconnectTimer = null;
let reconnectDelay = 500;
let heartbeat = null;
let lastHeard = 0;

function wsOpen() {
  return ws !== null && ws.readyState === WebSocket.OPEN;
}

function send(message) {
  if (!wsOpen()) return false;
  ws.send(JSON.stringify(message));
  return true;
}

function sendHello() {
  send({
    type: 'hello',
    v: PROTOCOL_VERSION,
    device_id: state.identity.deviceId,
    secret: state.identity.secret,
    name: pendingName || (state.me && state.me.name) || '',
    ext: state.me ? state.me.ext : undefined,
    pin: pendingPin || undefined,
  });
}

function connect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = null;
  if (!state.identity || state.conn === 'blocked' || state.conn === 'refused') return;
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  const socket = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}${BASE}/ws`);
  ws = socket;
  socket.onopen = () => {
    lastHeard = Date.now();
    sendHello();
  };
  socket.onmessage = (event) => {
    lastHeard = Date.now();
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    try {
      onMessage(message);
    } catch (error) {
      console.error('Failed to handle', message && message.type, error);
    }
  };
  socket.onclose = () => {
    if (ws !== socket) return;
    ws = null;
    if (state.sup) endSupervision('Monitoring stopped: the connection was lost');
    if (state.conn === 'replaced' || state.conn === 'blocked' || state.conn === 'refused') return;
    setConn('offline');
    reconnectTimer = setTimeout(connect, reconnectDelay + Math.random() * 400);
    reconnectDelay = Math.min(reconnectDelay * 2, 10000);
  };

  clearInterval(heartbeat);
  heartbeat = setInterval(() => {
    if (!wsOpen()) return;
    // A dead Wi-Fi link can leave a socket "open" for minutes; notice it ourselves.
    if (Date.now() - lastHeard > 35000) ws.close();
    else send({ type: 'ping' });
  }, 15000);
}

function reconnectNow() {
  if (wsOpen() || state.conn === 'replaced' || state.conn === 'blocked' || state.conn === 'refused') return;
  reconnectDelay = 500;
  connect();
}

function setConn(value) {
  state.conn = value;
  renderHeader();
  renderBanner();
}

function onMessage(msg) {
  switch (msg.type) {
    case 'welcome':
      return onWelcome(msg);
    case 'profile':
      state.me = msg.me;
      storage.set('me', state.me);
      renderHeader();
      return undefined;
    case 'directory':
      state.directory = msg.entries || [];
      renderDirectory();
      renderDialMatch();
      return undefined;
    case 'ringing':
      return onRinging(msg);
    case 'incoming':
      return onIncoming(msg);
    case 'accepted':
      return onAccepted(msg);
    case 'call_failed':
      if (state.call && !state.call.id && state.call.phase === 'dialing') finishCall(msg.reason);
      return undefined;
    case 'ended':
      if (state.call && state.call.id && state.call.id === msg.call_id) {
        const next = msg.reason === 'transferred' && state.call.transferTo;
        if (next) {
          // The other person passed us on: ring the new extension straight away.
          finishCall('transferred', { silent: true });
          toast(`Transferred to ${next.name}`);
          startCall(next.ext, next.name);
        } else {
          finishCall(msg.reason === 'gone' ? 'hangup' : msg.reason);
        }
      }
      return undefined;
    case 'transfer':
      if (state.call && state.call.id === msg.call_id && msg.to) state.call.transferTo = msg.to;
      return undefined;
    case 'transfer_failed':
      toast(`Could not transfer: ${TRANSFER_FAIL[msg.reason] || 'try again'}`);
      return undefined;
    case 'role':
      state.role = msg.role === 'supervisor' ? 'supervisor' : 'user';
      renderRole();
      return undefined;
    case 'calls':
      state.liveCalls = msg.entries || [];
      renderLiveCalls();
      return undefined;
    case 'sup_join':
      return onSupJoin(msg);
    case 'sup_update':
      if (state.call && state.call.sup && state.call.sup.id === msg.sup_id) {
        state.call.sup.state = msg.state;
        state.call.sup.by = msg.by;
        renderCall();
      }
      return undefined;
    case 'sup_signal':
      if (state.sup && state.sup.id === msg.sup_id && state.sup.legs[msg.leg]) state.sup.legs[msg.leg].link.signal(msg.data, msg.from_ip);
      else if (state.call && state.call.sup && state.call.sup.id === msg.sup_id) state.call.sup.link.signal(msg.data, msg.from_ip);
      return undefined;
    case 'sup_started':
      return onSupStarted(msg);
    case 'sup_failed':
      if (state.sup && !state.sup.id) endSupervision(SUP_FAIL[msg.reason] || 'Could not monitor that call');
      return undefined;
    case 'sup_end':
      if (state.sup && state.sup.id === msg.sup_id) endSupervision(msg.reason === 'call_ended' ? 'The call ended' : '');
      else if (state.call && state.call.sup && state.call.sup.id === msg.sup_id) {
        closeCallSup(state.call);
        renderCall();
      }
      return undefined;
    case 'signal':
      if (state.call && state.call.id === msg.call_id && msg.data) {
        // Read synchronously: the hang-up that follows must find the reason already recorded.
        if (msg.data.failure === 'blocked') state.call.endOverride = END_TEXT.peer_blocked;
        else queueSignal(state.call, msg.data, msg.from_ip);
      }
      return undefined;
    case 'missed':
      addMissed(msg.entries || [], true);
      return undefined;
    case 'replaced':
      setConn('replaced');
      if (state.call) finishCall('replaced', { silent: true });
      return undefined;
    case 'error':
      return onServerError(msg);
    default:
      return undefined;
  }
}

function onWelcome(msg) {
  reconnectDelay = 500;
  pendingName = '';
  pendingPin = '';
  sessionStorage.removeItem('lancall.reloaded');
  state.me = msg.me;
  state.server = msg.server;
  state.role = msg.role === 'supervisor' ? 'supervisor' : 'user';
  state.iceServers = Array.isArray(msg.ice_servers) ? msg.ice_servers : [];
  storage.set('me', state.me);
  renderRole();
  document.title = baseTitle();
  $('about').textContent = `${msg.server.name} · version ${msg.server.version}`;

  if (!$('view-onboarding').hidden) showView('main');
  setConn('online');

  if (msg.missed && msg.missed.length) {
    addMissed(msg.missed, false);
    send({ type: 'missed_ack' });
  }
  reconcileCall(msg.call);
  checkCallsAllowed();
}

/* After a reconnect the server and this page may disagree about the call. */
function reconcileCall(serverCall) {
  const call = state.call && state.call.phase !== 'ended' ? state.call : null;
  if (!call) {
    if (!serverCall) return;
    if (serverCall.role === 'callee' && serverCall.state === 'ringing') {
      onIncoming({ call_id: serverCall.call_id, from: serverCall.peer }); // page was reloaded while ringing
    } else {
      send({ type: 'hangup', call_id: serverCall.call_id }); // left over from before a reload
    }
    return;
  }
  if (!serverCall) {
    finishCall(call.phase === 'dialing' ? 'not_connected' : 'connection_lost');
    return;
  }
  if (!call.id && serverCall.role === 'caller') {
    call.id = serverCall.call_id; // our "call" request got through just before the drop
    if (call.phase === 'dialing') {
      call.phase = 'ringing';
      tones.play('ringback');
    }
  }
  if (call.id !== serverCall.call_id) {
    send({ type: 'hangup', call_id: serverCall.call_id });
    finishCall('connection_lost');
    return;
  }
  if (call.role === 'caller' && call.phase === 'ringing' && serverCall.state === 'active') {
    onAccepted({ call_id: call.id }); // they answered while we were reconnecting
  } else if (call.recovering) {
    restartIce(call);
  }
  renderCall();
}

function onServerError(msg) {
  switch (msg.code) {
    case 'pin_required':
    case 'pin_invalid':
    case 'pin_locked':
      showOnboarding({ needPin: true, error: msg.code === 'pin_required' ? '' : msg.message });
      return;
    case 'auth_failed':
    case 'bad_identity':
    case 'removed':
      if (state.call) finishCall('admin', { silent: true });
      setConn('blocked');
      return;
    case 'disabled':
    case 'suspended':
    case 'directory_full':
      state.refusedText = msg.message;
      if (state.call) finishCall('admin', { silent: true });
      if (!state.me) showOnboarding({ error: msg.message });
      setConn('refused');
      return;
    case 'unsupported_version':
      // The server was upgraded under us; fetch the matching app, but only once.
      if (!sessionStorage.getItem('lancall.reloaded')) {
        sessionStorage.setItem('lancall.reloaded', '1');
        location.reload();
      } else {
        setConn('blocked');
      }
      return;
    case 'invalid_name':
    case 'invalid_ext':
    case 'ext_taken':
      if ($('settings').open) $('settings-error').textContent = msg.message;
      else toast(msg.message);
      return;
    case 'not_registered':
      return;
    default:
      if (msg.message) toast(msg.message);
  }
}

// ------------------------------------------------------------------ calls

const END_TEXT = {
  busy: 'Busy',
  declined: 'Call declined',
  no_answer: 'No answer',
  offline: 'Not available. They will see a missed call.',
  dnd: 'Do not disturb is on. They will see a missed call.',
  not_found: 'No phone has that extension',
  self: 'That is your own extension',
  already_in_call: 'You are already in a call',
  hangup: 'Call ended',
  cancelled: 'Call ended',
  local: 'Call ended',
  connection_lost: 'Call ended: the connection was lost',
  media_failed: 'Could not connect the audio between the two phones',
  not_connected: 'Not connected to the server',
  server_shutdown: 'Call ended: the phone system is restarting',
  replaced: 'Call ended: this phone was opened somewhere else',
  blocked: 'This browser is blocking calls. Turn off any VPN or proxy extension for this site, or use another browser.',
  peer_blocked: 'The other phone’s browser is blocking calls (a VPN or proxy extension).',
  transferred: 'Call transferred',
  admin: 'Call ended by the administrator',
};
const TRANSFER_FAIL = {
  not_found: 'no phone has that extension',
  self: 'that phone is already on this call',
  offline: 'that phone is not available',
  dnd: 'that phone is on do not disturb',
  busy: 'that phone is busy',
};
const SUP_FAIL = {
  forbidden: 'This phone is not allowed to monitor calls',
  busy: 'You are already on a call',
  gone: 'That call has ended',
  taken: 'Another supervisor is already on that call',
};
const SUP_NOTICE = {
  monitor: () => 'A supervisor is listening to this call',
  whisper: (by) => `${(by && by.name) || 'A supervisor'} is speaking only to you`,
  barge: (by) => `${(by && by.name) || 'A supervisor'} joined the call`,
};
const BUSY_TONE_REASONS = new Set(['busy', 'declined', 'offline', 'dnd', 'not_found', 'no_answer']);

function newCall(role, peer, phase) {
  return {
    id: null,
    role,
    peer: { ext: String(peer.ext || ''), name: peer.name || `Extension ${peer.ext}` },
    phase, // dialing | ringing | incoming | connecting | active | ended
    pc: null,
    stream: null,
    pending: [],
    chain: Promise.resolve(),
    muted: false,
    held: false,
    peerHeld: false,
    recovering: false,
    connectedAt: 0,
    endedAt: 0,
    endText: '',
    quality: '',
    declinedByMe: false,
    diag: { local: [], remote: [], errors: [], offer: false, answer: false },
    timers: {},
  };
}

function isCurrent(call) {
  return state.call === call && call.phase !== 'ended';
}

async function startCall(ext, name) {
  ext = String(ext || '').trim();
  if (!ext) return;
  if (state.call && state.call.phase === 'ended') clearCall();
  if (state.call || state.sup) return;
  if (state.me && ext === state.me.ext) {
    toast(END_TEXT.self);
    return;
  }
  if (!wsOpen() || state.conn !== 'online') {
    toast(END_TEXT.not_connected);
    return;
  }
  const known = state.directory.find((entry) => entry.ext === ext);
  const call = newCall('caller', { ext, name: name || (known && known.name) }, 'dialing');
  state.call = call;
  renderCall();

  // Get the microphone before the other phone rings, so a blocked microphone
  // never leaves someone answering a silent call.
  try {
    call.stream = await getMic();
  } catch (error) {
    if (isCurrent(call)) finishCall('mic', { text: micErrorText(error) });
    return;
  }
  if (!isCurrent(call)) {
    stopStream(call.stream);
    return;
  }
  if (!send({ type: 'call', to: ext })) finishCall('not_connected');
}

function onRinging(msg) {
  const call = state.call;
  const stale = !call || call.role !== 'caller' || call.id || call.phase !== 'dialing' || (msg.to && String(msg.to.ext) !== call.peer.ext);
  if (stale) {
    send({ type: 'hangup', call_id: msg.call_id }); // we gave up before the server answered
    return;
  }
  call.id = msg.call_id;
  if (Array.isArray(msg.ice_servers)) state.iceServers = msg.ice_servers;
  if (msg.to) call.peer = { ext: msg.to.ext, name: msg.to.name };
  call.phase = 'ringing';
  tones.play('ringback');
  renderCall();
}

function onIncoming(msg) {
  if (state.call && state.call.phase === 'ended') clearCall();
  if (state.call) {
    send({ type: 'decline', call_id: msg.call_id });
    return;
  }
  if (Array.isArray(msg.ice_servers)) state.iceServers = msg.ice_servers;
  const call = newCall('callee', msg.from || {}, 'incoming');
  call.id = msg.call_id;
  state.call = call;
  if ($('settings').open) $('settings').close();
  tones.play('ring');
  vibrate([400, 200, 400, 200, 400]);
  notifyIncoming(call);
  renderCall();
}

async function acceptCall() {
  const call = state.call;
  if (!call || call.phase !== 'incoming') return;
  tones.stop();
  clearNotification();
  call.phase = 'connecting';
  renderCall();
  try {
    call.stream = await getMic();
  } catch (error) {
    if (isCurrent(call)) {
      call.declinedByMe = true;
      send({ type: 'decline', call_id: call.id });
      finishCall('mic', { text: micErrorText(error) });
    }
    return;
  }
  if (!isCurrent(call)) {
    stopStream(call.stream);
    return;
  }
  createPeer(call);
  if (!send({ type: 'accept', call_id: call.id })) finishCall('not_connected');
}

function declineCall() {
  const call = state.call;
  if (!call || call.phase !== 'incoming') return;
  call.declinedByMe = true;
  send({ type: 'decline', call_id: call.id });
  finishCall('local');
}

function onAccepted(msg) {
  const call = state.call;
  if (!call || call.id !== msg.call_id || call.role !== 'caller' || call.phase !== 'ringing') return;
  tones.stop();
  call.phase = 'connecting';
  createPeer(call);
  negotiate(call, false);
  renderCall();
}

function hangUp() {
  const call = state.call;
  if (!call || call.phase === 'ended') return;
  if (call.id) send({ type: 'hangup', call_id: call.id });
  // With no id yet the server has not replied; onRinging cancels it when it does.
  finishCall('local');
}

function createPeer(call) {
  const pc = new RTCPeerConnection({ iceServers: state.iceServers });
  call.pc = pc;
  for (const track of call.stream.getTracks()) pc.addTrack(track, call.stream);

  pc.onicecandidate = (event) => {
    if (!event.candidate || !isCurrent(call)) return;
    call.diag.local.push(describeCandidate(event.candidate.candidate));
    send({ type: 'signal', call_id: call.id, data: { candidate: event.candidate } });
  };
  pc.onicegatheringstatechange = () => {
    // Finished looking for network paths and found none at all: something in
    // this browser (typically a VPN or proxy extension) forbids direct audio.
    // Say so at once instead of leaving both people on "Connecting".
    if (pc.iceGatheringState !== 'complete' || call.diag.local.length > 0 || !isCurrent(call) || call.phase === 'active') return;
    reportAudioProblem(call, 'no local candidates');
    send({ type: 'signal', call_id: call.id, data: { failure: 'blocked' } });
    send({ type: 'hangup', call_id: call.id });
    finishCall('blocked');
  };
  pc.onicecandidateerror = (event) => {
    if (call.diag.errors.length < 10) call.diag.errors.push(`ice ${event.errorCode} ${event.errorText || ''}`.trim());
  };
  pc.ontrack = (event) => {
    const audio = $('remote-audio');
    audio.srcObject = event.streams[0] || new MediaStream([event.track]);
    applySpeaker();
    audio.play().catch(() => {});
  };
  pc.onconnectionstatechange = () => {
    if (!isCurrent(call)) return;
    const s = pc.connectionState;
    if (s === 'connected') {
      clearTimeout(call.timers.setup);
      clearTimeout(call.timers.recover);
      clearTimeout(call.timers.restart);
      call.recovering = false;
      if (call.phase !== 'active') {
        call.phase = 'active';
        call.connectedAt = Date.now();
        call.timers.tick = setInterval(renderCallStatus, 1000);
        call.timers.stats = setInterval(() => sampleQuality(call), 3000);
      }
      renderCall();
    } else if ((s === 'disconnected' || s === 'failed') && call.phase === 'active') {
      // A Wi-Fi roam or a sleeping phone drops the path for a moment. Try to
      // find a new one before giving up on the call.
      if (!call.recovering) {
        call.recovering = true;
        call.timers.recover = setTimeout(() => {
          if (!isCurrent(call) || !call.recovering) return;
          reportAudioProblem(call, 'lost mid-call');
          send({ type: 'hangup', call_id: call.id });
          finishCall('connection_lost');
        }, MEDIA_RECOVER_TIMEOUT);
      }
      clearTimeout(call.timers.restart);
      if (s === 'failed') restartIce(call);
      else call.timers.restart = setTimeout(() => call.recovering && restartIce(call), 3000);
      renderCall();
    } else if (s === 'failed') {
      reportAudioProblem(call, 'ice failed during setup');
      send({ type: 'hangup', call_id: call.id });
      finishCall('media_failed');
    }
  };

  call.timers.setup = setTimeout(() => {
    if (!isCurrent(call) || call.phase === 'active') return;
    reportAudioProblem(call, 'setup timeout');
    send({ type: 'hangup', call_id: call.id });
    finishCall('media_failed');
  }, MEDIA_SETUP_TIMEOUT);
}

/* Only the caller ever makes offers, so the two sides can never collide. */
async function negotiate(call, iceRestart) {
  const pc = call.pc;
  if (!pc || call.role !== 'caller' || !isCurrent(call)) return;
  if (pc.signalingState !== 'stable') {
    clearTimeout(call.timers.negotiate);
    call.timers.negotiate = setTimeout(() => negotiate(call, iceRestart), 1000);
    return;
  }
  try {
    const offer = await pc.createOffer({ iceRestart });
    if (!isCurrent(call) || pc.signalingState !== 'stable') return;
    await pc.setLocalDescription(offer);
    send({ type: 'signal', call_id: call.id, data: { description: pc.localDescription } });
  } catch (error) {
    console.error('Offer failed', error);
  }
}

function restartIce(call) {
  if (!isCurrent(call) || !call.pc) return;
  if (call.role === 'caller') negotiate(call, true);
  else send({ type: 'signal', call_id: call.id, data: { restart: true } });
}

function queueSignal(call, data, peerIp) {
  call.chain = call.chain.then(() => handleSignal(call, data, peerIp)).catch((error) => {
    console.error('Signal failed', error);
    if (call.diag.errors.length < 10) call.diag.errors.push(`signal ${error && error.name}: ${error && error.message}`);
  });
}

/* "candidate:1 1 udp 2113937151 192.168.1.7 54321 typ host ..." */
function describeCandidate(line) {
  const parts = String(line || '').split(' ');
  const address = parts[4] || '?';
  const kind = address.endsWith('.local') ? 'mdns' : address;
  return `${parts[7] || '?'} ${String(parts[2] || '?').toLowerCase()} ${kind}`;
}

/*
 * A browser may advertise "<random>.local" instead of its LAN address. Finding
 * that name needs multicast DNS, which office Wi-Fi often blocks, and then the
 * two phones never find each other. The server saw the peer's real address when
 * it connected, so substitute it.
 */
function unmaskCandidate(line, peerIp) {
  if (!peerIp || typeof line !== 'string') return line;
  const parts = line.split(' ');
  if (parts.length > 5 && parts[4].endsWith('.local')) parts[4] = peerIp;
  return parts.join(' ');
}

function unmaskSdp(sdp, peerIp) {
  if (!peerIp || typeof sdp !== 'string' || !sdp.includes('.local')) return sdp;
  return sdp.split('\r\n').map((row) => (row.startsWith('a=candidate:') ? 'a=' + unmaskCandidate(row.slice(2), peerIp) : row)).join('\r\n');
}

/* Tell the server what this phone saw, so the cause can be found in data/lancall.log. */
function reportAudioProblem(call, what) {
  const pc = call.pc;
  send({
    type: 'report',
    data: {
      what,
      call_id: call.id,
      role: call.role,
      ice: pc && pc.iceConnectionState,
      gathering: pc && pc.iceGatheringState,
      signaling: pc && pc.signalingState,
      gotOffer: call.diag.offer,
      gotAnswer: call.diag.answer,
      local: call.diag.local.slice(0, 20),
      remote: call.diag.remote.slice(0, 20),
      errors: call.diag.errors,
      browser: navigator.userAgent,
    },
  });
}

async function flushCandidates(call) {
  const queued = call.pending.splice(0);
  for (const candidate of queued) {
    try {
      await call.pc.addIceCandidate(candidate);
    } catch {
      /* a stale candidate from before a restart */
    }
  }
}

async function handleSignal(call, data, peerIp) {
  const pc = call.pc;
  if ('hold' in data) {
    call.peerHeld = !!data.hold;
    renderCall();
    return;
  }
  if (!pc || !isCurrent(call)) return;
  if (data.description) {
    const description = { type: data.description.type, sdp: unmaskSdp(data.description.sdp, peerIp) };
    if (description.type === 'offer') call.diag.offer = true;
    else call.diag.answer = true;
    if (description.type === 'offer' && call.role === 'callee') {
      await pc.setRemoteDescription(description);
      await flushCandidates(call);
      await pc.setLocalDescription(await pc.createAnswer());
      send({ type: 'signal', call_id: call.id, data: { description: pc.localDescription } });
    } else if (description.type === 'answer' && pc.signalingState === 'have-local-offer') {
      await pc.setRemoteDescription(description);
      await flushCandidates(call);
    }
  } else if (data.candidate) {
    const candidate = { ...data.candidate, candidate: unmaskCandidate(data.candidate.candidate, peerIp) };
    call.diag.remote.push(describeCandidate(data.candidate.candidate));
    if (pc.remoteDescription) {
      try {
        await pc.addIceCandidate(candidate);
      } catch (error) {
        if (call.diag.errors.length < 10) call.diag.errors.push(`candidate ${error && error.message}`);
      }
    } else {
      call.pending.push(candidate);
    }
  } else if (data.restart && call.role === 'caller') {
    negotiate(call, true);
  }
}

function applyTrackState(call) {
  if (!call.stream) return;
  for (const track of call.stream.getAudioTracks()) track.enabled = !call.muted && !call.held;
  $('remote-audio').muted = call.held;
  $('sup-audio').muted = call.held;
}

// ---------------------------------------------------------------- transfer

function openTransfer() {
  const call = state.call;
  if (!call || call.phase !== 'active') return;
  const myExt = state.me && state.me.ext;
  $('transfer-list').replaceChildren(
    ...state.directory
      .filter((entry) => entry.status === 'online' && entry.ext !== myExt && entry.ext !== call.peer.ext)
      .map((entry) => el('option', { value: entry.ext, label: `${entry.name} (${entry.ext})` })),
  );
  $('transfer-ext').value = '';
  $('transfer').showModal();
}

// ------------------------------------------------------------- supervision
//
// A supervisor's phone opens its own audio connection to each of the two
// phones on a call. It always hears both. Its microphone reaches nobody
// (monitor), one of them (whisper) or both (barge).

function supLink(sendData, audio, iceServers) {
  const pc = new RTCPeerConnection({ iceServers: iceServers || state.iceServers });
  const pending = [];
  let chain = Promise.resolve();
  const flush = async () => {
    for (const candidate of pending.splice(0)) {
      try {
        await pc.addIceCandidate(candidate);
      } catch {
        /* stale */
      }
    }
  };
  pc.onicecandidate = (event) => {
    if (event.candidate) sendData({ candidate: event.candidate });
  };
  pc.ontrack = (event) => {
    audio.srcObject = event.streams[0] || new MediaStream([event.track]);
    if (audio.setSinkId) audio.setSinkId(state.settings.speakerId || '').catch(() => {});
    audio.play().catch(() => {});
  };
  const step = (work) => {
    chain = chain.then(work).catch((error) => console.error('Supervision signal failed', error));
  };
  return {
    pc,
    offer() {
      step(async () => {
        await pc.setLocalDescription(await pc.createOffer());
        sendData({ description: pc.localDescription });
      });
    },
    signal(data, peerIp) {
      step(async () => {
        if (pc.signalingState === 'closed') return;
        if (data.description) {
          const description = { type: data.description.type, sdp: unmaskSdp(data.description.sdp, peerIp) };
          if (description.type === 'offer') {
            await pc.setRemoteDescription(description);
            await flush();
            await pc.setLocalDescription(await pc.createAnswer());
            sendData({ description: pc.localDescription });
          } else if (pc.signalingState === 'have-local-offer') {
            await pc.setRemoteDescription(description);
            await flush();
          }
        } else if (data.candidate) {
          const candidate = { ...data.candidate, candidate: unmaskCandidate(data.candidate.candidate, peerIp) };
          if (pc.remoteDescription) await pc.addIceCandidate(candidate);
          else pending.push(candidate);
        }
      });
    },
    close() {
      pc.onicecandidate = pc.ontrack = pc.onconnectionstatechange = null;
      pc.close();
      audio.srcObject = null;
    },
  };
}

/* On a phone that is in the call: a supervisor is joining. */
function onSupJoin(msg) {
  const call = state.call;
  if (!call || call.id !== msg.call_id || !call.stream || call.phase === 'ended') return;
  closeCallSup(call);
  const link = supLink((data) => send({ type: 'sup_signal', sup_id: msg.sup_id, data }), $('sup-audio'), msg.ice_servers);
  for (const track of call.stream.getTracks()) link.pc.addTrack(track, call.stream);
  call.sup = { id: msg.sup_id, link, state: msg.state, by: msg.by };
  renderCall();
}

function closeCallSup(call) {
  if (!call.sup) return;
  call.sup.link.close();
  call.sup = null;
}

/* On the supervisor's phone. */
async function startSupervision(entry) {
  if (state.call || state.sup) return;
  if (!wsOpen() || state.conn !== 'online') {
    toast(END_TEXT.not_connected);
    return;
  }
  const sup = { id: null, callId: entry.call_id, mode: 'monitor', leg: null, parties: [entry.a, entry.b].map((p, i) => ({ leg: 'ab'[i], ...p })), legs: {}, stream: null, startedAt: 0, timers: {} };
  state.sup = sup;
  renderSup();
  try {
    sup.stream = await getMic();
  } catch {
    sup.stream = null; // no microphone: listening still works, talking does not
  }
  if (state.sup !== sup) {
    stopStream(sup.stream);
    return;
  }
  if (!send({ type: 'supervise', call_id: entry.call_id })) endSupervision(END_TEXT.not_connected);
}

function onSupStarted(msg) {
  const sup = state.sup;
  if (!sup || sup.id || sup.callId !== msg.call_id) {
    send({ type: 'sup_stop', sup_id: msg.sup_id });
    return;
  }
  sup.id = msg.sup_id;
  sup.parties = msg.parties || sup.parties;
  sup.startedAt = Date.now();
  if (Array.isArray(msg.ice_servers)) state.iceServers = msg.ice_servers;
  for (const party of sup.parties) {
    const leg = party.leg;
    const link = supLink((data) => send({ type: 'sup_signal', sup_id: sup.id, leg, data }), $('sup-audio-' + leg));
    let track = null;
    if (sup.stream) {
      // A copy per leg, so the microphone can be open to one phone and closed to the other.
      track = sup.stream.getAudioTracks()[0].clone();
      track.enabled = false;
      link.pc.addTrack(track, new MediaStream([track]));
    } else {
      link.pc.addTransceiver('audio', { direction: 'recvonly' });
    }
    link.pc.onconnectionstatechange = () => state.sup === sup && renderSup();
    sup.legs[leg] = { link, track };
    link.offer();
  }
  sup.timers.tick = setInterval(renderSupStatus, 1000);
  sup.timers.setup = setTimeout(() => {
    if (state.sup === sup && !supConnected(sup)) endSupervision('Could not connect the audio to that call');
  }, MEDIA_SETUP_TIMEOUT);
  renderSup();
}

function supConnected(sup) {
  const legs = Object.values(sup.legs);
  return legs.length > 0 && legs.every((leg) => leg.link.pc.connectionState === 'connected');
}

function setSupMode(mode, leg) {
  const sup = state.sup;
  if (!sup || !sup.id || (mode !== 'monitor' && !sup.stream)) return;
  sup.mode = mode;
  sup.leg = mode === 'whisper' ? leg : null;
  for (const [name, item] of Object.entries(sup.legs)) {
    if (item.track) item.track.enabled = mode === 'barge' || (mode === 'whisper' && name === leg);
  }
  send({ type: 'sup_mode', sup_id: sup.id, mode, leg: sup.leg });
  renderSup();
}

function endSupervision(text) {
  const sup = state.sup;
  if (!sup) return;
  state.sup = null;
  if (sup.id) send({ type: 'sup_stop', sup_id: sup.id });
  for (const timer of Object.values(sup.timers)) {
    clearTimeout(timer);
    clearInterval(timer);
  }
  for (const item of Object.values(sup.legs)) {
    item.link.close();
    if (item.track) item.track.stop();
  }
  stopStream(sup.stream);
  showView(state.identity && state.me ? 'main' : 'onboarding');
  if (text) toast(text);
  renderLiveCalls();
  updateWakeLock();
}

function renderSupStatus() {
  const sup = state.sup;
  if (!sup) return;
  const text = !sup.id || !supConnected(sup) ? 'Connecting…' : formatDuration((Date.now() - sup.startedAt) / 1000);
  if ($('sup-status').textContent !== text) $('sup-status').textContent = text;
}

function renderSup() {
  const sup = state.sup;
  if (!sup) return;
  showView('sup');
  const [a, b] = sup.parties;
  $('sup-names').textContent = `${a.name} (${a.ext}) and ${b.name} (${b.ext})`;
  const whisperTo = sup.parties.find((party) => party.leg === sup.leg);
  $('sup-mode').textContent = {
    monitor: 'Listening only. They cannot hear you.',
    whisper: `Only ${whisperTo ? whisperTo.name : 'one person'} can hear you.`,
    barge: 'You are in the call. Both can hear you.',
  }[sup.mode];
  const ready = !!sup.id;
  const options = [
    ['sup-listen', 'monitor', null, 'Listen only'],
    ['sup-whisper-a', 'whisper', 'a', `Whisper to ${a.name}`],
    ['sup-whisper-b', 'whisper', 'b', `Whisper to ${b.name}`],
    ['sup-barge', 'barge', null, 'Barge in'],
  ];
  for (const [id, mode, leg, label] of options) {
    const button = $(id);
    button.textContent = label;
    button.disabled = !ready || (mode !== 'monitor' && !sup.stream);
    button.setAttribute('aria-pressed', String(sup.mode === mode && (sup.leg || null) === leg));
  }
  $('sup-nomic').hidden = !ready || !!sup.stream;
  renderSupStatus();
  updateWakeLock();
}

function renderRole() {
  const supervisor = state.role === 'supervisor';
  $('tab-calls').hidden = !supervisor;
  if (!supervisor) {
    state.liveCalls = [];
    if (state.tab === 'calls') selectTab('directory');
    if (state.sup) endSupervision('Monitoring was switched off for this phone');
  }
  renderLiveCalls();
}

function renderLiveCalls() {
  const myExt = state.me && state.me.ext;
  const calls = state.liveCalls.filter((entry) => entry.a.ext !== myExt && entry.b.ext !== myExt);
  $('live-calls').replaceChildren(
    ...calls.map((entry) =>
      el('li', {},
        el('div', { class: 'who' },
          el('div', { class: 'name', text: `${entry.a.name} and ${entry.b.name}` }),
          el('div', { class: 'meta num', text: `${entry.a.ext} · ${entry.b.ext} · since ${formatWhen(entry.answered || entry.started)}${entry.supervisor ? ` · monitored by ${entry.supervisor}` : ''}` })),
        el('button', { type: 'button', class: 'btn small', disabled: !!entry.supervisor || !!state.call || !!state.sup, onclick: () => startSupervision(entry) }, 'Monitor'))),
  );
  $('live-calls-empty').hidden = calls.length > 0;
  const badge = $('calls-badge');
  badge.hidden = calls.length === 0;
  badge.textContent = String(calls.length);
}

function toggleMute() {
  const call = state.call;
  if (!call || call.phase !== 'active') return;
  call.muted = !call.muted;
  applyTrackState(call);
  renderCall();
}

function toggleHold() {
  const call = state.call;
  if (!call || call.phase !== 'active') return;
  call.held = !call.held;
  applyTrackState(call);
  send({ type: 'signal', call_id: call.id, data: { hold: call.held } });
  renderCall();
}

async function sampleQuality(call) {
  if (!isCurrent(call) || !call.pc) return;
  try {
    const stats = await call.pc.getStats();
    let rtt = 0;
    let lost = 0;
    let received = 0;
    stats.forEach((report) => {
      if (report.type === 'remote-inbound-rtp' && report.kind === 'audio' && report.roundTripTime) rtt = report.roundTripTime;
      if (report.type === 'inbound-rtp' && report.kind === 'audio') {
        lost = report.packetsLost || 0;
        received = report.packetsReceived || 0;
      }
    });
    const previous = call.lastStats || { lost: 0, received: 0 };
    call.lastStats = { lost, received };
    const dLost = Math.max(0, lost - previous.lost);
    const dTotal = dLost + Math.max(0, received - previous.received);
    const loss = dTotal > 0 ? dLost / dTotal : 0;
    call.quality = loss > 0.08 || rtt > 0.4 ? 'Poor connection' : loss > 0.02 || rtt > 0.2 ? 'Weak connection' : '';
    renderCallStatus();
  } catch {
    /* stats are a nicety */
  }
}

function finishCall(reason, options = {}) {
  const call = state.call;
  if (!call || call.phase === 'ended') return;
  const wasIncoming = call.phase === 'incoming';
  const reached = call.phase !== 'dialing';

  tones.stop();
  clearNotification();
  vibrate(0);
  for (const timer of Object.values(call.timers)) {
    clearTimeout(timer);
    clearInterval(timer);
  }
  if (call.pc) {
    call.pc.onicecandidate = call.pc.onicegatheringstatechange = call.pc.onicecandidateerror = call.pc.ontrack = call.pc.onconnectionstatechange = null;
    call.pc.close();
    call.pc = null;
  }
  closeCallSup(call);
  if ($('transfer').open) $('transfer').close();
  stopStream(call.stream);
  call.stream = null;
  const audio = $('remote-audio');
  audio.srcObject = null;
  audio.muted = false;
  $('sup-audio').muted = false;

  call.endedAt = Date.now();
  call.phase = 'ended';
  call.endText = options.text || call.endOverride || END_TEXT[reason] || 'Call ended';
  logCall(call);

  if (!options.silent) {
    if (call.role === 'caller' && BUSY_TONE_REASONS.has(reason)) tones.play('busy');
    else if (reached && !wasIncoming) tones.play('end');
  }
  // An unanswered incoming call needs no "ended" screen: just stop ringing.
  if (wasIncoming || options.silent) {
    clearCall();
    return;
  }
  renderCall();
  call.timers.dismiss = setTimeout(() => state.call === call && clearCall(), call.endText.length > 30 ? 5000 : 2500);
}

function clearCall() {
  const call = state.call;
  if (call) clearTimeout(call.timers.dismiss);
  state.call = null;
  renderCall();
  renderLiveCalls();
  updateWakeLock();
}

// ---------------------------------------------------------------- history

function saveHistory() {
  state.history.length = Math.min(state.history.length, HISTORY_LIMIT);
  storage.set('history', state.history);
  renderRecents();
}

function logCall(call) {
  const answered = call.connectedAt > 0;
  if (call.role === 'callee' && !answered && !call.declinedByMe) return; // arrives as a "missed" entry from the server
  if (!call.peer.ext) return;
  state.history.unshift({
    dir: call.role === 'caller' ? 'out' : 'in',
    ext: call.peer.ext,
    name: call.peer.name,
    ts: Date.now(),
    duration: answered ? Math.round((call.endedAt - call.connectedAt) / 1000) : 0,
    answered,
  });
  saveHistory();
}

function addMissed(entries, live) {
  let added = 0;
  for (const entry of entries) {
    if (!entry || !entry.id || state.history.some((item) => item.id === entry.id)) continue;
    state.history.unshift({ id: entry.id, dir: 'missed', ext: String(entry.ext), name: entry.name, ts: entry.ts || Date.now(), duration: 0, answered: false });
    added += 1;
    if (live) toast(`Missed call from ${entry.name}`);
  }
  if (!added) return;
  state.history.sort((a, b) => b.ts - a.ts);
  if (state.tab !== 'recents' || document.hidden) {
    state.unseenMissed += added;
    storage.set('unseenMissed', state.unseenMissed);
  }
  if (!live) toast(added === 1 ? 'You have 1 missed call' : `You have ${added} missed calls`);
  saveHistory();
  renderBadge();
}

// ------------------------------------------------------------ notifications

let swRegistration = null;
let pageNotification = null;
const NOTIFY_TAG = 'lancall-incoming';

if ('serviceWorker' in navigator && window.isSecureContext) {
  // Fails on an untrusted certificate; notifications then fall back to the page.
  navigator.serviceWorker.register('sw.js').then((registration) => {
    swRegistration = registration;
  }).catch(() => {});
}

function notifyIncoming(call) {
  if (!document.hidden || !('Notification' in window) || Notification.permission !== 'granted') return;
  const title = `Incoming call: ${call.peer.name}`;
  const options = { body: `Extension ${call.peer.ext}`, tag: NOTIFY_TAG, icon: 'icon.svg', requireInteraction: true };
  if (swRegistration) {
    swRegistration.showNotification(title, options).catch(() => {});
    return;
  }
  try {
    pageNotification = new Notification(title, options);
    pageNotification.onclick = () => window.focus();
  } catch {
    /* Android only allows notifications through a service worker */
  }
}

function clearNotification() {
  if (pageNotification) {
    pageNotification.close();
    pageNotification = null;
  }
  if (swRegistration && swRegistration.getNotifications) {
    swRegistration.getNotifications({ tag: NOTIFY_TAG }).then((list) => list.forEach((n) => n.close())).catch(() => {});
  }
}

// --------------------------------------------------------------- wake lock

let wakeLock = null;

async function updateWakeLock() {
  const wanted = (state.settings.keepAwake || !!state.call || !!state.sup) && !document.hidden;
  if (!('wakeLock' in navigator)) return;
  try {
    if (wanted && !wakeLock) {
      wakeLock = await navigator.wakeLock.request('screen');
      wakeLock.addEventListener('release', () => {
        wakeLock = null;
      });
    } else if (!wanted && wakeLock) {
      await wakeLock.release();
      wakeLock = null;
    }
  } catch {
    /* denied on low battery, for example */
  }
}

// --------------------------------------------------------------- rendering

function baseTitle() {
  const name = (state.server && state.server.name) || 'LAN Call';
  return state.unseenMissed > 0 ? `(${state.unseenMissed}) ${name}` : name;
}

function renderHeader() {
  const me = state.me;
  $('my-name').textContent = me ? me.name : ' ';
  $('my-ext').textContent = me ? me.ext : '';
  $('my-ext-wrap').hidden = !me;
  $('dnd').checked = !!(me && me.dnd);
  $('dnd').disabled = state.conn !== 'online';
  const online = state.conn === 'online';
  $('conn-dot').dataset.state = !online ? (state.conn === 'blocked' ? 'error' : 'offline') : me && me.dnd ? 'dnd' : 'online';
  $('conn-text').textContent = online ? (me && me.dnd ? 'Do not disturb' : 'Ready') : state.conn === 'connecting' ? 'Connecting…' : 'Not connected';
}

let bannerAction = null;

function renderBanner() {
  let text = '';
  let label = '';
  bannerAction = null;
  if (!window.RTCPeerConnection || !window.WebSocket) {
    text = 'This browser cannot make calls. Use a current version of Chrome, Edge, Firefox or Safari.';
  } else if (state.conn === 'replaced') {
    text = 'This phone is open in another tab or window.';
    label = 'Use it here';
    bannerAction = () => {
      setConn('connecting');
      connect();
    };
  } else if (state.conn === 'blocked') {
    text = 'The server did not accept this phone’s saved identity. Resetting gives it a new extension.';
    label = 'Reset this phone';
    bannerAction = () => {
      storage.remove('identity');
      storage.remove('me');
      location.reload();
    };
  } else if (!window.isSecureContext || !navigator.mediaDevices) {
    text = 'Calls are disabled because this page is not secure.';
    label = 'How to fix';
    bannerAction = () => {
      location.href = '/setup';
    };
  } else if (state.conn === 'refused') {
    text = state.refusedText || 'The phone system did not accept this phone.';
    label = 'Try again';
    bannerAction = () => {
      setConn('connecting');
      connect();
    };
  } else if (state.conn === 'offline') {
    text = 'Not connected to the phone system. Reconnecting…';
  } else if (state.callsBlocked) {
    text = END_TEXT.blocked;
    label = 'Check again';
    bannerAction = checkCallsAllowed;
  } else if (!tones.unlocked) {
    text = 'Tap or click anywhere so this phone can ring out loud.';
  }
  $('banner').hidden = !text;
  $('banner-text').textContent = text;
  $('banner-action').hidden = !label;
  $('banner-action').textContent = label;
}

const STATUS_TEXT = { online: 'Available', busy: 'On a call', dnd: 'Do not disturb', offline: 'Offline' };

function renderDirectory() {
  const list = $('directory');
  const query = $('search').value.trim().toLowerCase();
  const myExt = state.me && state.me.ext;
  const others = state.directory.filter((entry) => entry.ext !== myExt);
  const shown = others.filter((entry) => !query || entry.name.toLowerCase().includes(query) || entry.ext.includes(query));
  const rank = { online: 0, busy: 1, dnd: 2, offline: 3 };
  shown.sort((a, b) => (rank[a.status] - rank[b.status]) || a.name.localeCompare(b.name));

  list.replaceChildren(
    ...shown.map((entry) =>
      el('li', { class: entry.status === 'offline' ? 'offline' : '' },
        el('div', { class: 'avatar', 'aria-hidden': 'true', text: initials(entry.name) }),
        el('div', { class: 'who' },
          el('div', { class: 'name', text: entry.name }),
          el('div', { class: 'meta' },
            el('span', { class: 'dot', 'data-state': entry.status }),
            el('span', { class: 'num', text: `${entry.ext} · ${STATUS_TEXT[entry.status] || entry.status}` }))),
        el('button', { type: 'button', class: 'call-btn', 'aria-label': `Call ${entry.name}, extension ${entry.ext}`, onclick: () => startCall(entry.ext, entry.name) }, icon('i-phone')))),
  );
  const empty = $('directory-empty');
  empty.hidden = shown.length > 0;
  $('directory-empty-text').textContent = others.length === 0
    ? `No other phones yet. Open ${location.origin}${BASE}/ on another device to add one.`
    : 'No match.';
}

const timeFormat = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' });
const dateFormat = new Intl.DateTimeFormat(undefined, { month: 'short', day: 'numeric' });

function formatWhen(ts) {
  const date = new Date(ts);
  return date.toDateString() === new Date().toDateString() ? timeFormat.format(date) : `${dateFormat.format(date)}, ${timeFormat.format(date)}`;
}

function formatDuration(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const pad = (n) => String(n).padStart(2, '0');
  return s >= 3600 ? `${Math.floor(s / 3600)}:${pad(Math.floor((s % 3600) / 60))}:${pad(s % 60)}` : `${Math.floor(s / 60)}:${pad(s % 60)}`;
}

function renderRecents() {
  const labels = { in: 'Incoming', out: 'Outgoing', missed: 'Missed' };
  $('recents').replaceChildren(
    ...state.history.map((item) => {
      const detail = item.dir === 'missed' ? 'Missed' : item.answered ? `${labels[item.dir]} · ${formatDuration(item.duration)}` : item.dir === 'out' ? 'Outgoing · not answered' : 'Incoming · declined';
      const glyph = icon(item.dir === 'missed' ? 'i-missed' : item.dir === 'out' ? 'i-out' : 'i-in');
      glyph.classList.add('dir-icon');
      return el('li', { class: item.dir === 'missed' ? 'missed' : '' },
        glyph,
        el('div', { class: 'who' },
          el('div', { class: 'name', text: item.name || `Extension ${item.ext}` }),
          el('div', { class: 'meta num', text: `${item.ext} · ${detail}` })),
        el('div', { class: 'when', text: formatWhen(item.ts) }),
        el('button', { type: 'button', class: 'call-btn', 'aria-label': `Call ${item.name || item.ext}`, onclick: () => startCall(item.ext, item.name) }, icon('i-phone')));
    }),
  );
  $('recents-empty').hidden = state.history.length > 0;
  $('clear-recents').hidden = state.history.length === 0;
}

function renderBadge() {
  const badge = $('missed-badge');
  badge.hidden = state.unseenMissed <= 0;
  badge.textContent = state.unseenMissed > 99 ? '99+' : String(state.unseenMissed);
  badge.setAttribute('aria-label', `${state.unseenMissed} missed`);
  if (!state.call || state.call.phase !== 'incoming') document.title = baseTitle();
}

function callStatusText(call) {
  switch (call.phase) {
    case 'dialing':
      return 'Calling…';
    case 'ringing':
      return 'Ringing…';
    case 'incoming':
      return 'Incoming call';
    case 'connecting':
      return 'Connecting…';
    case 'ended':
      return call.endText;
    default:
      if (call.recovering) return 'Reconnecting…';
      if (call.held) return 'On hold';
      if (call.peerHeld) return 'They put you on hold';
      return formatDuration((Date.now() - call.connectedAt) / 1000) + (call.muted ? ' · muted' : '');
  }
}

function renderCallStatus() {
  const call = state.call;
  if (!call) return;
  const text = callStatusText(call);
  // Only touch the live region when the words change, or a screen reader would read the clock every second.
  const status = $('call-status');
  if (call.phase === 'active' && !call.recovering && !call.held && !call.peerHeld) {
    status.setAttribute('aria-live', 'off');
  } else {
    status.setAttribute('aria-live', 'polite');
  }
  if (status.textContent !== text) status.textContent = text;
  const quality = $('call-quality');
  quality.hidden = !(call.phase === 'active' && call.quality);
  quality.textContent = call.quality;
}

function renderCall() {
  const call = state.call;
  if (!call) {
    if (!$('view-call').hidden) showView(state.identity && state.me ? 'main' : 'onboarding');
    document.title = baseTitle();
    return;
  }
  const wasHidden = $('view-call').hidden;
  showView('call');
  const notice = call.sup && SUP_NOTICE[call.sup.state] ? SUP_NOTICE[call.sup.state](call.sup.by) : '';
  $('call-sup').hidden = !notice || call.phase === 'ended';
  if ($('call-sup').textContent !== notice) $('call-sup').textContent = notice;
  $('btn-transfer').disabled = call.phase !== 'active';
  $('view-call').dataset.phase = call.phase;
  $('call-name').textContent = call.peer.name;
  $('call-ext').textContent = call.peer.ext ? `Extension ${call.peer.ext}` : '';
  $('call-avatar').textContent = initials(call.peer.name);
  $('call-incoming').hidden = call.phase !== 'incoming';
  $('call-live').hidden = call.phase === 'incoming' || call.phase === 'ended';
  $('call-done').hidden = call.phase !== 'ended';
  const live = call.phase === 'active';
  $('btn-mute').disabled = !live;
  $('btn-hold').disabled = !live;
  $('btn-mute').setAttribute('aria-pressed', String(call.muted));
  $('btn-mute').lastElementChild.textContent = call.muted ? 'Unmute' : 'Mute';
  $('mute-icon').setAttribute('href', call.muted ? '#i-mic-off' : '#i-mic');
  $('btn-hold').setAttribute('aria-pressed', String(call.held));
  $('btn-hold').lastElementChild.textContent = call.held ? 'Resume' : 'Hold';
  renderCallStatus();
  document.title = call.phase === 'incoming' ? `Incoming call: ${call.peer.name}` : baseTitle();
  if (wasHidden) {
    (call.phase === 'incoming' ? $('btn-accept') : $('btn-hangup')).focus({ preventScroll: true });
    updateWakeLock();
  }
}

function renderDialMatch() {
  const value = $('dial-input').value;
  const match = value && state.directory.find((entry) => entry.ext === value);
  $('dial-match').textContent = match ? match.name : ' ';
}

// -------------------------------------------------------------------- tabs

const TAB_NAMES = ['directory', 'keypad', 'recents', 'calls']; // "calls" shows for supervisors only
const nativeFocusgroup = Object.hasOwn(HTMLElement.prototype, 'focusGroup');

function selectTab(name) {
  state.tab = name;
  for (const tab of TAB_NAMES) {
    const button = $('tab-' + tab);
    const selected = tab === name;
    button.setAttribute('aria-selected', String(selected));
    button.toggleAttribute('focusgroupstart', selected);
    if (!nativeFocusgroup) button.tabIndex = selected ? 0 : -1; // roving tabindex where focusgroup is missing
    $('panel-' + tab).hidden = !selected;
  }
  if (name === 'recents' && state.unseenMissed) {
    state.unseenMissed = 0;
    storage.set('unseenMissed', 0);
    renderBadge();
  }
}

for (const name of TAB_NAMES) {
  const button = $('tab-' + name);
  button.addEventListener('click', () => selectTab(name));
  button.addEventListener('focus', () => state.tab !== name && selectTab(name)); // selection follows focus
  if (!nativeFocusgroup) {
    button.addEventListener('keydown', (event) => {
      const shown = TAB_NAMES.filter((tab) => !$('tab-' + tab).hidden);
      const index = shown.indexOf(name);
      const next = { ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: shown.length - 1 }[event.key];
      if (next === undefined) return;
      event.preventDefault();
      $('tab-' + shown[(next + shown.length) % shown.length]).focus();
    });
  }
}

// --------------------------------------------------------------- onboarding

function showOnboarding({ needPin = false, error = '' } = {}) {
  showView('onboarding');
  $('onboarding-pin-row').hidden = !needPin;
  $('onboarding-pin').required = needPin;
  $('onboarding-error').textContent = error;
  if (!$('onboarding-name').value) $('onboarding-name').value = pendingName || (state.me && state.me.name) || '';
  (needPin && $('onboarding-name').value ? $('onboarding-pin') : $('onboarding-name')).focus();
}

$('onboarding-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const name = $('onboarding-name').value.trim();
  if (!name) return;
  pendingName = name;
  pendingPin = $('onboarding-pin').value;
  $('onboarding-error').textContent = '';
  if (!state.identity) {
    state.identity = { deviceId: randomToken(18), secret: randomToken(32) };
    storage.set('identity', state.identity);
  }
  if (wsOpen()) sendHello();
  else connect();
});

// ----------------------------------------------------------------- settings

let micTest = null;

function stopMicTest() {
  if (!micTest) return;
  cancelAnimationFrame(micTest.frame);
  clearTimeout(micTest.timeout);
  stopStream(micTest.stream);
  micTest.source.disconnect();
  micTest = null;
  $('mic-level').value = 0;
  $('mic-test').textContent = 'Test microphone';
}

async function startMicTest() {
  if (micTest) {
    stopMicTest();
    return;
  }
  const ctx = tones.ensure();
  if (!ctx) return;
  const previous = state.settings.micId;
  state.settings.micId = $('set-mic').value;
  let stream;
  try {
    stream = await getMic();
  } catch (error) {
    $('settings-error').textContent = micErrorText(error);
    return;
  } finally {
    state.settings.micId = previous;
  }
  $('settings-error').textContent = '';
  const source = ctx.createMediaStreamSource(stream);
  const analyser = ctx.createAnalyser();
  analyser.fftSize = 512;
  source.connect(analyser);
  const samples = new Float32Array(analyser.fftSize);
  micTest = { stream, source, frame: 0, timeout: setTimeout(stopMicTest, 15000) };
  $('mic-test').textContent = 'Stop test';
  fillDeviceLists(); // labels become available once permission is granted
  const tick = () => {
    if (!micTest) return;
    analyser.getFloatTimeDomainData(samples);
    let peak = 0;
    for (const sample of samples) peak = Math.max(peak, Math.abs(sample));
    $('mic-level').value = Math.min(1, peak * 1.6);
    micTest.frame = requestAnimationFrame(tick);
  };
  tick();
}

async function fillDeviceLists() {
  const mic = $('set-mic');
  const speaker = $('set-speaker');
  let devices = [];
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch {
    /* insecure page or no permission */
  }
  const fill = (select, kind, chosen, word) => {
    const options = [el('option', { value: '', text: 'System default' })];
    let n = 0;
    for (const device of devices) {
      if (device.kind !== kind || !device.deviceId || device.deviceId === 'default') continue;
      n += 1;
      options.push(el('option', { value: device.deviceId, text: device.label || `${word} ${n}` }));
    }
    select.replaceChildren(...options);
    select.value = [...select.options].some((option) => option.value === chosen) ? chosen : '';
  };
  fill(mic, 'audioinput', mic.value || state.settings.micId, 'Microphone');
  fill(speaker, 'audiooutput', speaker.value || state.settings.speakerId, 'Speaker');
  $('set-speaker-row').hidden = !('setSinkId' in HTMLMediaElement.prototype);
}

function applySpeaker() {
  const audio = $('remote-audio');
  if (audio.setSinkId) audio.setSinkId(state.settings.speakerId || '').catch(() => {});
}

function renderNotifyRow() {
  const supported = 'Notification' in window;
  const permission = supported ? Notification.permission : 'unsupported';
  $('notify-enable').hidden = permission !== 'default';
  $('notify-text').textContent = {
    granted: 'Notifications are on: you are told about calls while this tab is in the background.',
    denied: 'Notifications are blocked for this site in your browser settings.',
    default: 'Get told about calls while this tab is in the background.',
    unsupported: 'This browser does not support notifications.',
  }[permission];
}

function openSettings() {
  if (!state.me) return;
  $('set-name').value = state.me.name;
  $('set-ext').value = state.me.ext;
  $('set-volume').value = state.settings.ringVolume;
  $('set-awake').checked = state.settings.keepAwake;
  $('settings-error').textContent = '';
  $('set-mic').replaceChildren();
  $('set-speaker').replaceChildren();
  fillDeviceLists();
  renderNotifyRow();
  $('settings').showModal();
}

$('open-settings').addEventListener('click', openSettings);
$('settings-cancel').addEventListener('click', () => $('settings').close());
$('mic-test').addEventListener('click', startMicTest);
$('ring-test').addEventListener('click', () => {
  const saved = state.settings.ringVolume;
  state.settings.ringVolume = Number($('set-volume').value);
  tones.play('ring');
  state.settings.ringVolume = saved;
  setTimeout(() => (!state.call || state.call.phase !== 'incoming') && tones.stop(), 1700);
});
$('notify-enable').addEventListener('click', async () => {
  try {
    await Notification.requestPermission();
  } catch {
    /* ignore */
  }
  renderNotifyRow();
});
$('settings').addEventListener('close', () => {
  stopMicTest();
  if (!state.call || state.call.phase !== 'incoming') tones.stop();
});

$('settings-form').addEventListener('submit', (event) => {
  // Keep the dialog open until the server confirms: the extension may be taken.
  event.preventDefault();
  const name = $('set-name').value.trim();
  const ext = $('set-ext').value.trim();
  state.settings.micId = $('set-mic').value;
  state.settings.speakerId = $('set-speaker').value;
  state.settings.ringVolume = Number($('set-volume').value);
  state.settings.keepAwake = $('set-awake').checked;
  storage.set('settings', state.settings);
  applySpeaker();
  updateWakeLock();

  if (name === state.me.name && ext === state.me.ext) {
    $('settings').close();
    return;
  }
  if (!send({ type: 'set_profile', name, ext })) {
    $('settings-error').textContent = END_TEXT.not_connected;
    return;
  }
  const expected = `${name}\n${ext}`;
  const started = Date.now();
  const wait = setInterval(() => {
    if (!$('settings').open || $('settings-error').textContent) clearInterval(wait);
    else if (state.me && `${state.me.name}\n${state.me.ext}` === expected) {
      clearInterval(wait);
      $('settings').close();
    } else if (Date.now() - started > 5000) {
      clearInterval(wait);
      $('settings-error').textContent = 'The server did not answer. Try again.';
    }
  }, 100);
});

// Light dismiss for browsers without <dialog closedby>. Uses pointer position,
// not event.target, and needs press and release outside so a text-selection
// drag that ends on the backdrop does not close the dialog.
if (!('closedBy' in HTMLDialogElement.prototype)) {
  const dialog = $('settings');
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

// ------------------------------------------------------------------ wiring

$('banner-action').addEventListener('click', () => bannerAction && bannerAction());
$('search').addEventListener('input', renderDirectory);
$('dnd').addEventListener('change', (event) => {
  if (!send({ type: 'set_profile', dnd: event.target.checked })) renderHeader();
});

$('dial-input').addEventListener('input', (event) => {
  event.target.value = event.target.value.replace(/\D/g, '').slice(0, 6);
  renderDialMatch();
});
for (const key of document.querySelectorAll('.key[data-key]')) {
  key.addEventListener('click', () => {
    const input = $('dial-input');
    if (key.dataset.key === 'back') input.value = input.value.slice(0, -1);
    else if (input.value.length < 6) {
      input.value += key.dataset.key;
      tones.key(key.dataset.key);
    }
    renderDialMatch();
  });
}
$('dial-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const ext = $('dial-input').value;
  if (!ext) return;
  $('dial-input').value = '';
  renderDialMatch();
  startCall(ext);
});

$('clear-recents').addEventListener('click', () => {
  state.history = [];
  saveHistory();
});

$('btn-accept').addEventListener('click', acceptCall);
$('btn-decline').addEventListener('click', declineCall);
$('btn-hangup').addEventListener('click', hangUp);
$('btn-mute').addEventListener('click', toggleMute);
$('btn-hold').addEventListener('click', toggleHold);
$('btn-done').addEventListener('click', clearCall);
$('btn-transfer').addEventListener('click', openTransfer);
$('transfer-cancel').addEventListener('click', () => $('transfer').close());
$('transfer-form').addEventListener('submit', (event) => {
  event.preventDefault();
  const call = state.call;
  const to = $('transfer-ext').value.trim();
  if (!to || !call || call.phase !== 'active') return;
  send({ type: 'transfer', call_id: call.id, to });
  $('transfer').close();
});
$('transfer-ext').addEventListener('input', (event) => {
  event.target.value = event.target.value.replace(/\D/g, '').slice(0, 6);
});

$('sup-listen').addEventListener('click', () => setSupMode('monitor', null));
$('sup-whisper-a').addEventListener('click', () => setSupMode('whisper', 'a'));
$('sup-whisper-b').addEventListener('click', () => setSupMode('whisper', 'b'));
$('sup-barge').addEventListener('click', () => setSupMode('barge', null));
$('sup-stop').addEventListener('click', () => endSupervision(''));

document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  reconnectNow();
  updateWakeLock();
  if (state.tab === 'recents') selectTab('recents');
  if (state.call && state.call.phase !== 'incoming') clearNotification();
});
window.addEventListener('online', reconnectNow);
window.addEventListener('pageshow', (event) => event.persisted && reconnectNow());

// Closing the tab ends the call for the other side straight away instead of
// leaving them to wait for the timeout.
window.addEventListener('pagehide', () => {
  const call = state.call;
  if (call && call.id && call.phase !== 'ended') send({ type: call.phase === 'incoming' ? 'decline' : 'hangup', call_id: call.id });
  if (state.sup && state.sup.id) send({ type: 'sup_stop', sup_id: state.sup.id });
});

if (navigator.mediaDevices && navigator.mediaDevices.addEventListener) {
  navigator.mediaDevices.addEventListener('devicechange', async () => {
    if ($('settings').open) fillDeviceLists();
    // The microphone in use was unplugged mid-call: switch to whatever is left.
    const call = state.call;
    if (!call || !call.pc || !call.stream) return;
    const track = call.stream.getAudioTracks()[0];
    if (track && track.readyState === 'live') return;
    try {
      const fresh = await getMic();
      if (!isCurrent(call) || !call.pc) {
        stopStream(fresh);
        return;
      }
      const sender = call.pc.getSenders().find((s) => s.track === track || (s.track && s.track.kind === 'audio'));
      if (sender) await sender.replaceTrack(fresh.getAudioTracks()[0]);
      const supSender = call.sup && call.sup.link.pc.getSenders().find((s) => s.track && s.track.kind === 'audio');
      if (supSender) await supSender.replaceTrack(fresh.getAudioTracks()[0]);
      stopStream(call.stream);
      call.stream = fresh;
      applyTrackState(call);
    } catch {
      toast('The microphone was disconnected.');
    }
  });
}

/*
 * Ask the browser for its network paths, with nothing on the other end. A
 * healthy browser offers at least one. VPN and proxy extensions commonly
 * forbid them all, and then no call can ever connect; better to say so up
 * front than on the first call.
 */
async function checkCallsAllowed() {
  if (!window.RTCPeerConnection || state.call) return;
  let found = 0;
  const pc = new RTCPeerConnection({ iceServers: state.iceServers });
  try {
    pc.createDataChannel('probe');
    const done = new Promise((resolve) => {
      pc.onicecandidate = (event) => (event.candidate ? (found += 1) : resolve());
      setTimeout(resolve, 5000);
    });
    await pc.setLocalDescription(await pc.createOffer());
    await done;
  } catch {
    found = 1; // could not run the check: do not raise a false alarm
  } finally {
    pc.close();
  }
  state.callsBlocked = found === 0;
  renderBanner();
}

// ------------------------------------------------------------------- start

selectTab('directory');
renderHeader();
renderRecents();
renderBadge();
renderBanner();
applySpeaker();
updateWakeLock();

if (state.identity) {
  showView('main');
  connect();
} else {
  showOnboarding();
}

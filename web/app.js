// The page: builds the controls, turns user input into commands, and renders status frames.

import { Clock, ClockAuthError } from './ble.js';
import {
  BLACK,
  DEFAULT_PASSWORD,
  EFFECTS,
  TUBES,
  effectAfterKey,
  encodeColor,
  encodeEffect,
  encodeKey,
  encodeTime,
  encodeTimeSync,
  encodeToggle,
  fullBrightness,
  hueToRgb,
  rgbToCss,
  rgbToHue,
  tubeCount,
  validatePassword,
} from './protocol.js';

const PASSWORD_STORAGE_KEY = 'guoran-nixie.password';
const TIME_SYNC_INTERVAL_MS = 60_000;
const TOAST_MS = 4000;
const LOG_LINES = 500;
/** Bluetooth traffic, what the user did on the page, and what the page told them. */
const LOG_MARKERS = { sent: '→', received: '←', info: '·', action: '●', message: 'i', error: '!' };

const TOGGLE_LABELS = {
  time_sync: 'Phone time sync',
  alarm_1: 'Alarm 1',
  alarm_2: 'Alarm 2',
  schedule: 'Scheduled power on/off',
  auto_brightness: 'Auto brightness',
  hour_12: '12-hour format',
  ambient_light: 'Ambient light sensing',
  hourly_chime: 'Hourly voice chime',
  english_voice: 'English voice',
  sensor_switch: 'Sensor switch',
};
const KEY_LABELS = {
  // The remote's › (‹ does the same): the next preset colour, steady.
  next: ['Next colour', 'Next preset backlight colour, steady'],
  mode: ['Next effect', 'Next backlight effect'],
  // Not ⏻: Android's system fonts have no glyph for it.
  power: ['POWER', 'Power'],
  back: ['↩', 'Back'],
  plus: ['+', 'Plus'],
  set: ['SET', 'Set'],
  ok: ['OK', 'OK'],
  minus: ['−', 'Minus'],
};
const EFFECT_LABELS = {
  steady: 'Steady',
  breathing: 'Breathing',
  color_breathing: 'Breathing through colours',
  rainbow: 'Rainbow',
  rainbow_per_tube: 'Rainbow, one colour per tube',
};
const TIME_LABELS = {
  alarm_1: 'Alarm 1',
  alarm_2: 'Alarm 2',
  power_on: 'Power on at',
  power_off: 'Power off at',
};

const clock = new Clock();
const $ = (selector) => document.querySelector(selector);
const pad = (value) => String(value).padStart(2, '0');
const clockTime = (date) => `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;

let status = null;
/** The backlight effect last set from this page, null until then: the clock cannot report it. */
let effect = null;
let toastTimer = null;
let pendingColor = null;
let colorInFlight = false;
let disconnectRequested = false;

const storage = {
  get(key) {
    try {
      return localStorage.getItem(key);
    } catch {
      return null;
    }
  },
  set(key, value) {
    try {
      localStorage.setItem(key, value);
    } catch {
      // Private mode or blocked storage: the password just is not remembered.
    }
  },
};

function element(tag, props = {}, children = []) {
  const node = Object.assign(document.createElement(tag), props);
  node.append(...children);
  return node;
}

function switchRow(toggle) {
  const input = element('input', { type: 'checkbox', role: 'switch' });
  input.dataset.toggle = toggle;
  return element('label', { className: 'switch' }, [element('span', { textContent: TOGGLE_LABELS[toggle] }), input]);
}

function buildControls() {
  for (const container of document.querySelectorAll('[data-switches]')) {
    container.append(...container.dataset.switches.split(' ').map(switchRow));
  }
  for (const container of document.querySelectorAll('[data-keys]')) {
    for (const key of container.dataset.keys.split(' ')) {
      const [glyph, label] = KEY_LABELS[key];
      const button = element('button', { type: 'button', textContent: glyph, title: label });
      button.setAttribute('aria-label', label);
      button.dataset.key = key;
      container.append(button);
    }
  }
  $('#effect').append(...EFFECTS.map((value) => element('option', { value, textContent: EFFECT_LABELS[value] })));
  $('#tubes').append(
    ...TUBES.map((toggle, index) => {
      const input = element('input', { type: 'checkbox' });
      input.dataset.toggle = toggle;
      input.setAttribute('aria-label', `Tube ${index + 1} backlight`);
      return element('label', { className: 'tube' }, [input, element('span', { textContent: String(index + 1) })]);
    }),
  );
  for (const container of document.querySelectorAll('[data-time]')) {
    const setting = container.dataset.time;
    const input = element('input', { type: 'time', step: 60, value: '00:00' });
    input.dataset.setting = setting;
    container.append(element('label', { className: 'row' }, [element('span', { textContent: TIME_LABELS[setting] }), input]));
    if (container.dataset.switch) container.append(switchRow(container.dataset.switch));
  }
}

function toast(message, { error = false } = {}) {
  const node = $('#toast');
  node.textContent = message;
  node.classList.toggle('error', error);
  logEntry(error ? 'error' : 'message', message);
  node.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (node.hidden = true), TOAST_MS);
}

/** Run a clock operation, reporting failures instead of throwing them at an event handler. */
async function run(operation) {
  try {
    return await operation();
  } catch (error) {
    console.error(error);
    toast(error.message, { error: true });
    return undefined;
  }
}

function renderConnection() {
  const connected = clock.connected;
  $('#controls').disabled = !connected;
  $('#connect').textContent = connected ? 'Disconnect' : 'Connect';
  $('#device-line').textContent = connected ? `Connected to ${clock.name}` : clock.name ? `${clock.name} · disconnected` : 'Not connected';
  renderTubes();
}

/** Only the tubes this model has: a 4-tube clock has no use for switches 5 and 6. */
function renderTubes() {
  const count = tubeCount(clock.name);
  $('#tubes').style.setProperty('--tubes', count);
  $('#tubes').querySelectorAll('.tube').forEach((tube, index) => (tube.hidden = index >= count));
}

function renderColor(rgb) {
  const swatch = $('#swatch');
  const off = rgb.every((channel) => channel === 0);
  swatch.classList.toggle('off', off);
  if (!off) swatch.style.setProperty('--swatch', rgbToCss(rgb));
  const position = rgbToHue(fullBrightness(rgb));
  if (position !== null) $('#hue').value = String(position);
}

function renderEffect(next) {
  effect = next;
  $('#effect').value = effect ?? '';
}

/** Until the clock reports its settings, a switch's position would be a guess: show it as unknown. */
function renderUnread() {
  $('#unread').hidden = false;
  $('#controls').classList.add('unread');
  for (const input of document.querySelectorAll('[data-toggle]')) input.indeterminate = true;
}

function logEntry(kind, text) {
  const log = $('#log');
  const now = new Date();
  const stamp = `${clockTime(now)}.${String(now.getMilliseconds()).padStart(3, '0')}`;
  log.append(`${stamp} ${LOG_MARKERS[kind]} ${text}\n`);
  while (log.childNodes.length > LOG_LINES) log.firstChild.remove();
  log.scrollTop = log.scrollHeight;
}

/** How the log names a control: its accessible name, else its row's text, else its own. */
function controlName(control) {
  const name = control.getAttribute('aria-label') || control.closest('label')?.textContent || control.textContent;
  return name.replace(/\s+/g, ' ').trim();
}

/** The new value of a changed input, as the log shows it; nothing for the password. */
function controlValue(input) {
  if (input.type === 'checkbox') return input.checked ? 'on' : 'off';
  if (input.type === 'time') return input.value || 'cleared';
  if (input.id === 'hue') return rgbToCss(hueToRgb(Number(input.value)));
  if (input.tagName === 'SELECT') return input.selectedOptions[0]?.textContent ?? null;
  return null;
}

function logAction(control, value = null) {
  logEntry('action', value === null ? controlName(control) : `${controlName(control)}: ${value}`);
}

function render(next) {
  status = next;
  $('#unread').hidden = true;
  $('#controls').classList.remove('unread');
  renderColor(next.rgb);
  for (const input of document.querySelectorAll('[data-toggle]')) {
    input.indeterminate = false;
    input.checked = next.toggles[input.dataset.toggle];
  }
  for (const input of document.querySelectorAll('[data-setting]')) {
    if (document.activeElement !== input) input.value = next.times[input.dataset.setting] ?? '';
  }
  $('#updated').textContent = `Read at ${new Date().toLocaleTimeString()}`;
}

/** No read-back: the clock answers a status request only right after connecting. */
function command(text) {
  return run(() => clock.send(text));
}

/** Slider drags fire many events: send the latest colour only, one write at a time. */
async function sendColor(rgb) {
  pendingColor = rgb;
  if (colorInFlight) return;
  colorInFlight = true;
  try {
    while (pendingColor) {
      const next = pendingColor;
      pendingColor = null;
      await run(() => clock.send(encodeColor(next)));
    }
  } finally {
    colorInFlight = false;
  }
}

/** Run an effect in the slider's colour; if it did not get through, the effect is unknown. */
async function setEffect(next) {
  const rgb = hueToRgb(Number($('#hue').value));
  renderColor(rgb);
  renderEffect(next);
  const sent = await run(async () => {
    await clock.send(...encodeEffect(next, rgb));
    return true;
  });
  if (!sent) renderEffect(null);
}

function currentPassword() {
  const value = $('#password').value.trim();
  validatePassword(value);
  return value;
}

async function toggleConnection() {
  if (clock.connected) {
    disconnectRequested = true;
    clock.disconnect();
    return;
  }
  await openSession((password) => clock.connect(password));
}

/** The clock reports its settings only right after connecting: reading them again reconnects. */
async function readSettings() {
  disconnectRequested = clock.connected;
  await openSession((password) => clock.reconnect(password));
}

/** Connect (with `open`), then show the clock's settings. */
async function openSession(open) {
  let password;
  try {
    password = currentPassword();
  } catch (error) {
    $('.settings').open = true;
    toast(error.message, { error: true });
    return;
  }
  storage.set(PASSWORD_STORAGE_KEY, password);
  $('#connect').disabled = true;
  $('#device-line').textContent = 'Connecting…';
  try {
    await open(password);
    status = null;
    renderUnread();
    renderConnection();
    toast(`Connected to ${clock.name}`);
    await clock.requestStatus().catch((error) => {
      toast(`Connected, but the clock did not send its settings (${error.message})`, { error: true });
    });
  } catch (error) {
    if (error.name === 'NotFoundError') {
      toast('No clock chosen. Close the original app or any other phone connected to the clock.');
    } else if (error.name === 'SecurityError') {
      // The chooser needs a fresh tap when reconnecting took too long to open it from the first.
      toast('Tap Connect again to choose the clock.');
    } else {
      if (error instanceof ClockAuthError) $('.settings').open = true;
      toast(error.message, { error: true });
    }
  } finally {
    $('#connect').disabled = false;
    renderConnection();
  }
}

function syncTimeIfEnabled() {
  if (clock.connected && status?.toggles.time_sync) run(() => clock.send(encodeTimeSync(new Date())));
}

function tickPhoneClock() {
  const now = new Date();
  $('#phone-time').textContent = clockTime(now);
}

function wireEvents() {
  // Capture phase: the action is logged before the commands it sends, and before a handler
  // relabels the control (Connect becomes Disconnect).
  document.addEventListener('click', (event) => {
    const button = event.target.closest('button');
    if (button) logAction(button);
  }, true);
  document.addEventListener('change', (event) => logAction(event.target, controlValue(event.target)), true);
  $('#connect').addEventListener('click', toggleConnection);
  $('#forget').addEventListener('click', () => {
    clock.forget();
    renderConnection();
  });
  $('#refresh').addEventListener('click', readSettings);
  $('#sync-now').addEventListener('click', async () => {
    await run(() => clock.send(encodeTimeSync(new Date())));
    toast('Clock time set');
  });
  $('#backlight-off').addEventListener('click', () => {
    renderColor(BLACK);
    sendColor(BLACK);
  });
  $('#hue').addEventListener('input', (event) => {
    const rgb = hueToRgb(Number(event.target.value));
    renderColor(rgb);
    sendColor(rgb);
  });
  $('#controls').addEventListener('click', (event) => {
    const key = event.target.closest('[data-key]')?.dataset.key;
    if (!key) return;
    command(encodeKey(key));
    renderEffect(effectAfterKey(effect, key));
  });
  $('#effect').addEventListener('change', (event) => setEffect(event.target.value));
  $('#controls').addEventListener('change', (event) => {
    const { toggle, setting } = event.target.dataset;
    if (toggle) {
      // Assume the clock took it: there is no reading it back without reconnecting.
      if (status) status.toggles[toggle] = event.target.checked;
      command(encodeToggle(toggle, event.target.checked));
      if (toggle === 'time_sync' && event.target.checked) run(() => clock.send(encodeTimeSync(new Date())));
    } else if (setting && event.target.value) {
      const [hour, minute] = event.target.value.split(':').map(Number);
      command(encodeTime(setting, hour, minute));
    }
  });
  clock.addEventListener('status', (event) => render(event.detail));
  clock.addEventListener('traffic', ({ detail }) => logEntry(detail.direction, detail.text));
  $('#clear-log').addEventListener('click', () => ($('#log').textContent = ''));
  $('#copy-log').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText($('#log').textContent);
      toast('Log copied');
    } catch (error) {
      toast(`Could not copy the log: ${error.message}`, { error: true });
    }
  });
  clock.addEventListener('disconnected', () => {
    renderConnection();
    if (!disconnectRequested) toast('The clock disconnected');
    disconnectRequested = false;
  });
}

function start() {
  buildControls();
  $('#password').value = storage.get(PASSWORD_STORAGE_KEY) ?? DEFAULT_PASSWORD;
  if (!Clock.supported) {
    $('#unsupported').hidden = false;
    $('#connect').disabled = true;
  }
  wireEvents();
  renderConnection();
  tickPhoneClock();
  setInterval(tickPhoneClock, 1000);
  setInterval(syncTimeIfEnabled, TIME_SYNC_INTERVAL_MS);
  if ('serviceWorker' in navigator) {
    navigator.serviceWorker.register('sw.js').catch((error) => console.warn('No offline support:', error));
  }
}

start();

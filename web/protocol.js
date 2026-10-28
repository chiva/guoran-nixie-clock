// Wire protocol of the Guoran nixie clock, decoded from the original app (see docs/PROTOCOL.md).
// Pure functions, no Bluetooth: ble.js moves the bytes. Mirrors
// custom_components/guoran_nixie/protocol.py; both are tested against
// docs/fixtures/protocol-vectors.json.

export const NAME_PREFIX = 'XGGF';

const uuid16 = (short) => `0000${short.toString(16).padStart(4, '0')}-0000-1000-8000-00805f9b34fb`;

export const UART_RX_SERVICE = uuid16(0xffe0);
export const UART_RX_CHAR = uuid16(0xffe4);
export const UART_TX_SERVICE = uuid16(0xffe5);
export const UART_TX_CHAR = uuid16(0xffe9);
export const AUTH_SERVICE = uuid16(0xffc0);
export const AUTH_WRITE_CHAR = uuid16(0xffc1);
export const AUTH_NOTIFY_CHAR = uuid16(0xffc2);
/** Without these there is no session; the password service is optional. */
export const REQUIRED_SERVICES = Object.freeze([UART_RX_SERVICE, UART_TX_SERVICE]);
/** Also on an IN1204M (2023, "XGGF-IN1204231115.1"): FFF0 (module GPIO), FF90 and FD00 (unknown).
 * Unused, but declared so a failed connection can report everything the clock offered. */
const OBSERVED_SERVICES = Object.freeze([uuid16(0xfff0), uuid16(0xff90), uuid16(0xfd00)]);
export const OPTIONAL_SERVICES = Object.freeze([...REQUIRED_SERVICES, AUTH_SERVICE, ...OBSERVED_SERVICES]);

/** "0000ffe0-0000-1000-8000-00805f9b34fb" → "FFE0"; other UUIDs unchanged. */
export const shortUuid = (uuid) =>
  /^0000([0-9a-f]{4})-0000-1000-8000-00805f9b34fb$/i.exec(uuid)?.[1].toUpperCase() ?? uuid;

/** The original app changes the Bluetooth module's password to this on first connection. */
export const DEFAULT_PASSWORD = '210709';
/** The Bluetooth module's password before any app has changed it. */
export const FACTORY_PASSWORD = '000000';
export const PASSWORD_LENGTH = 6;

export const STATUS_REQUEST = 'OSC';
export const STATUS_PREFIX = 'R';
export const STATUS_TERMINATOR = 'CSS';
export const STATUS_LENGTH = 93;

export const CHANNEL_MAX = 255;
export const HUE_SEGMENT = CHANNEL_MAX;
/** The original colour slider runs 0…1530 around the hue wheel: six segments of 255 steps. */
export const HUE_POSITIONS = 6 * HUE_SEGMENT;
export const BLACK = Object.freeze([0, 0, 0]);
/** Default MTU (23) minus the ATT header; Web Bluetooth cannot read the negotiated MTU. */
export const ATT_PAYLOAD = 20;

export class ProtocolError extends Error {
  name = 'ProtocolError';
}

export class StatusFrameError extends ProtocolError {
  name = 'StatusFrameError';
}

export class AuthResultError extends ProtocolError {
  name = 'AuthResultError';
}

/** On/off settings, in the order the status frame reports them. */
export const TOGGLES = Object.freeze([
  'tube_1', 'tube_2', 'tube_3', 'tube_4', 'tube_5', 'tube_6',
  'time_sync', 'alarm_1', 'alarm_2', 'schedule',
  'auto_brightness', 'hour_12', 'ambient_light', 'hourly_chime', 'english_voice', 'sensor_switch',
]);

/** The character after "S" in each toggle command. */
export const TOGGLE_IDS = Object.freeze(
  Object.fromEntries(TOGGLES.map((toggle, index) => [toggle, '1234567890ABCDEF'[index]])),
);

/** The IR remote's buttons; previous/mode/next step the backlight colour and mode (inferred). */
export const KEYS = Object.freeze(['previous', 'mode', 'next', 'power', 'back', 'plus', 'set', 'ok', 'minus']);

export const KEY_CODES = Object.freeze(
  Object.fromEntries(KEYS.map((key, index) => [key, `K${String(index + 1).padStart(2, '0')}`])),
);

/** Times the clock stores, in the order the status frame reports them. */
export const TIME_SETTINGS = Object.freeze(['alarm_1', 'alarm_2', 'power_on', 'power_off']);

/** [prefix, suffix] around HH:MM in each time command. */
export const TIME_FORMATS = Object.freeze({
  alarm_1: ['A', 'A'],
  alarm_2: ['A', 'B'],
  power_on: ['T', 'O'],
  power_off: ['T', 'C'],
});

/** Indexed by the single byte the password characteristic notifies. */
export const AUTH_RESULTS = Object.freeze(['ok', 'wrong_password', 'password_changed', 'password_removed']);
export const AUTH_ACCEPTED = Object.freeze(new Set(['ok', 'password_changed']));

const pad = (value, width = 2) => String(value).padStart(width, '0');

function requireMember(collection, value, what) {
  if (!collection.includes(value)) throw new RangeError(`unknown ${what}: ${value}`);
}

/** The clock lights green from the "R" field and red from the "G" field (observed on an
 * IN1204M; the original app sent them unswapped, so its slider showed the wrong colours too).
 * Swapping is its own inverse: it turns a colour into the fields, and the fields into a colour. */
const swapRedGreen = ([first, second, third]) => [second, first, third];

/** Red [255, 0, 0] → "R000-G255-B000": see swapRedGreen. */
export function encodeColor(rgb) {
  for (const channel of rgb) {
    if (!Number.isInteger(channel) || channel < 0 || channel > CHANNEL_MAX) {
      throw new RangeError(`colour channels must be 0…${CHANNEL_MAX}: ${rgb}`);
    }
  }
  const [r, g, b] = swapRedGreen(rgb).map((channel) => pad(channel, 3));
  return `R${r}-G${g}-B${b}`;
}

export function encodeToggle(toggle, on) {
  requireMember(TOGGLES, toggle, 'toggle');
  return `S${TOGGLE_IDS[toggle]}${on ? 1 : 0}`;
}

export function encodeKey(key) {
  requireMember(KEYS, key, 'key');
  return KEY_CODES[key];
}

/** Alarm 1 at 07:05 → "A07:05A"; power-off at 18:30 → "T18:30C". */
export function encodeTime(setting, hour, minute) {
  requireMember(TIME_SETTINGS, setting, 'time setting');
  const valid = (value, max) => Number.isInteger(value) && value >= 0 && value <= max;
  if (!valid(hour, 23) || !valid(minute, 59)) throw new RangeError(`not a time: ${hour}:${minute}`);
  const [prefix, suffix] = TIME_FORMATS[setting];
  return `${prefix}${pad(hour)}:${pad(minute)}${suffix}`;
}

/** "$YYYY-MM-DD;HH:MM:SS;0W" in local time, W the weekday with Sunday as 0. */
export function encodeTimeSync(date) {
  const day = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}`;
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  return `$${day};${clock};0${date.getDay()}`;
}

export function validatePassword(password) {
  if (typeof password !== 'string' || !new RegExp(`^[0-9]{${PASSWORD_LENGTH}}$`).test(password)) {
    throw new RangeError(`the password must be ${PASSWORD_LENGTH} digits`);
  }
}

/** The password write: the current password, then the one to keep (the same to just log in). */
export function encodeAuth(oldPassword, newPassword) {
  validatePassword(oldPassword);
  validatePassword(newPassword);
  return oldPassword + newPassword;
}

export function parseAuthResult(bytes) {
  if (bytes.length !== 1) throw new AuthResultError(`expected one byte, got ${bytes.length}`);
  const result = AUTH_RESULTS[bytes[0]];
  if (result === undefined) throw new AuthResultError(`unknown password result ${bytes[0]}`);
  return result;
}

/** A position on the original 0…1530 colour slider → the fully saturated colour it sends. */
export function hueToRgb(position) {
  if (!Number.isInteger(position) || position < 0 || position > HUE_POSITIONS) {
    throw new RangeError(`hue position must be 0…${HUE_POSITIONS}: ${position}`);
  }
  const segment = Math.min(Math.floor(position / HUE_SEGMENT), 5);
  const offset = position - segment * HUE_SEGMENT;
  const falling = CHANNEL_MAX - offset;
  return [
    [CHANNEL_MAX, offset, 0],
    [falling, CHANNEL_MAX, 0],
    [0, CHANNEL_MAX, offset],
    [0, falling, CHANNEL_MAX],
    [offset, 0, CHANNEL_MAX],
    [CHANNEL_MAX, 0, falling],
  ][segment];
}

/** The slider position of a colour on the hue wheel, or null for any other colour. */
export function rgbToHue([red, green, blue]) {
  const max = CHANNEL_MAX;
  if (red === max && blue === 0) return green;
  if (green === max && blue === 0) return HUE_SEGMENT + (max - red);
  if (red === 0 && green === max) return 2 * HUE_SEGMENT + blue;
  if (red === 0 && blue === max) return 3 * HUE_SEGMENT + (max - green);
  if (green === 0 && blue === max) return 4 * HUE_SEGMENT + red;
  if (red === max && green === 0) return 5 * HUE_SEGMENT + (max - blue);
  return null;
}

export const rgbToCss = ([red, green, blue]) => `rgb(${red} ${green} ${blue})`;

const COLOR_FIELD = /^R(\d{3})-G(\d{3})-B(\d{3})$/;
const TIME_FIELD = /^[AT](\d{2}):(\d{2})[ABOC]$/;
const COLOR_LENGTH = 14;
const TOGGLE_LENGTH = 3;
const TIME_LENGTH = 7;
const TIMES_START = COLOR_LENGTH + TOGGLES.length * TOGGLE_LENGTH;

/**
 * Decode a complete status frame: the clock's settings written as the commands that would set
 * them (R…-G…-B…, sixteen S<id><0|1>, four times) and closed by "CSS". Times come back "HH:MM".
 */
export function parseStatus(frame) {
  if (
    frame.length !== STATUS_LENGTH ||
    !frame.startsWith(STATUS_PREFIX) ||
    !frame.endsWith(STATUS_TERMINATOR)
  ) {
    throw new StatusFrameError(`not a status frame: ${JSON.stringify(frame)}`);
  }
  const color = COLOR_FIELD.exec(frame.slice(0, COLOR_LENGTH));
  const fields = color ? color.slice(1).map(Number) : [];
  if (fields.length !== 3 || fields.some((channel) => channel > CHANNEL_MAX)) {
    throw new StatusFrameError(`bad colour field: ${frame.slice(0, COLOR_LENGTH)}`);
  }
  const rgb = swapRedGreen(fields);
  const toggles = {};
  TOGGLES.forEach((toggle, index) => {
    // As the original app: the third character, "0" for off and anything else for on.
    toggles[toggle] = frame[COLOR_LENGTH + index * TOGGLE_LENGTH + 2] !== '0';
  });
  const times = {};
  TIME_SETTINGS.forEach((setting, index) => {
    const start = TIMES_START + index * TIME_LENGTH;
    const field = frame.slice(start, start + TIME_LENGTH);
    const match = TIME_FIELD.exec(field);
    if (!match || Number(match[1]) > 23 || Number(match[2]) > 59) {
      throw new StatusFrameError(`bad ${setting} field: ${field}`);
    }
    times[setting] = `${match[1]}:${match[2]}`;
  });
  return { rgb, toggles, times };
}

/** Split a write to fit one ATT packet: a 23-character time sync exceeds the default 20. */
export function chunked(bytes, size = ATT_PAYLOAD) {
  const chunks = [];
  for (let start = 0; start < bytes.length; start += size) chunks.push(bytes.slice(start, start + size));
  return chunks;
}

/**
 * Rebuilds status frames from notifications. A frame is 93 bytes, more than one notification
 * carries at the default MTU, so it arrives in pieces. Bytes before a frame's "R", and any 93
 * bytes from an "R" that do not end in "CSS", are dropped so one corrupted piece cannot wedge
 * the stream.
 */
export class StatusAssembler {
  #buffer = '';
  #decoder = new TextDecoder('ascii');

  /** Add one notification (bytes or text); return the frames it completes. */
  feed(chunk) {
    this.#buffer += typeof chunk === 'string' ? chunk : this.#decoder.decode(chunk);
    const frames = [];
    let start;
    while ((start = this.#buffer.indexOf(STATUS_PREFIX)) !== -1) {
      this.#buffer = this.#buffer.slice(start);
      if (this.#buffer.length < STATUS_LENGTH) return frames;
      const candidate = this.#buffer.slice(0, STATUS_LENGTH);
      if (candidate.endsWith(STATUS_TERMINATOR)) {
        frames.push(candidate);
        this.#buffer = this.#buffer.slice(STATUS_LENGTH);
      } else {
        this.#buffer = this.#buffer.slice(1);
      }
    }
    this.#buffer = '';
    return frames;
  }

  reset() {
    this.#buffer = '';
  }
}

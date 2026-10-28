// A stand-in for navigator.bluetooth and the clock's Bluetooth module, as Web Bluetooth sees it.
// Mirrors tests/fake_clock.py: a password service answering one byte, an undelimited UART stream
// (so chunked writes must reassemble), and status frames sent back in 20-byte notifications.

import {
  ATT_PAYLOAD,
  AUTH_NOTIFY_CHAR,
  AUTH_SERVICE,
  AUTH_WRITE_CHAR,
  DEFAULT_PASSWORD,
  PASSWORD_LENGTH,
  STATUS_REQUEST,
  STATUS_TERMINATOR,
  TIME_FORMATS,
  TIME_SETTINGS,
  TOGGLES,
  UART_RX_CHAR,
  UART_RX_SERVICE,
  UART_TX_CHAR,
  UART_TX_SERVICE,
  encodeColor,
  encodeTime,
  encodeToggle,
} from '../protocol.js';

const uuid16 = (short) => `0000${short.toString(16).padStart(4, '0')}-0000-1000-8000-00805f9b34fb`;
/** The services of a real IN1204M (chrome://bluetooth-internals), in the order it lists them. */
const DISCOVERY_ORDER = [
  uuid16(0xfff0),
  uuid16(0xff90),
  UART_TX_SERVICE,
  uuid16(0xfd00),
  UART_RX_SERVICE,
  AUTH_SERVICE,
  uuid16(0x180a),
];
/** While discovery lags, FFF0, FF90 and FFE5 are known but not FFE0. */
const LAGGING_DISCOVERY = 3;

const COMMAND = /^(R\d{3}-G\d{3}-B\d{3}|S[0-9A-F][01]|K0[1-9]|[AT]\d{2}:\d{2}[ABOC]|\$\d{4}-\d{2}-\d{2};\d{2}:\d{2}:\d{2};0[0-6]|OSC)/;

class FakeCharacteristic extends EventTarget {
  constructor(clock, uuid, properties) {
    super();
    Object.assign(this, { clock, uuid, properties, value: null });
  }

  async startNotifications() {
    this.clock.subscribed.add(this.uuid);
  }

  writeValueWithResponse(chunk) {
    return this.clock.receive(this, chunk, true);
  }

  writeValueWithoutResponse(chunk) {
    return this.clock.receive(this, chunk, false);
  }

  notify(bytes) {
    this.value = new DataView(Uint8Array.from(bytes).buffer);
    this.dispatchEvent(new Event('characteristicvaluechanged'));
  }
}

export class FakeClock extends EventTarget {
  name = 'XGGF-1234';
  password = DEFAULT_PASSWORD;
  hasAuthService = true;
  rgb = [0, 255, 255];
  toggles = Object.fromEntries(TOGGLES.map((toggle) => [toggle, false]));
  times = Object.fromEntries(TIME_SETTINGS.map((setting) => [setting, '07:00']));
  ignoreStatusRequests = 0;
  garbleStatusReplies = 0;
  commands = [];
  authWrites = [];
  writes = [];
  connections = 0;
  chooserShown = 0;
  subscribed = new Set();
  /** Service lists that miss the UART services at the start of each connection, as the real
   * module's slow discovery did. */
  discoveryLagPolls = 0;
  /** Connections, from the first, whose discovery never completes. */
  brokenDiscoveryConnections = 0;
  /** Connections, from the first, that the clock drops before any service is discovered, as a
   * Pixel 9 saw: no services at all, then a disconnect about 20 s in. */
  droppedDiscoveryConnections = 0;
  /** As Chrome on macOS after a disconnect: the device left Chrome's cache, so connecting fails
   * until the chooser scans for it again. */
  outOfRange = false;
  discoveryPolls = 0;
  #polls = 0;
  #uart = '';

  constructor() {
    super();
    this.characteristics = {
      [UART_TX_CHAR]: new FakeCharacteristic(this, UART_TX_CHAR, { writeWithoutResponse: true }),
      [UART_RX_CHAR]: new FakeCharacteristic(this, UART_RX_CHAR, { notify: true }),
      [AUTH_WRITE_CHAR]: new FakeCharacteristic(this, AUTH_WRITE_CHAR, { write: true }),
      [AUTH_NOTIFY_CHAR]: new FakeCharacteristic(this, AUTH_NOTIFY_CHAR, { notify: true }),
    };
    const clock = this;
    this.gatt = {
      connected: false,
      async connect() {
        if (clock.outOfRange) throw new DOMException('Bluetooth Device is no longer in range.', 'NetworkError');
        clock.connections += 1;
        clock.gatt.connected = true;
        clock.#uart = '';
        clock.#polls = 0;
        return clock.gatt;
      },
      disconnect() {
        if (!clock.gatt.connected) return;
        clock.gatt.connected = false;
        clock.dispatchEvent(new Event('gattserverdisconnected'));
      },
      /** As Chrome: only the services the page declared, in discovery order; NotFoundError if
       * none. While discovery lags, only the first services the real clock lists are known. */
      async getPrimaryServices() {
        clock.discoveryPolls += 1;
        clock.#polls += 1;
        if (clock.connections <= clock.droppedDiscoveryConnections) {
          clock.gatt.disconnect();
          throw new DOMException('GATT Server is disconnected', 'NetworkError');
        }
        const lagging = clock.connections <= clock.brokenDiscoveryConnections || clock.#polls <= clock.discoveryLagPolls;
        const discovered = lagging ? DISCOVERY_ORDER.slice(0, LAGGING_DISCOVERY) : DISCOVERY_ORDER;
        const allowed = new Set(clock.lastRequest?.optionalServices ?? []);
        const visible = discovered.filter(
          (uuid) => allowed.has(uuid) && (uuid !== AUTH_SERVICE || clock.hasAuthService),
        );
        if (!visible.length) throw new DOMException('No Services found in device.', 'NotFoundError');
        return visible.map((uuid) => ({
          uuid,
          getCharacteristic: async (charUuid) => clock.characteristics[charUuid],
        }));
      },
    };
  }

  /** navigator.bluetooth */
  get bluetooth() {
    return {
      requestDevice: async (options) => {
        this.chooserShown += 1;
        this.lastRequest = options;
        this.outOfRange = false;
        return this;
      },
    };
  }

  statusFrame() {
    return (
      encodeColor(this.rgb) +
      TOGGLES.map((toggle) => encodeToggle(toggle, this.toggles[toggle])).join('') +
      TIME_SETTINGS.map((setting) => {
        const [h, m] = this.times[setting].split(':').map(Number);
        return encodeTime(setting, h, m);
      }).join('') +
      STATUS_TERMINATOR
    );
  }

  async receive(characteristic, chunk, withResponse) {
    if (!this.gatt.connected) throw new DOMException('GATT Server is disconnected', 'NetworkError');
    if (chunk.length > ATT_PAYLOAD) throw new DOMException('value too long', 'InvalidModificationError');
    const text = new TextDecoder().decode(chunk);
    this.writes.push({ uuid: characteristic.uuid, text, withResponse });
    if (characteristic.uuid === AUTH_WRITE_CHAR) this.#onAuth(text);
    if (characteristic.uuid === UART_TX_CHAR) {
      this.#uart += text;
      let match;
      while ((match = COMMAND.exec(this.#uart))) {
        this.#uart = this.#uart.slice(match[0].length);
        this.#apply(match[0]);
      }
    }
  }

  #onAuth(payload) {
    this.authWrites.push(payload);
    const [old, next] = [payload.slice(0, PASSWORD_LENGTH), payload.slice(PASSWORD_LENGTH)];
    let result = 0;
    if (old !== this.password) result = 1;
    else if (next !== old) [this.password, result] = [next, 2];
    queueMicrotask(() => this.characteristics[AUTH_NOTIFY_CHAR].notify([result]));
  }

  #apply(command) {
    this.commands.push(command);
    if (command === STATUS_REQUEST) this.#answerStatus();
    else if (command.startsWith('R')) {
      // The colour the LEDs show: the clock lights green from "R" and red from "G".
      const [rField, gField, bField] = command.match(/\d{3}/g).map(Number);
      this.rgb = [gField, rField, bField];
    }
    else if (command.startsWith('S')) {
      this.toggles[TOGGLES['1234567890ABCDEF'.indexOf(command[1])]] = command[2] === '1';
    } else if (command.startsWith('A') || command.startsWith('T')) {
      const affixes = `${command[0]}${command.at(-1)}`;
      const setting = Object.keys(TIME_FORMATS).find((key) => TIME_FORMATS[key].join('') === affixes);
      this.times[setting] = command.slice(1, 6);
    }
  }

  #answerStatus() {
    if (this.ignoreStatusRequests > 0) {
      this.ignoreStatusRequests -= 1;
      return;
    }
    let frame = this.statusFrame();
    if (this.garbleStatusReplies > 0) {
      this.garbleStatusReplies -= 1;
      frame = `R999${frame.slice(4)}`;
    }
    const bytes = new TextEncoder().encode(frame);
    for (let start = 0; start < bytes.length; start += ATT_PAYLOAD) {
      const piece = bytes.slice(start, start + ATT_PAYLOAD);
      setTimeout(() => this.characteristics[UART_RX_CHAR].notify(piece), 0);
    }
  }
}

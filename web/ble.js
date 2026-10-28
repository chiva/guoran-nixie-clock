// Web Bluetooth session with the clock: pick it, log in, send commands, read the status.
// Same behaviour as custom_components/guoran_nixie/client.py, for Chrome on Android or desktop.

import {
  AUTH_ACCEPTED,
  AUTH_NOTIFY_CHAR,
  AUTH_SERVICE,
  AUTH_WRITE_CHAR,
  FACTORY_PASSWORD,
  NAME_PREFIX,
  OPTIONAL_SERVICES,
  REQUIRED_SERVICES,
  STATUS_REQUEST,
  StatusAssembler,
  StatusFrameError,
  UART_RX_CHAR,
  UART_RX_SERVICE,
  UART_TX_CHAR,
  UART_TX_SERVICE,
  chunked,
  encodeAuth,
  parseAuthResult,
  parseStatus,
  shortUuid,
} from './protocol.js';

const STATUS_ATTEMPTS = 2;
/** A connection whose services are still missing after polling is dropped and opened once more. */
const CONNECTION_ATTEMPTS = 2;
const MS_PER_S = 1000;
export const DEFAULT_TIMING = Object.freeze({
  authTimeoutMs: 5000,
  statusTimeoutMs: 5000,
  /** The module forwards bytes to the clock's serial port without delimiters: commands sent
   * back to back could merge into one the clock cannot parse. */
  commandGapMs: 150,
  /** The module may finish service discovery late: the original app asked every 500 ms until the
   * services appeared (and an older version waited 3 s), so this does the same, up to 5 s. */
  discoveryPollMs: 500,
  discoveryPolls: 10,
});

export class ClockConnectionError extends Error {
  name = 'ClockConnectionError';
}

export class ClockAuthError extends Error {
  name = 'ClockAuthError';
}

/** The clock never offered the UART services; `offered` lists the short UUIDs it did offer. */
export class ClockDiscoveryError extends ClockConnectionError {
  name = 'ClockDiscoveryError';

  constructor(message, offered) {
    super(message);
    this.offered = offered;
  }
}

/** Chrome no longer knows the remembered clock: only its device chooser can find it again. */
export class ClockOutOfRangeError extends ClockConnectionError {
  name = 'ClockOutOfRangeError';
}

const encoder = new TextEncoder();
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function withTimeout(promise, ms, message) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new ClockConnectionError(message)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/** Resolves with the next value a characteristic notifies. */
function nextNotification(characteristic) {
  return new Promise((resolve) => {
    characteristic.addEventListener(
      'characteristicvaluechanged',
      (event) => resolve(new Uint8Array(event.target.value.buffer)),
      { once: true },
    );
  });
}

const CHARACTERISTIC_PROPERTIES = ['read', 'write', 'writeWithoutResponse', 'notify', 'indicate'];

const PRINTABLE = /^[\x20-\x7e]$/;
const hexByte = (byte) => `\\x${byte.toString(16).padStart(2, '0')}`;

/** Bytes as text for the Bluetooth log: printable ASCII as is, anything else as \xNN. */
export function describeBytes(bytes) {
  return Array.from(bytes, (byte) => (PRINTABLE.test(String.fromCharCode(byte)) ? String.fromCharCode(byte) : hexByte(byte))).join('');
}

/**
 * Events: "status" (detail: parsed status), "disconnected", and "traffic" (detail: { direction:
 * "sent" | "received" | "info", text }) for every command, notification and login step, and what
 * the connection allows.
 * Every status frame the clock sends is announced, requested or not.
 */
export class Clock extends EventTarget {
  #device = null;
  #watched = new WeakSet();
  #tx = null;
  #assembler = new StatusAssembler();
  #queue = Promise.resolve();
  #lastCommandAt = 0;
  #statusWaiters = new Set();
  #timing;

  constructor(timing = {}) {
    super();
    this.#timing = { ...DEFAULT_TIMING, ...timing };
  }

  static get supported() {
    return Boolean(globalThis.navigator?.bluetooth);
  }

  get name() {
    return this.#device?.name ?? null;
  }

  get connected() {
    return Boolean(this.#device?.gatt.connected && this.#tx);
  }

  /** Shows Chrome's device chooser the first time; reconnects to the same clock afterwards. Chrome
   * forgets a device it has not seen advertising lately ("no longer in range", as on macOS after a
   * disconnect), and only its chooser scans again: then the chooser opens once more. */
  async connect(password) {
    if (this.#device) {
      try {
        return await this.#open(password);
      } catch (error) {
        if (!(error instanceof ClockOutOfRangeError)) throw error;
        this.#device = null;
      }
    }
    this.#device = await navigator.bluetooth.requestDevice({
      filters: [{ namePrefix: NAME_PREFIX }],
      optionalServices: OPTIONAL_SERVICES,
    });
    // Chrome may hand back the same device object when the same clock is chosen again.
    if (!this.#watched.has(this.#device)) {
      this.#watched.add(this.#device);
      this.#device.addEventListener('gattserverdisconnected', () => this.#onDisconnected());
    }
    return this.#open(password);
  }

  async #open(password) {
    try {
      const services = await this.#connectWithServices();
      await this.#authenticate(services.get(AUTH_SERVICE), password);
      const rx = await services.get(UART_RX_SERVICE).getCharacteristic(UART_RX_CHAR);
      // Chrome may hand back the same characteristic object on a reconnect: listen once.
      if (!this.#watched.has(rx)) {
        this.#watched.add(rx);
        rx.addEventListener('characteristicvaluechanged', (event) => {
          this.#onUart(new Uint8Array(event.target.value.buffer));
        });
      }
      await rx.startNotifications();
      this.#tx = await services.get(UART_TX_SERVICE).getCharacteristic(UART_TX_CHAR);
      const allowed = CHARACTERISTIC_PROPERTIES.filter((property) => this.#tx.properties[property]);
      const mode = this.#tx.properties.write ? 'with' : 'without';
      this.#traffic('info', `command characteristic allows: ${allowed.join(', ')}; writing ${mode} response`);
    } catch (error) {
      this.#device.gatt.disconnect();
      if (error instanceof ClockAuthError || error instanceof ClockConnectionError) throw error;
      throw new ClockConnectionError(`connecting to ${this.name}: ${error.message}`);
    }
  }

  async #gattConnect(connection) {
    try {
      return await this.#device.gatt.connect();
    } catch (error) {
      // Chrome reports a device dropped from its cache this way, before any radio traffic.
      if (connection === 1 && error.name === 'NetworkError') throw new ClockOutOfRangeError(`${this.name}: ${error.message}`);
      throw error;
    }
  }

  /** Connect and wait for service discovery to include the UART services; reconnect once if it
   * never does. Resolves with the services by UUID. */
  async #connectWithServices() {
    let offered = [];
    let droppedAfterMs = null;
    for (let connection = 1; connection <= CONNECTION_ATTEMPTS; connection += 1) {
      const server = await this.#gattConnect(connection);
      const connectedAt = performance.now();
      droppedAfterMs = null;
      for (let poll = 1; poll <= this.#timing.discoveryPolls; poll += 1) {
        const services = new Map((await server.getPrimaryServices().catch(() => [])).map((s) => [s.uuid, s]));
        if (REQUIRED_SERVICES.every((uuid) => services.has(uuid))) return services;
        // A list read after the clock dropped the link is empty: keep what it offered before.
        if (services.size) offered = [...services.keys()];
        if (!this.#device.gatt.connected) {
          droppedAfterMs = performance.now() - connectedAt;
          break;
        }
        await sleep(this.#timing.discoveryPollMs);
      }
      this.#device.gatt.disconnect();
    }
    const shortOffered = offered.map(shortUuid);
    const found = shortOffered.length ? shortOffered.join(', ') : 'none';
    const needed = REQUIRED_SERVICES.map(shortUuid).join(' and ');
    const failure =
      droppedAfterMs === null
        ? `did not offer services ${needed}`
        : `dropped the connection ${Math.round(droppedAfterMs / MS_PER_S)} s after connecting, before offering services ${needed}`;
    throw new ClockDiscoveryError(
      `${this.name} ${failure} (offered: ${found}). ` +
        'Close any other app connected to the clock, unplug it for 10 s and try again.',
      shortOffered,
    );
  }

  /** Drop the connection and open a new one. The clock answers a status request only right after
   * connecting (observed on an IN1204M), so this is how its settings are read again. */
  async reconnect(password) {
    if (this.connected) {
      const closed = new Promise((resolve) => this.addEventListener('disconnected', resolve, { once: true }));
      this.disconnect();
      await closed;
    }
    return this.connect(password);
  }

  disconnect() {
    this.#device?.gatt.disconnect();
  }

  /** Forget the chosen clock, so the next connect shows the chooser again. */
  forget() {
    this.disconnect();
    this.#device = null;
  }

  /** Send commands in order, spaced so the clock reads each one on its own. */
  send(...commands) {
    const run = this.#queue.then(async () => {
      for (const command of commands) await this.#write(command);
    });
    this.#queue = run.catch(() => {});
    return run;
  }

  /** Ask for the status frame; asks again once if it goes missing or arrives garbled. */
  async requestStatus() {
    let lastError = new ClockConnectionError('no status frame from the clock');
    for (let attempt = 1; attempt <= STATUS_ATTEMPTS; attempt += 1) {
      const frame = this.#nextFrame();
      this.#assembler.reset();
      await this.send(STATUS_REQUEST);
      try {
        return parseStatus(await withTimeout(frame.promise, this.#timing.statusTimeoutMs, 'no status frame from the clock'));
      } catch (error) {
        if (!(error instanceof StatusFrameError || error instanceof ClockConnectionError)) throw error;
        lastError = error;
      } finally {
        this.#statusWaiters.delete(frame);
      }
    }
    throw lastError;
  }

  /** Log in to the module's anti-hijack service, when it has one (`service` from the settled
   * discovery, so a late discovery cannot pass for a module without a password). The configured
   * password first; if refused, the module may still have its factory password, so change that. */
  async #authenticate(service, password) {
    if (!service) return;
    const write = await service.getCharacteristic(AUTH_WRITE_CHAR);
    const notify = await service.getCharacteristic(AUTH_NOTIFY_CHAR);
    await notify.startNotifications();
    let result = 'wrong_password';
    for (const current of new Set([password, FACTORY_PASSWORD])) {
      const answer = nextNotification(notify);
      await this.#writeChunks(write, encodeAuth(current, password));
      this.#traffic('sent', current === password ? 'password login' : 'password login with the factory password');
      const bytes = await withTimeout(answer, this.#timing.authTimeoutMs, 'the clock did not answer the login');
      this.#traffic('received', `password answer ${describeBytes(bytes)}`);
      result = parseAuthResult(bytes);
      if (AUTH_ACCEPTED.has(result)) return;
      if (result !== 'wrong_password') break;
    }
    throw new ClockAuthError(`the clock refused the password (${result})`);
  }

  async #write(command) {
    if (!this.connected) throw new ClockConnectionError('not connected');
    // Timers round to whole milliseconds and may fire early: wait until the gap has really passed.
    let wait;
    while ((wait = this.#lastCommandAt + this.#timing.commandGapMs - performance.now()) > 0) {
      await sleep(Math.ceil(wait));
    }
    try {
      await this.#writeChunks(this.#tx, command);
      this.#traffic('sent', command);
    } catch (error) {
      throw new ClockConnectionError(`sending ${command}: ${error.message}`);
    } finally {
      this.#lastCommandAt = performance.now();
    }
  }

  async #writeChunks(characteristic, text) {
    // Acknowledged whenever the characteristic allows it, as the Home Assistant client does: the
    // clock ignored many commands written without response (2026-10-28).
    const withResponse = Boolean(characteristic.properties.write);
    for (const chunk of chunked(encoder.encode(text))) {
      await (withResponse
        ? characteristic.writeValueWithResponse(chunk)
        : characteristic.writeValueWithoutResponse(chunk));
    }
  }

  #nextFrame() {
    const waiter = {};
    waiter.promise = new Promise((resolve, reject) => Object.assign(waiter, { resolve, reject }));
    this.#statusWaiters.add(waiter);
    return waiter;
  }

  #traffic(direction, text) {
    this.dispatchEvent(new CustomEvent('traffic', { detail: { direction, text } }));
  }

  #onUart(bytes) {
    this.#traffic('received', describeBytes(bytes));
    for (const frame of this.#assembler.feed(bytes)) {
      for (const waiter of this.#statusWaiters) waiter.resolve(frame);
      try {
        this.dispatchEvent(new CustomEvent('status', { detail: parseStatus(frame) }));
      } catch (error) {
        if (!(error instanceof StatusFrameError)) throw error;
      }
    }
  }

  #onDisconnected() {
    this.#tx = null;
    this.#assembler.reset();
    for (const waiter of this.#statusWaiters) waiter.reject(new ClockConnectionError('the clock disconnected'));
    this.dispatchEvent(new Event('disconnected'));
  }
}

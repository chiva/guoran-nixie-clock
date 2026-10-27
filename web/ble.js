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
} from './protocol.js';

const STATUS_ATTEMPTS = 2;
export const DEFAULT_TIMING = Object.freeze({
  authTimeoutMs: 5000,
  statusTimeoutMs: 5000,
  /** The module forwards bytes to the clock's serial port without delimiters: commands sent
   * back to back could merge into one the clock cannot parse. */
  commandGapMs: 150,
});

export class ClockConnectionError extends Error {
  name = 'ClockConnectionError';
}

export class ClockAuthError extends Error {
  name = 'ClockAuthError';
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

/**
 * Events: "status" (detail: parsed status), "disconnected".
 * Every status frame the clock sends is announced, requested or not.
 */
export class Clock extends EventTarget {
  #device = null;
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

  /** Shows Chrome's device chooser the first time; reconnects to the same clock afterwards. */
  async connect(password) {
    if (!this.#device) {
      this.#device = await navigator.bluetooth.requestDevice({
        filters: [{ namePrefix: NAME_PREFIX }],
        optionalServices: OPTIONAL_SERVICES,
      });
      this.#device.addEventListener('gattserverdisconnected', () => this.#onDisconnected());
    }
    try {
      const server = await this.#device.gatt.connect();
      await this.#authenticate(server, password);
      const rx = await (await server.getPrimaryService(UART_RX_SERVICE)).getCharacteristic(UART_RX_CHAR);
      rx.addEventListener('characteristicvaluechanged', (event) => {
        this.#onUart(new Uint8Array(event.target.value.buffer));
      });
      await rx.startNotifications();
      this.#tx = await (await server.getPrimaryService(UART_TX_SERVICE)).getCharacteristic(UART_TX_CHAR);
    } catch (error) {
      this.#device.gatt.disconnect();
      if (error instanceof ClockAuthError || error instanceof ClockConnectionError) throw error;
      throw new ClockConnectionError(`connecting to ${this.name}: ${error.message}`);
    }
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

  /** Log in to the module's anti-hijack service, when it has one. The configured password
   * first; if refused, the module may still have its factory password, so change that. */
  async #authenticate(server, password) {
    let service;
    try {
      service = await server.getPrimaryService(AUTH_SERVICE);
    } catch {
      return; // no password service on this module
    }
    const write = await service.getCharacteristic(AUTH_WRITE_CHAR);
    const notify = await service.getCharacteristic(AUTH_NOTIFY_CHAR);
    await notify.startNotifications();
    let result = 'wrong_password';
    for (const current of new Set([password, FACTORY_PASSWORD])) {
      const answer = nextNotification(notify);
      await this.#writeChunks(write, encodeAuth(current, password));
      result = parseAuthResult(await withTimeout(answer, this.#timing.authTimeoutMs, 'the clock did not answer the login'));
      if (AUTH_ACCEPTED.has(result)) return;
      if (result !== 'wrong_password') break;
    }
    throw new ClockAuthError(`the clock refused the password (${result})`);
  }

  async #write(command) {
    if (!this.connected) throw new ClockConnectionError('not connected');
    const wait = this.#lastCommandAt + this.#timing.commandGapMs - performance.now();
    if (wait > 0) await sleep(wait);
    try {
      await this.#writeChunks(this.#tx, command);
    } catch (error) {
      throw new ClockConnectionError(`sending ${command}: ${error.message}`);
    } finally {
      this.#lastCommandAt = performance.now();
    }
  }

  async #writeChunks(characteristic, text) {
    const withResponse = !characteristic.properties.writeWithoutResponse;
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

  #onUart(bytes) {
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

// The Web Bluetooth session against the fake module: chooser, login, framing, retries.
import assert from 'node:assert/strict';
import { afterEach, beforeEach, describe, it } from 'node:test';

import { Clock, ClockAuthError, ClockConnectionError } from '../ble.js';
import { FACTORY_PASSWORD, NAME_PREFIX, STATUS_REQUEST, StatusFrameError, UART_TX_CHAR, encodeTime, encodeTimeSync } from '../protocol.js';
import { FakeClock } from './fake-bluetooth.mjs';

const PASSWORD = '210709';
const FAST = { authTimeoutMs: 30, statusTimeoutMs: 30, commandGapMs: 0 };

let fake;
let clock;

beforeEach(() => {
  fake = new FakeClock();
  Object.defineProperty(globalThis, 'navigator', { value: { bluetooth: fake.bluetooth }, configurable: true });
  clock = new Clock(FAST);
});

afterEach(() => clock.disconnect());

describe('connect', () => {
  it('asks Chrome for XGGF clocks only, with the services it needs', async () => {
    assert.equal(Clock.supported, true);
    await clock.connect(PASSWORD);
    assert.deepEqual(fake.lastRequest.filters, [{ namePrefix: NAME_PREFIX }]);
    assert.equal(fake.lastRequest.optionalServices.length, 3);
    assert.equal(clock.connected, true);
    assert.equal(clock.name, 'XGGF-1234');
    assert.deepEqual(fake.authWrites, [PASSWORD + PASSWORD]);
  });

  it('changes a factory module to the configured password', async () => {
    fake.password = FACTORY_PASSWORD;
    await clock.connect(PASSWORD);
    assert.deepEqual(fake.authWrites, [PASSWORD + PASSWORD, FACTORY_PASSWORD + PASSWORD]);
    assert.equal(fake.password, PASSWORD);
  });

  it('rejects a wrong password and drops the link', async () => {
    fake.password = '123456';
    await assert.rejects(clock.connect(PASSWORD), ClockAuthError);
    assert.equal(fake.gatt.connected, false);
    assert.equal(clock.connected, false);
  });

  it('skips the login on a module without a password service', async () => {
    fake.hasAuthService = false;
    await clock.connect(PASSWORD);
    assert.deepEqual(fake.authWrites, []);
  });

  it('reconnects to the same clock without the chooser', async () => {
    await clock.connect(PASSWORD);
    fake.gatt.disconnect();
    await clock.connect(PASSWORD);
    assert.equal(fake.chooserShown, 1);
    assert.equal(fake.connections, 2);
    clock.forget();
    await clock.connect(PASSWORD);
    assert.equal(fake.chooserShown, 2);
  });
});

describe('status', () => {
  it('reads and announces the whole frame', async () => {
    fake.toggles.alarm_1 = true;
    fake.times.alarm_1 = '06:45';
    await clock.connect(PASSWORD);
    const announced = new Promise((resolve) => clock.addEventListener('status', (e) => resolve(e.detail), { once: true }));

    const status = await clock.requestStatus();

    assert.deepEqual(status.rgb, [0, 255, 255]);
    assert.equal(status.toggles.alarm_1, true);
    assert.equal(status.times.alarm_1, '06:45');
    assert.deepEqual(await announced, status);
  });

  it('asks again when the reply goes missing', async () => {
    await clock.connect(PASSWORD);
    fake.ignoreStatusRequests = 1;
    await clock.requestStatus();
    assert.deepEqual(fake.commands, [STATUS_REQUEST, STATUS_REQUEST]);
  });

  it('gives up after every attempt goes unanswered', async () => {
    await clock.connect(PASSWORD);
    fake.ignoreStatusRequests = 2;
    await assert.rejects(clock.requestStatus(), ClockConnectionError);
  });

  it('asks again on a garbled frame, and reports it when it persists', async () => {
    await clock.connect(PASSWORD);
    fake.garbleStatusReplies = 1;
    assert.deepEqual((await clock.requestStatus()).rgb, [0, 255, 255]);
    fake.garbleStatusReplies = 2;
    await assert.rejects(clock.requestStatus(), StatusFrameError);
  });

  it('fails fast when the clock disconnects mid-request', async () => {
    await clock.connect(PASSWORD);
    fake.ignoreStatusRequests = 1;
    const lost = new Promise((resolve) => clock.addEventListener('disconnected', resolve, { once: true }));
    const request = clock.requestStatus();
    setTimeout(() => fake.gatt.disconnect(), 0);
    await assert.rejects(request, /disconnected|not connected/);
    await lost;
    assert.equal(clock.connected, false);
  });
});

describe('commands', () => {
  it('splits long commands into 20-byte writes the clock reassembles', async () => {
    await clock.connect(PASSWORD);
    const sync = encodeTimeSync(new Date('2026-10-25T09:03:07'));
    await clock.send(sync, 'S20');
    const uart = fake.writes.filter((w) => w.uuid === UART_TX_CHAR);
    assert.deepEqual(uart.map((w) => w.text.length), [20, 3, 3]);
    assert.ok(uart.every((w) => w.withResponse === false));
    assert.deepEqual(fake.commands, [sync, 'S20']);
    assert.equal(fake.toggles.tube_2, false);
  });

  it('sets a time the next status reports back', async () => {
    await clock.connect(PASSWORD);
    await clock.send(encodeTime('alarm_2', 5, 15), encodeTime('power_off', 23, 0));
    const status = await clock.requestStatus();
    assert.equal(status.times.alarm_2, '05:15');
    assert.equal(status.times.power_off, '23:00');
  });

  it('keeps commands in order across concurrent calls', async () => {
    await clock.connect(PASSWORD);
    await Promise.all([clock.send('S11'), clock.send('K02'), clock.send('R255-G000-B000')]);
    assert.deepEqual(fake.commands, ['S11', 'K02', 'R255-G000-B000']);
    assert.deepEqual(fake.rgb, [255, 0, 0]);
  });

  it('spaces commands by the configured gap', async () => {
    const spaced = new Clock({ ...FAST, commandGapMs: 40 });
    await spaced.connect(PASSWORD);
    const start = performance.now();
    await spaced.send('K01', 'K02', 'K03');
    // Timers may fire up to ~1 ms early relative to performance.now(): allow that per gap.
    const elapsed = performance.now() - start;
    assert.ok(elapsed >= 2 * 40 - 2, `two gaps between three commands, got ${elapsed} ms`);
    spaced.disconnect();
  });

  it('refuses to send while disconnected, then keeps working after reconnecting', async () => {
    await clock.connect(PASSWORD);
    fake.gatt.disconnect();
    await assert.rejects(clock.send('K01'), ClockConnectionError);
    await clock.connect(PASSWORD);
    await clock.send('K01');
    assert.deepEqual(fake.commands, ['K01']);
  });
});

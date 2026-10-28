// The Web Bluetooth session against the fake module: chooser, login, framing, retries.
import assert from 'node:assert/strict';
import { afterEach, beforeEach, describe, it } from 'node:test';

import { Clock, ClockAuthError, ClockConnectionError, describeBytes } from '../ble.js';
import {
  FACTORY_PASSWORD,
  NAME_PREFIX,
  OPTIONAL_SERVICES,
  STATUS_REQUEST,
  StatusFrameError,
  UART_TX_CHAR,
  encodeTime,
  encodeTimeSync,
  shortUuid,
} from '../protocol.js';
import { FakeClock } from './fake-bluetooth.mjs';

const PASSWORD = '210709';
const FAST = { authTimeoutMs: 30, statusTimeoutMs: 30, commandGapMs: 0, discoveryPollMs: 1, discoveryPolls: 10 };

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
    assert.deepEqual(fake.lastRequest.optionalServices.map(shortUuid), ['FFE0', 'FFE5', 'FFC0', 'FFF0', 'FF90', 'FD00']);
    assert.deepEqual(fake.lastRequest.optionalServices, OPTIONAL_SERVICES);
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

  it('waits for the clock to finish service discovery, and still logs in', async () => {
    fake.discoveryLagPolls = 4;
    await clock.connect(PASSWORD);
    assert.equal(clock.connected, true);
    assert.equal(fake.connections, 1);
    assert.equal(fake.discoveryPolls, 5);
    assert.deepEqual(fake.authWrites, [PASSWORD + PASSWORD], 'a late password service is not skipped');
  });

  it('reconnects once when discovery never completes on the first connection', async () => {
    fake.brokenDiscoveryConnections = 1;
    await clock.connect(PASSWORD);
    assert.equal(clock.connected, true);
    assert.equal(fake.connections, 2);
    assert.equal(fake.discoveryPolls, FAST.discoveryPolls + 1);
  });

  it('reports what the clock offered when the services never appear', async () => {
    fake.brokenDiscoveryConnections = 2;
    await assert.rejects(
      clock.connect(PASSWORD),
      (error) => {
        assert.ok(error instanceof ClockConnectionError);
        assert.match(error.message, /did not offer services FFE0 and FFE5 \(offered: FFF0, FF90, FFE5\)/);
        return true;
      },
    );
    assert.equal(fake.connections, 2);
    assert.equal(fake.gatt.connected, false);
    assert.deepEqual(fake.authWrites, []);
  });

  it('says when the clock dropped the connection during discovery', async () => {
    fake.droppedDiscoveryConnections = 2;
    await assert.rejects(
      clock.connect(PASSWORD),
      (error) => {
        assert.ok(error instanceof ClockConnectionError);
        assert.match(
          error.message,
          /dropped the connection \d+ s after connecting, before offering services FFE0 and FFE5 \(offered: none\)/,
        );
        return true;
      },
    );
    assert.equal(fake.connections, 2, 'one reconnect, no more');
    assert.equal(fake.discoveryPolls, 2, 'no polling a dropped link');
  });

  it('keeps what the clock offered before it dropped the connection', async () => {
    const LISTS_BEFORE_DROP = 2;
    fake.brokenDiscoveryConnections = 2;
    const discover = fake.gatt.getPrimaryServices;
    fake.gatt.getPrimaryServices = async () => {
      if (fake.discoveryPolls < LISTS_BEFORE_DROP) return discover();
      fake.gatt.disconnect();
      throw new DOMException('GATT Server is disconnected', 'NetworkError');
    };
    await assert.rejects(clock.connect(PASSWORD), /dropped the connection .*\(offered: FFF0, FF90, FFE5\)/);
  });

  it('opens the chooser again when Chrome has forgotten the clock', async () => {
    let disconnects = 0;
    clock.addEventListener('disconnected', () => (disconnects += 1));
    await clock.connect(PASSWORD);
    fake.gatt.disconnect();
    fake.outOfRange = true;
    await clock.connect(PASSWORD);
    assert.equal(clock.connected, true);
    assert.equal(fake.chooserShown, 2);
    fake.gatt.disconnect();
    assert.equal(disconnects, 2, 'the same device chosen again is not watched twice');
  });

  it('reports a clock still out of range after choosing it again', async () => {
    await clock.connect(PASSWORD);
    fake.gatt.disconnect();
    fake.outOfRange = true;
    // A chooser whose scan does not bring the clock back.
    const requestDevice = async (options) => {
      fake.chooserShown += 1;
      fake.lastRequest = options;
      return fake;
    };
    Object.defineProperty(globalThis, 'navigator', { value: { bluetooth: { requestDevice } }, configurable: true });
    await assert.rejects(clock.connect(PASSWORD), (error) => error.name === 'ClockOutOfRangeError' && /no longer in range/.test(error.message));
    assert.equal(fake.chooserShown, 2, 'the chooser opens once, not in a loop');
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

describe('traffic log', () => {
  const record = () => {
    const lines = [];
    clock.addEventListener('traffic', (event) => lines.push(`${event.detail.direction} ${event.detail.text}`));
    return lines;
  };

  it('shows bytes as text, and anything unprintable as \\xNN', () => {
    assert.equal(describeBytes(new TextEncoder().encode('OSC')), 'OSC');
    assert.equal(describeBytes(Uint8Array.from([0x00, 0x52, 0x0d, 0xff])), '\\x00R\\x0d\\xff');
  });

  it('records the login without the password, commands, and every raw notification', async () => {
    const lines = record();
    await clock.connect(PASSWORD);
    await clock.requestStatus();
    console.log(lines.join('\n'));
    assert.deepEqual(lines.slice(0, 4), [
      'sent password login',
      'received password answer \\x00',
      'info command characteristic allows: writeWithoutResponse; writing without response',
      `sent ${STATUS_REQUEST}`,
    ]);
    const received = lines.slice(4).map((line) => line.replace(/^received /, ''));
    assert.equal(received.join(''), fake.statusFrame(), 'the notifications, in order, make up the frame');
    assert.ok(received.length > 1, 'one line per notification, not per frame');
    assert.ok(!lines.some((line) => line.includes(PASSWORD)), 'the password never reaches the log');
  });

  it('records the factory password retry', async () => {
    fake.password = FACTORY_PASSWORD;
    const lines = record();
    await clock.connect(PASSWORD);
    assert.deepEqual(lines, [
      'sent password login',
      'received password answer \\x01',
      'sent password login with the factory password',
      'received password answer \\x02',
      'info command characteristic allows: writeWithoutResponse; writing without response',
    ]);
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

  it('reads the settings again by reconnecting, as the clock answers only once per connection', async () => {
    fake.statusOncePerConnection = true;
    await clock.connect(PASSWORD);
    await clock.requestStatus();
    await clock.send(encodeTime('alarm_1', 6, 15));
    await assert.rejects(clock.requestStatus(), ClockConnectionError, 'a second request on the same connection');

    await clock.reconnect(PASSWORD);
    const status = await clock.requestStatus();

    assert.equal(status.times.alarm_1, '06:15');
    assert.equal(fake.connections, 2);
    assert.equal(fake.chooserShown, 1, 'the same clock, without the chooser');
  });

  it('connects on a reconnect while disconnected', async () => {
    await clock.reconnect(PASSWORD);
    assert.ok(clock.connected);
    assert.equal(fake.connections, 1);
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

  it('writes with response when the module allows both, and says so in the log', async () => {
    fake.characteristics[UART_TX_CHAR].properties.write = true;
    const lines = [];
    clock.addEventListener('traffic', (event) => lines.push(event.detail.text));
    await clock.connect(PASSWORD);
    await clock.send('K02');
    const uart = fake.writes.filter((w) => w.uuid === UART_TX_CHAR);
    assert.deepEqual(uart.map((w) => w.withResponse), [true]);
    assert.ok(lines.includes('command characteristic allows: write, writeWithoutResponse; writing with response'));
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
    assert.deepEqual(fake.rgb, [0, 255, 0], 'the R field lights green');
  });

  it('spaces commands by the configured gap', async () => {
    const spaced = new Clock({ ...FAST, commandGapMs: 40 });
    await spaced.connect(PASSWORD);
    const start = performance.now();
    await spaced.send('K01', 'K02', 'K03');
    const elapsed = performance.now() - start;
    assert.ok(elapsed >= 2 * 40, `two full gaps between three commands, got ${elapsed} ms`);
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

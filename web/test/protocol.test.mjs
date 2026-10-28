// Encoders and the status parser, against the vectors shared with the Python integration.
import assert from 'node:assert/strict';
import { describe, it } from 'node:test';

import vectors from '../../docs/fixtures/protocol-vectors.json' with { type: 'json' };
import {
  AuthResultError,
  HUE_POSITIONS,
  STATUS_LENGTH,
  StatusAssembler,
  StatusFrameError,
  TOGGLES,
  chunked,
  encodeAuth,
  encodeColor,
  encodeKey,
  encodeTime,
  encodeTimeSync,
  encodeToggle,
  hueToRgb,
  parseAuthResult,
  parseStatus,
  rgbToHue,
} from '../protocol.js';

const FRAME = vectors.status.frame;
const split = (text, size) => chunked(new TextEncoder().encode(text), size);

describe('encoders', () => {
  for (const { rgb, command } of vectors.color) {
    it(`colour ${rgb} → ${command}`, () => assert.equal(encodeColor(rgb), command));
  }
  for (const rgb of vectors.color_invalid) {
    it(`rejects colour ${rgb}`, () => assert.throws(() => encodeColor(rgb), RangeError));
  }
  for (const { toggle, on, command } of vectors.toggle) {
    it(`toggle ${toggle}=${on} → ${command}`, () => assert.equal(encodeToggle(toggle, on), command));
  }
  it('gives every toggle a distinct command', () => {
    assert.equal(new Set(TOGGLES.map((toggle) => encodeToggle(toggle, true))).size, 16);
  });
  it('rejects an unknown toggle or key', () => {
    assert.throws(() => encodeToggle('tube_7', true), RangeError);
    assert.throws(() => encodeKey('reset'), RangeError);
  });
  for (const { key, command } of vectors.key) {
    it(`key ${key} → ${command}`, () => assert.equal(encodeKey(key), command));
  }
  for (const { setting, time, command } of vectors.time) {
    it(`time ${setting} ${time} → ${command}`, () => {
      const [hour, minute] = time.split(':').map(Number);
      assert.equal(encodeTime(setting, hour, minute), command);
    });
  }
  for (const [hour, minute] of vectors.time_invalid) {
    it(`rejects time ${hour}:${minute}`, () => {
      assert.throws(() => encodeTime('alarm_1', hour, minute), RangeError);
    });
  }
  for (const { local, command } of vectors.time_sync) {
    it(`time sync ${local} → ${command} (Sunday is 0)`, () => {
      assert.equal(encodeTimeSync(new Date(local)), command);
    });
  }
  for (const { old, new: next, command } of vectors.auth) {
    it(`password ${old}→${next}`, () => assert.equal(encodeAuth(old, next), command));
  }
  for (const [old, next] of vectors.auth_invalid) {
    it(`rejects password ${old}/${next}`, () => assert.throws(() => encodeAuth(old, next), /6 digits/));
  }
});

describe('password answers', () => {
  for (const { byte, result } of vectors.auth_result) {
    it(`byte ${byte} → ${result}`, () => assert.equal(parseAuthResult(Uint8Array.of(byte)), result));
  }
  for (const bytes of vectors.auth_result_invalid) {
    it(`rejects ${JSON.stringify(bytes)}`, () => {
      assert.throws(() => parseAuthResult(Uint8Array.from(bytes)), AuthResultError);
    });
  }
});

describe('hue slider', () => {
  for (const { position, rgb } of vectors.hue) {
    it(`position ${position} ↔ ${rgb}`, () => {
      assert.deepEqual(hueToRgb(position), rgb);
      assert.equal(rgbToHue(rgb), position);
    });
  }
  it('wraps the end of the slider back to red', () => {
    assert.deepEqual(hueToRgb(HUE_POSITIONS), [255, 0, 0]);
  });
  it('rejects positions off the slider', () => {
    assert.throws(() => hueToRgb(-1), RangeError);
    assert.throws(() => hueToRgb(HUE_POSITIONS + 1), RangeError);
  });
  it('has no position for colours off the wheel', () => {
    assert.equal(rgbToHue([0, 0, 0]), null);
    assert.equal(rgbToHue([120, 60, 30]), null);
  });
});

describe('status frames', () => {
  it('parses every field', () => {
    assert.deepEqual(parseStatus(FRAME), vectors.status.expected);
  });
  it('reports a time no clock could show as unknown instead of rejecting the frame', () => {
    const { frame, times } = vectors.status_unset_time;
    assert.deepEqual(parseStatus(frame).times, times);
  });
  for (const { reason, frame } of vectors.status_invalid) {
    it(`rejects a frame: ${reason}`, () => assert.throws(() => parseStatus(frame), StatusFrameError));
  }
  for (const size of [1, 7, 20, 93, 200]) {
    it(`reassembles from ${size}-byte notifications`, () => {
      const assembler = new StatusAssembler();
      const frames = split(FRAME, size).flatMap((chunk) => assembler.feed(chunk));
      assert.deepEqual(frames, [FRAME]);
    });
  }
  it('skips noise and a truncated frame', () => {
    const assembler = new StatusAssembler();
    const stream = `garbage${FRAME.slice(0, 40)}${FRAME}${FRAME}`;
    assert.deepEqual(split(stream, 20).flatMap((chunk) => assembler.feed(chunk)), [FRAME, FRAME]);
  });
  it('drops a partial frame on reset', () => {
    const assembler = new StatusAssembler();
    assert.deepEqual(assembler.feed(FRAME.slice(0, 50)), []);
    assembler.reset();
    assert.deepEqual(assembler.feed(FRAME.slice(50)), []);
    assert.deepEqual(assembler.feed(FRAME), [FRAME]);
  });
  it('never mistakes 93 bytes without a terminator for a frame', () => {
    assert.deepEqual(new StatusAssembler().feed(`R${'x'.repeat(STATUS_LENGTH - 1)}`), []);
  });
});

describe('chunked', () => {
  it('splits a write into ATT-sized packets', () => {
    const packets = chunked(new TextEncoder().encode('$2026-10-25;09:03:07;00'));
    assert.deepEqual(packets.map((packet) => packet.length), [20, 3]);
  });
});

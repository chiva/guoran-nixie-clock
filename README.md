# Guoran nixie clock: modern Bluetooth control

Control the Guoran (果然, "点亮你的生活") Bluetooth nixie tube clock without its abandoned
Android app. That clock is the GANXIN / diym.vip IN1204M, its IN1404M, IN1206M and IN1406M
siblings, and any other clock advertising as `XGGF…` that the original `guoran.apk` could drive.
This repository has three parts:

- **[Web app](web/)**: an English replacement for the phone app. It runs in Chrome on Android,
  Windows, macOS, Linux or ChromeOS through Web Bluetooth, and needs no installation.
- **[Home Assistant integration](custom_components/guoran_nixie/)**: the clock as a device with a
  backlight light, 16 switches, 4 times and 10 buttons. It works through the local adapter or any
  ESPHome Bluetooth proxy.
- **[Protocol documentation](docs/PROTOCOL.md)** and a **[capability map](docs/CAPABILITIES.md)**,
  decoded from the original app's source and cross-checked against the clock's manual.

The original app is not included here; the vendor still hosts it at
<https://diym.vip/upload/guoran.apk>.

## Why the original app fails on recent phones

The APK is a 2019-era DCloud HTML5+ wrapper. It targets Android API 26, and its closed-source
Bluetooth runtime predates the Android 12 permission model (`BLUETOOTH_SCAN` and
`BLUETOOTH_CONNECT`, which the manifest does not declare). Its scan filter (`FFF0`, `FFB0`) is
not the problem: the clock advertises exactly those services. But the app is not the only
problem either: on a Pixel 9, Android itself never lists the clock's services, even to nRF
Connect (see [Service discovery on Android](docs/PROTOCOL.md#service-discovery-on-android)).
The app also asks for contacts, microphone and phone-state permissions it never uses.
Repackaging it would not fix the runtime, so it is replaced instead. The vendor's own manual
admits it: *"If the clock cannot be found, it may be the compatibility problem between mobile
phone and software, please change the mobile phone"*. The vendor's WeChat mini program 果然时钟
is another way to reach the clock from a recent phone.

## Web app

Open <https://chiva.github.io/guoran-nixie-clock/> in Chrome. GitHub Pages serves it over
HTTPS, which Web Bluetooth requires. Tap **Connect**, pick the `XGGF…` clock, and Android asks for the *Nearby devices*
permission for Chrome. *Add to Home screen* installs it as an app that also opens offline.

- Close the original app, or any other phone connected to the clock, first: the clock's module
  accepts one connection at a time.
- The default password is 210709, the one the original app sets. It can be changed under
  *Connection settings*.
- iPhone and iPad browsers do not support Web Bluetooth.
- **Known issue:** on a Pixel 9 the connection fails because Android never lists the clock's
  services. Chrome on macOS works with the same clock.

To run it locally (`localhost` counts as a secure context):

```bash
python3 -m http.server 8000 -d web   # then open http://localhost:8000 in Chrome
```

## Home Assistant

### 1. A Bluetooth radio near the clock

Home Assistant must *connect* to the clock, not just hear it. Use either:

- the Home Assistant host's own Bluetooth adapter, if it is within range, or
- an **ESPHome Bluetooth proxy** with active connections: any ESP32 flashed with
  [`esphome/nixie-bt-proxy.yaml`](esphome/nixie-bt-proxy.yaml) (copy `secrets.example.yaml` to
  `secrets.yaml` first), or a ready-made proxy from <https://esphome.github.io/bluetooth-proxies/>.
  A passive proxy (`active: false`) is not enough.

### 2. Install the integration

- **HACS**: *HACS → ⋮ → Custom repositories*, add `https://github.com/chiva/guoran-nixie-clock`
  as an *Integration*, install
  *Guoran Nixie Clock* and restart Home Assistant.
- **Manually**: copy `custom_components/guoran_nixie` into your configuration's
  `custom_components/` folder and restart.

The clock is discovered automatically (*Settings → Devices & services → Discovered*). It can also
be added from *Add integration → Guoran Nixie Clock*. Enter the Bluetooth password (210709 if the
original app was ever used with the clock).

### Behaviour

- Home Assistant connects on demand and **disconnects after 30 s idle**, so the web app can still
  connect in between. Each status refresh (every 5 min by default, configurable) briefly holds the
  connection.
- With *Keep the clock's time in sync* on (the default), the clock is set to Home Assistant's
  local time on the first refresh and then hourly. **Sync time now** does it on demand.
- Commands update the entity at once, then a refresh confirms what the clock kept.
- A changed password triggers a re-authentication prompt.

### Example automation

```yaml
automation:
  - alias: Nixie backlight follows sunset
    triggers:
      - trigger: sun
        event: sunset
    actions:
      - action: light.turn_on
        target:
          entity_id: light.xggf_1234_backlight
        data:
          hs_color: [25, 100]   # warm amber
```

## Open questions

A few protocol details are inferred from the app's code and not yet confirmed on hardware:
packet sizes, whether off-wheel RGB values and "black" are honoured, and what the time-sync toggle
changes. See [PROTOCOL.md § Open questions](docs/PROTOCOL.md#open-questions) for how to settle
them with an HCI snoop log from the old phone.

## Development

```bash
uv sync                                   # Home Assistant test environment (Python 3.14)
uv run ruff check . && uv run ruff format --check . && uv run mypy
uv run pytest --cov                       # integration + protocol, against a fake clock
(cd web && npm test)                      # web app protocol and Web Bluetooth session
```

Both protocol implementations are tested against the shared vectors in
[`docs/fixtures/protocol-vectors.json`](docs/fixtures/protocol-vectors.json). A change to the
protocol starts there.

`uv run pre-commit install` runs the same checks on each commit. Changes go through pull requests
with a Conventional Commit title; CI must pass before merging. release-please collects merged
changes into a release PR, and merging that publishes the release: the HACS zip and the web app
on GitHub Pages. GitHub does not start CI on PRs opened by Actions, so close and reopen the release
PR to run its checks, then approve it (the code owner's review is required) and merge.

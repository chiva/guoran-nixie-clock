# Capabilities

Everything the original app could do, and where each control lives now. Commands are described
in [PROTOCOL.md](PROTOCOL.md). Home Assistant entity ids assume the default device name
`XGGF-1234`.

## The clock

GANXIN / diym.vip "Guoran" nixie clock, V2 Bluetooth generation: **IN1204M** (4 × IN-12 tubes,
walnut or rosewood case, 5 V USB, under 400 mA) and its siblings IN1404M (4 × IN-14), IN1206M and
IN1406M (6 tubes). It has an RTC, an external temperature sensor, voice announcements, an RGB
backlight under each tube, three touch buttons and a 14-key IR remote. Manuals:
<https://diym.vip/archives/%E8%AF%B4%E6%98%8E%E4%B9%A6> (the IN1204M one:
[English PDF](https://diym.vip/upload/IN1404M_IN1204M_BLE_EN.pdf)).

| Touch button | Click | Inside the settings menu |
|---|---|---|
| L | backlight on/off | + |
| OK | clock on/off | OK |
| R | show the date | − |
| L + R | open/close the settings menu | |
| OK + R | start the timer | |

Remote: ON-OFF, SET, BACK, OK, +, −, VOL, LIGHT (brightness), TIME, DATE, TEMP, and 1/2/3 for the
backlight (change colour, fixed colour, colour-change mode). The Bluetooth app sends the first
six; see [PROTOCOL.md § Keys](PROTOCOL.md#keys).

The same app and protocol serve the 4- and 6-tube models, which is why there are six tube
backlight switches. On a 4-tube clock, tubes 5 and 6 presumably control nothing.

## Backlight

| Original (中文 / English) | Command | Web app | Home Assistant |
|---|---|---|---|
| 颜色控制台 / Color console slider | `R…-G…-B…` | Backlight hue slider | `light.xggf_1234_backlight` (hue; saturation and brightness ignored) |
| — | `R000-G000-B000` | Backlight **Off** | turning the light off (unverified on hardware) |
| 6 tube switches | `S1x`–`S6x` | Tube backlights 1–6 | `switch.xggf_1234_tube_N_backlight` |

## Front-panel buttons

| Original | Command | Web app | Home Assistant |
|---|---|---|---|
| `<` `M` `>` (under the slider): previous colour, backlight mode, next colour (inferred) | `K01` `K02` `K03` | ‹ M › | `button.xggf_1234_backlight_previous_colour`, `_backlight_mode`, `_backlight_next_colour` |
| 操作按键 / Operating buttons: power, back, +, SET, OK, − (the remote's) | `K04`–`K09` | Front-panel buttons | `button.xggf_1234_power`, `_back`, `_plus`, `_set`, `_ok`, `_minus` |

## Time

| Original | Command | Web app | Home Assistant |
|---|---|---|---|
| 获取系统时间 / Acquisition system time: the clock takes its time from the phone (menu 01); the app sent it every second | `S7x`, `$…` | Phone time sync switch (sends the time every minute while open) and **Set the clock to this time** | `switch.xggf_1234_phone_time_sync`; option *Keep the clock's time in sync* (on connect, then hourly); `button.xggf_1234_sync_time_now` |

## Alarms and schedule

| Original | Command | Web app | Home Assistant |
|---|---|---|---|
| 闹钟1 / Alarm clock 1 (switch + time) | `S8x`, `AHH:MMA` | Alarm 1 | `switch.xggf_1234_alarm_1`, `time.xggf_1234_alarm_1_time` |
| 闹钟2 / Alarm clock 2 (switch + time) | `S9x`, `AHH:MMB` | Alarm 2 | `switch.xggf_1234_alarm_2`, `time.xggf_1234_alarm_2_time` |
| 定时开关机 / Timing switch: 开机时间 boot time, 关机时间 off time | `S0x`, `THH:MMO`, `THH:MMC` | Scheduled power | `switch.xggf_1234_scheduled_power_on_off`, `time.xggf_1234_power_on_time`, `time.xggf_1234_power_off_time` |

The original app only let you edit a time while its switch was on. Both new apps allow it at any
time.

## Other settings

| Original (中文 / English) | Command | Home Assistant |
|---|---|---|
| 亮度自动 / LUX Auto | `SAx` | `switch.xggf_1234_auto_brightness` |
| 使用12小时格式 / Time formats 12 | `SBx` | `switch.xggf_1234_12_hour_format` |
| 环境光线感应 / Ambient light induction | `SCx` | `switch.xggf_1234_ambient_light_sensing` |
| 整点语音报时 / On time alarm (hourly chime) | `SDx` | `switch.xggf_1234_hourly_voice_chime` |
| 语言使用英语 / Using English (voice language) | `SEx` | `switch.xggf_1234_english_voice` |
| 使用感应开关 / Inductive switch: external on/off input for a sensor (menu 15, inferred) | `SFx` | `switch.xggf_1234_sensor_switch` |

The web app shows the same six switches under *Other settings*.

## Menu-only settings

Transition effect, colon, prompt volume, display mode, night mode, alarm melody, weekday backlight,
tube protection and factory reset have no Bluetooth command. They are reached through the clock's
menu, with the **Set**, **Plus**, **Minus**, **OK** and **Back** buttons in either app (the clock
speaks each step). See [PROTOCOL.md § Settings only reachable through the menu](PROTOCOL.md#settings-only-reachable-through-the-menu).

## Connection

| Original | Now |
|---|---|
| Fixed password, hidden from the user (210709) | Configurable 6-digit password, defaulting to 210709 |
| Factory module taken over silently (`000000` → `210709`) | Same, to the password you choose |
| Scan filtered by service UUIDs `ffb0`/`fff0`, then name `XGGF` | Name prefix `XGGF` only |
| Status reply had to arrive in one 93-byte notification | Reassembled from any notification size |

# Busch-Jaeger 1095 UTA-BT for Home Assistant

Local Bluetooth integration for the **Busch-Jaeger / ABB 1095 UTA-BT** thermostat, using the protocol reconstructed from free@home Next and validated on a physical device.

**Works with an ESPHome Bluetooth Proxy.** Home Assistant runs authentication, encryption, and thermostat commands. A nearby ESP32 forwards the Bluetooth traffic over your home network; it needs only standard ESPHome Bluetooth Proxy firmware.

## Features

- A climate entity with measured temperature and desired/effective setpoint in °C.
- Target temperature changes in 0.5 °C steps.
- **Comfort** and **Eco** presets.
- Persistent connection for push updates, with automatic reconnection and a fresh authenticated session after a disconnect.
- UI setup from an installation export and password, or an existing 32-byte ACS secret.
- Reauthentication, configurable UI temperature limits, and credential-redacted diagnostics.

The integration exposes heating and the two presets. It does not expose a power-off command or infer a heating/idle action from room temperature. The thermostat continues regulating heat locally when Home Assistant or the proxy is offline.

## Requirements

- Home Assistant **2025.1 or newer**.
- The existing installation export from **free@home Next** (`.gepinstallation`) and its installation password, or the derived ACS secret.
- A connectable Bluetooth adapter in range, or a nearby **ESPHome active Bluetooth proxy** added to Home Assistant.
- Disconnect the phone app while Home Assistant is connecting to the thermostat.

An ESP32-WROOM-32 running the standard [ESPHome Bluetooth Proxy firmware](https://esphome.io/components/bluetooth_proxy/) is suitable. A continuously connected thermostat occupies one active connection slot. The HA server itself does not need to be in BLE range.

This first implementation uses the channel/input/group mapping validated for the supplied 1095 UTA-BT configuration (channel 1, function `0xCF`, KNX individual address `0x0201`). It does **not** claim support for every ABB/free@home BLE product or every thermostat configuration. The service UUID is shared by other ABB products, so accept discovery only for your 1095 UTA-BT.

## Install with HACS

1. In HACS, open **Custom repositories**.
2. Add `https://github.com/Bloeckchengrafik/hacs-1095-UTA-BT` with category **Integration**.
3. Download **Busch-Jaeger 1095 UTA-BT**. If no release is listed yet, select the default/main version.
4. Restart Home Assistant.
5. Go to **Settings → Devices & services → Add integration**, then select **Busch-Jaeger 1095 UTA-BT**. A discovery card may also appear.

For manual installation, copy `custom_components/busch_jaeger_1095_bt` into `/config/custom_components/` and restart Home Assistant.

## Configure

1. Enter the thermostat's Bluetooth address and a device name.
2. Choose **Upload installation export and enter its password**, then select the `.gepinstallation` file from your computer. You do not need to copy the export onto the HA server manually.
3. The integration derives the ACS secret, deletes the temporary upload, and validates an authenticated BLE connection. Setup sends **no thermostat-setting commands**.
4. The climate entity becomes available after setup.

Only the derived ACS secret is retained in Home Assistant's config entry. The installation export and installation password are not retained. Treat Home Assistant backups as sensitive because they contain the derived credential.

Alternatively, enter the 32-byte ACS secret as **64 hexadecimal characters**. The 16-byte `PK` printed in the device's Data Matrix is a different credential and cannot be used here.

Under the integration's options, adjust the displayed target-temperature range to match the installation. Defaults are **10–30 °C**. These are UI/service limits; they do not reprogram the thermostat's internal limits. The protocol client permits at most 5–35 °C.

## State and command behavior

- Room temperature is reported periodically through the authenticated connection.
- Setpoint and Eco status are updated from thermostat reports. They may initially be unknown after setup/reconnect until the thermostat reports them or a control changes.
- A preset selection also updates after both mode writes receive successful application replies, including firmware that does not send a separate mode report. Later device reports remain authoritative.
- The UI displays the **effective reported setpoint**. In Eco mode, that can differ from the normal comfort target. After a preset command succeeds, the integration reads that effective target from output datapoint 2; it does not calculate an Eco reduction locally. The previous target is cleared while waiting for a fresh value, unless a fresh report already arrived during the mode writes.
- If the device rejects the read or returns an invalid value, the preset remains selected and the target stays unknown until a valid report arrives, or keeps any fresh report received during the mode writes. A read timeout triggers reconnection without replaying the preset.
- A target-temperature command preserves the existing mode. Select **Comfort** separately if you also want to leave Eco.
- Presets are never updated merely because a command was sent: both application replies must succeed. State is not restored from a previous session.
- Connection failures mark the entity unavailable. The integration reconnects with exponential backoff, up to 60 seconds between attempts.
- A failed or timed-out command is **not automatically replayed**: it may already have been applied. Wait for reconnection and inspect reported state before repeating it.

## Troubleshooting

**Cannot connect:** verify that HA has added the ESPHome proxy, active connections are enabled, a connection slot is available, and the thermostat is within BLE range. Check that the phone app and diagnostic scripts are disconnected.

**Authentication failed:** use the export/password from this thermostat's current installation. If the credential changes, use Home Assistant's reauthentication flow. To correct a credential during initial setup, cancel the flow and start again.

**Unknown target/preset immediately after reconnect:** those fields have no assumed defaults. Temperature needs a report; a preset can also be filled by an acknowledged HA preset command. Change the setting on the thermostat or through HA to obtain a fresh report. There is no unvalidated startup query or setting write.

Enable debug logs for `custom_components.busch_jaeger_1095_bt` when investigating connection recovery. Diagnostics redact the ACS secret and Bluetooth address. Never attach your installation export, password, or secret to an issue.

## Development and validation

The protocol was validated with temperature changes and alternating temperature/Eco commands on the physical thermostat. This Home Assistant wrapper additionally has tests covering:

- Full P-256 / CMAC authentication with a simulated peripheral.
- Multiple encrypted commands in one session, with the correct reset of the inner telegram fragment sequence and continuous outer/cipher counters.
- Real captured reply/state formats, and stopping writes after corrupted ciphertext.
- Proxy-aware device lookup through HA's Bluetooth manager.
- UI setup, upload disposal, authentication errors, options, and reauthentication.
- Actual HA climate service calls, entity unload, and diagnostics redaction.
- Failed-command handling without replay.
- Effective setpoint readback after repeated preset transitions, mismatched reply filtering, and readback failures without repeating accepted writes.

The new setpoint read request was reconstructed from the APK; its actual response still needs confirmation on physical hardware. See [protocol readback notes](PROTOCOL.md).

Tests run against HA **2025.1.4 / Python 3.12** and **2026.10.2 / Python 3.14**. The HA/proxy wrapper still needs an end-to-end check in your installation; passing simulated tests is not that check.

```sh
python -m venv .venv
.venv/bin/pip install homeassistant==2026.10.2 pytest-homeassistant-custom-component pyserial pyudev serialx aiousbwatcher ruff
.venv/bin/pytest
```

Use a Python version supported by the selected Home Assistant release. No actual exports, passwords, session keys, or packet captures are included in this repository.

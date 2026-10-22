# Effective setpoint readback

Version 0.1.2 adds a read after an accepted preset change. The existing mode writes remain unchanged. No temperature value is written by the refresh.

## APK evidence

The supplied free@home Next ARM64 binary contains:

- `TelegramFactory::getDataPointValue` at `0x509e22c`: for `isInput=false`, selects device property `0xDF` and datapoint service `0x01`, appends the one-based channel, then the output index and finalizes lengths.
- `TelegramFactory::functionPropertyCommand` at `0x509e4c8`: constructs the function-property command envelope (`APCI 0x02C7`).
- `DatapointReader::start` at `0x4faddb8`: calls that factory method and registers a matching response callback.
- `DatapointReader::handleDPValue` at `0x4fadb20`: checks the returned datapoint index, then decodes the following value using the datapoint type. `XmlJobBase::setDataPointValue` at `0x50b1930` calls `IncomingTelegram::readValue`.

For the supported channel 1 / function CF mapping, output 2 is effective setpoint (pairing `0x0033`, DPT 9.001).

## Request

The uncompressed factory frame for source `FFF0`, destination `0201`, channel 1, output 2 is:

```text
11 00 04 00 FF F0 02 01 09 02 C7 00 DF 00 01 03 00 01 02
```

It is sent through the existing authenticated encrypted transport with the per-telegram prefix `03 02 00 00`. Transport/cipher counters continue across reads and writes.

Only a reply matching property `DF`, service `01`, channel 1, output 2, and the configured device source can satisfy the read. The value after the echoed index must contain exactly two bytes and is decoded as KNX float16. A rejected/malformed reply does not change the accepted preset or cause a command replay. A transport failure requires a new authenticated session.

The coordinator waits 300 ms after the mode acknowledgements before reading to allow the control cycle to apply the mode. The old effective setpoint is cleared until a fresh read/report arrives; a fresh report received during the mode writes is retained.

## Validation status

The frame construction and reply handling are covered by simulated encrypted sessions, including repeated mode changes, a different effective Eco target, mismatched acknowledgements, malformed/rejected reads, and timeout handling. The new read transaction has **not yet been validated on the physical thermostat**. The request follows the reconstructed app path; the expected reply layout follows the app's reader and existing function-property replies. Synthetic responses are test fixtures, not recorded read transactions.

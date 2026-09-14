# Seplos HV BCU — wire protocol reference

The master control box does **not** speak Modbus. This document records the frame format it
actually uses, the command map, payload decoders, and the live protection configuration —
everything needed to build a Home Assistant integration without a Windows machine in the loop.

| | |
|---|---|
| Unit | BCU-1002C-30443-1.0 |
| Firmware | HVP-B1018-30443-1.04 |
| Protocol string | HV-PACE-ALL-CA-DATA-V0.22 |
| Link | RS485-1, **57600 8-N-1** |
| Verified against | 894/894 and 4915/4915 captured frames, all CRC-checked |

---

## 1. System state

| Status | Item | Note |
|---|---|---|
| Resolved | BMU cell count | All four modules set to 32 cells, verified across a power cycle |
| Resolved | Breaker tripping | Broken sense tap at BMU2 cell 14/15; repaired, pack holds |
| Resolved | RS485-1 link | Never faulty — wrong protocol, not wrong wiring |
| **Open (blocking)** | **BMU1 NTC harness** | Reports zero temperature sensors; no thermal protection on that module |
| Open (watch) | BMU1 imbalance | 228 mV spread against a 400 mV L1 alarm; genuine, not a sense fault |
| Ready | Protection limits | Read and verified sound; no voltage changes required |

---

## 2. Frame format

```
9A │ src │ dst │ CMD(u16 BE) │ 00 │ LEN(u16 BE) │ payload │ CRC(u16 BE) │ 9D
```

* **Addressing** — host `0x01`, BCU `0x21`. Replies swap the two bytes.
* **Checksum** — CRC-16/MODBUS (poly `0x8005` reflected, init `0xFFFF`) computed over the frame
  from the `0x9A` **inclusive** up to the CRC, stored **big-endian**. Modbus itself stores it
  little-endian; getting this backwards is the easy mistake.
* **Requests** — every poll carries a six-byte zero payload; length is always `0x0006`.
* **Escaping** — none. `0x9A` and `0x9D` do occur inside payloads; frame on the length field,
  never on the delimiters.

```
request   9A 01 21 00 05 00 00 06 00 00 00 00 00 00 76 39 9D
reply     9A 21 01 00 05 00 00 1E 33 30 34 34 33 31 35 35 …  DD 13 9D
                                 └─ ASCII "304431553990004P" ─┘
```

Reference CRC:

```python
def crc16(data: bytes) -> int:
    c = 0xFFFF
    for b in data:
        c ^= b
        for _ in range(8):
            c = (c >> 1) ^ 0xA001 if c & 1 else c >> 1
    return c
# frame = header + payload;  frame += crc16(frame).to_bytes(2, "big") + b"\x9D"
```

### Why every Modbus attempt failed

Six exhaustive sweeps — five baud rates, all 248 Modbus addresses, both read function codes,
two framings, two adapters, both A/B orientations — returned zero bytes. The wire, adapter,
baud and driver were correct throughout. The BCU has no reason to answer a Modbus frame.
*When a device is provably alive and provably reachable but silent, question the protocol
before the hardware.*

---

## 3. Command map

### Telemetry and identity

| CMD | Bytes | Returns | Decoded |
|---|---:|---|---|
| `0x0001` | 30 | Protocol / model string | full |
| `0x0002` | 30 | Firmware version string | full |
| `0x0005` | 30 | BMS serial number | full |
| `0x0007` | 30 | Pack serial number (empty on this unit) | full |
| `0x0009` | 6 | — | unknown |
| `0x000A` | 82 | Pack summary — voltage, extremes, indices | partial |
| `0x000B` | 38 | Status / relay states | unknown |
| `0x000C` | 266 | All cell voltages, per module | full |
| `0x000D` | 120 | All temperatures, per module | full |
| `0x000E` | 162 | — | unknown |
| `0x0011` | 2 / 142 | Two response shapes | unknown |
| `0x0039` | 4 | — | unknown |

Seen once each during a tab sweep, purpose unestablished: `0x0015` `0x0017` `0x0019` `0x001D`
`0x001F` `0x0021` `0x0023` `0x0025` `0x0027` `0x002A` `0x002B` `0x0042` `0x0043`. Also `0x0057`,
which appeared only as an **unsolicited reply** — possibly an event push, worth watching.

### Read/write convention — the safety rule

Every parameter command observed doing a **read** is **odd**: `0x0201`, `0x0203` … `0x023F`.
Even ids are presumed to be the matching writes and were never issued.

> **Do not probe even ids speculatively.** A write command sent with a zero payload could clear
> protection limits on a live 250 kWh pack. If write support is ever wanted, capture the vendor
> tool actually saving a value — never infer a write opcode from an addressing pattern.

---

## 4. Payload decoders

### Cell voltages — `0x000C`

```
u16  module_count
repeat module_count times:
  u8   module_id          1..4
  u8   cell_count         32 on this system
  u16  cell_mV × cell_count
```

### Temperatures — `0x000D`

```
u8   module_count
u8   bcu_sensor_count
u16  bcu_temp × bcu_sensor_count        0.1 K
repeat module_count times:
  u8   module_id
  u8   sensor_count       0 on a module with a broken NTC harness
  u16  temp × sensor_count              0.1 K
  u8   extra_count
  u16  extra × extra_count
```

Temperatures are tenths of a kelvin: `°C = (raw − 2731) / 10`. A module with a failed
thermistor loom reports `sensor_count = 0` rather than null values — a clean signal to surface
as an integration diagnostic.

### Pack summary — `0x000A`

```
offset 0   u16   pack voltage, 0.1 V
offset 2   u16   secondary voltage, 0.1 V
offset 4   u16   tertiary voltage, 0.1 V
…                middle section undecoded (currents, capacities, SOC/SOH)
last 16 bytes:
  u16 max_cell_mV, u16 max_index
  u16 min_cell_mV, u16 min_index
  u16 max_temp,    u16 max_temp_index
  u16 min_temp,    u16 min_temp_index
```

Cell index is `module × 32 + cell`, zero-based. Self-validating: the block reported 3416 mV at
index 20 and 3188 mV at index 18, resolving to BMU1 C21 and BMU1 C19 — exactly the extremes in
the per-cell block.

### Protection parameters — `0x02xx`

```
24-byte blocks, all u16 BE, three levels:
  L1_trip, L1_trip_delay, L1_recover, L1_recover_delay,
  L2_trip, L2_trip_delay, L2_recover, L2_recover_delay,
  L3_trip, L3_trip_delay, L3_recover, L3_recover_delay

delay unit = 100 ms       temperature = 0.1 K       voltage = mV
```

Self-validating: trip values move monotonically outward L1 → L2 → L3 in every block, in the
correct direction for each parameter's polarity.

---

## 5. Live protection configuration

Read from the unit. This is the baseline to diff against after any change.

| CMD | Parameter | L1 | L2 | L3 | Unit |
|---|---|---|---|---|---|
| `0x0201` | Cell over-voltage | 3500 → 3400 | 3550 → 3450 | 3600 → 3380 | mV |
| `0x0203` | Cell under-voltage | 3000 → 3100 | 2900 → 3000 | 2800 → 3000 | mV |
| `0x020D` | Charge high temperature | 44.9 → 39.9 | 49.9 → 45.9 | 52.9 → 44.9 | °C |
| `0x020F` | Discharge high temperature | 44.9 → 41.9 | 49.9 → 46.9 | 57.9 → 51.9 | °C |
| `0x0211` | Charge low temperature | 4.9 → 9.9 | 2.9 → 6.9 | **−0.1 → 4.9** | °C |
| `0x0213` | Discharge low temperature | −5.1 → −2.1 | −10.1 → −7.1 | −20.1 → −15.1 | °C |
| `0x0215` | Ambient high temperature | 54.9 → 51.9 | 59.9 → 56.9 | 64.9 → 54.9 | °C |
| `0x0217` | Ambient low temperature | −10.1 → −7.1 | −15.1 → −12.1 | −20.1 → −10.1 | °C |
| `0x021F` | SOC high | 90 → 88 | 95 → 93 | 100 → 95 | % |
| `0x0221` | SOC low | 10 → 12 | 6 → 8 | 2 → 7 | % |
| `0x0223` | Insulation, positive | 1000 → 1200 | 600 → 800 | 200 → 700 | Ω/V |
| `0x0225` | Insulation, negative | 1000 → 1200 | 600 → 800 | 200 → 700 | Ω/V |
| `0x0227` | Cell delta, charge | 400 → 250 | 450 → 300 | 500 → 300 | mV |
| `0x0229` | Cell delta, discharge | 400 → 250 | 450 → 300 | 500 → 300 | mV |
| `0x022B` | Temperature delta, charge | 14.9 → 9.9 | 19.9 → 14.9 | 24.9 → 14.9 | °C |
| `0x023D` | Relay high temperature | 99.9 → 94.9 | 104.9 → 99.9 | 109.9 → 104.9 | °C |

Delays run 2–3 s throughout. Current limits sit near **90–100 A charge** and **−90 to −102 A
discharge** (`0x0209` / `0x020B`, four-pair layout not yet confidently mapped) — comfortably
inside the 150 A relays. Heating film runs 9.9 °C on, 14.9 °C off (`0x0239`).

Still undecoded: `0x022F`, `0x0231`, `0x0233`, `0x0235`, `0x0237`, `0x023B`, `0x023F`.

**Assessment.** This configuration is sound and needs no voltage changes. Cell limits step
3500 / 3550 / 3600 mV against a 3650 mV datasheet maximum; under-voltage bottoms at 2800 mV
against a 2500 mV floor; and the charge inhibit at −0.1 °C correctly blocks the lithium-plating
window. The three-level structure gives a warning tier and a trip tier with independent delays —
strictly better than the two-tier model the Modbus documentation describes.

---

## 6. Notes for the Home Assistant integration

### Transport

The CH344 adapter binds to the generic `cdc_acm` driver and exposes `/dev/ttyACM0–3`. It offers
no `TIOCGRS485` and needs none — direction switching is automatic in hardware, and no vendor
driver is required. Address the port by its stable path:

```
/dev/serial/by-id/usb-WCH.CN_USB_Quad_Serial_*-if00
```

rather than `ttyACM0`, which renumbers.

### Polling

The vendor tool polls its whole set roughly once per second — far more than Home Assistant
needs, and avoidable traffic on a shared bus.

* **Fast (10–30 s)** — `0x000A` pack summary. One 82-byte reply carries pack voltage plus
  min/max cell and temperature with their indices; covers most dashboard needs.
* **Medium (60 s)** — `0x000C` cells and `0x000D` temperatures, for per-cell detail.
* **Slow (hourly or at startup)** — identity strings and the `0x02xx` parameter blocks. These
  change only when someone changes them; reading them at startup gives a configuration baseline
  to alert on if it ever drifts.

**Serialise all access behind one lock.** The bus is half-duplex and there is no transaction id
in the frame, so two concurrent readers will interleave replies and corrupt each other.

### Entities worth exposing

* Pack voltage, min/max cell voltage, **cell spread** (max − min — the single most useful health
  number), min/max temperature.
* Per-module spread and the index of the offending cell. Naming these `BMU<n> C<m>` makes a
  fault immediately actionable at the rack.
* **Sensor-count diagnostics.** Expose each module's reported `cell_count` and `sensor_count`.
  A module dropping to zero sensors is exactly the BMU1 fault, and it is invisible in any
  aggregate.
* Headroom against the configured limits — distance from cell spread to the 400 mV L1 alarm,
  from min temperature to the 0 °C charge inhibit. Turns a static limit table into live warnings.

> **Keep the integration read-only.** Nothing here establishes a verified write path, and the
> parameter space controls protection limits on a live battery. An integration that can only
> read cannot misconfigure the pack — a property worth keeping deliberately.

---

## 7. Connecting the Solis inverter

**Use Pylon HV.** Both ends name it identically, so there is no ambiguity about which profile
talks to which, and it is by far the most widely deployed combination.

The BCU's table also holds a **native Ginlong entry** — Ginlong Technologies being the
manufacturer behind Solis — which is tempting on the name alone. It is not obviously actionable:
the inverter's battery list offers `GSL-HV` and `SolisStorage RE-H`, and nothing establishes that
either corresponds to the BCU's Ginlong profile. Matching two vendors' marketing names by
inference is how you end up with an inverter reporting no battery. Treat native Ginlong as an
experiment to run deliberately once Pylon HV works and you have a known-good state to revert to.

| Type | Hex | Description | Note |
|---|---|---|---|
| CAN | `0x1C` | PYLON CAN HV V1.26 | newest Pylon |
| CAN | `0x01` | PYLON CAN HV V1.21 | common default |
| CAN | `0x19` | PYLON CAN HV V1.20 | |
| CAN | `0x02` | PYLON CAN HV V1.18 | |
| CAN | `0x1B` | PYLON CAN HV V1.16 | |
| CAN | `0x08` | GINLONG CAN HV V1.0 | unverified pairing |
| CAN | `0x2D` | 014-GINLONG CAN HV V1.1 (2021-03-25) | unverified pairing |
| RS485 | `0x12` | PYLON RS485 Modbus HV V1.35 | if CAN is unavailable |

### Physical connection

PCS communication is **CAN-3**, on the external communication interface (connector ⑤):

* pin **7** — `CAN-L3`
* pin **8** — `CAN-H3`

Bus rate follows the selected PCS protocol; Pylon HV is conventionally 500 kbps. The box supports
CAN termination — check whether a terminator is needed at each end of the run rather than assuming.

### Before energising

1. **Fix the BMU1 NTC harness.** Treat this as blocking. Once the inverter starts cycling real
   current, thermal protection matters — and the BCU has no temperature input for one quarter of
   the pack. Charge is inhibited below 0 °C precisely because plating is permanent, and that
   inhibit cannot act on a module it cannot measure.
2. **Check the inverter's DC window.** At 128S the pack spans roughly **358 V** (2.80 V/cell L3)
   to **461 V** (3.60 V/cell L3), resting near 422 V. Confirm the Solis input range covers that
   span with margin, so the inverter never faults before the BMS protects.
3. **Set Pylon HV on the BCU** and the matching Pylon battery type on the inverter. A mismatch
   typically presents as the inverter seeing no battery at all.
4. **Let BMU1's imbalance settle.** 228 mV against a 400 mV alarm is workable but tight.

### One asymmetry worth planning around

The inverter link is CAN-3 and the monitoring link is RS485-1 — separate transports on separate
pins. They do not contend, so Home Assistant can keep polling over RS485 while the inverter talks
CAN. Confirm in practice once both are live, but by the pinout there is no reason it should not.

---

*Compiled from two frame captures of the vendor tool (894 and 4915 frames, all CRC-verified),
the application's own SQLite parameter and protocol databases, the Seplos HV Master Control Box
specification, and live reads from the unit. Undecoded fields are marked as such rather than guessed.*

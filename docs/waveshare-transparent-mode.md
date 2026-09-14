# Waveshare RS232/485/422 TO POE ETH (B) — transparent mode for the Seplos BCU

The BCU on RS485-1 speaks a proprietary `9A … 9D` frame protocol, **not Modbus**. The gateway
must therefore be a dumb serial-to-TCP pipe. Waveshare calls this the default *transparent
transmission* mode; the Modbus TCP↔RTU conversion you enabled for the Solis unit must be **off**
on this one.

Unit: reserve gateway, firmware V1.452, MAC `28-7A-E3-B3-E8-4D`, last known IP
`192.168.157.155` (mask 255.255.255.0, gw 192.168.157.3).

## Wiring (unchanged from 2026-07-13)

| BCU connector ⑤ pin | Signal   | Gateway terminal |
|---------------------|----------|------------------|
| 1                   | RS485-A1 | **TA**           |
| 2                   | RS485-B1 | **TB**           |
| 3                   | GND_A1   | GND              |

Twisted pair for TA/TB. Onboard 120 Ω termination jumper: **NC** for a short run. If the link is
silent, swap TA/TB first.

## Settings in the web UI (fields as labelled in V1.452)

| Field | Value | Why |
|---|---|---|
| IP mode | Static (or DHCP reservation on the MAC) | HA dials in by IP |
| IP address | `192.168.157.155` | keep the existing address |
| **Work Mode** | **TCP Server** | HA is the client |
| **Protocol** (conversion) | **None** | transparent pipe — NOT "Modbus TCP to RTU" |
| Device Port (local port) | **8899** | any free port; 502 would suggest Modbus, so avoid it |
| Baud Rate | **57600** | BCU RS485-1 default |
| Data bits / Parity / Stop | **8 / None / 1** | |
| Flow control | None | |
| Instruction Time out | 0 (only relevant in Modbus mode) | |
| Multi-host | off/disabled if visible (only relevant in Modbus mode) | one client: HA |
| Packet/frame settings ("Pack Time"/"Pack Length" if present) | Pack Time 10 ms or lower, Pack Length 0 (auto) | the integration re-frames by length, so segmentation is harmless; smaller is faster |
| Heartbeat / keepalive | default | |

Save and restart the gateway. A **Modbus gateway option left on will make the link look dead**:
in conversion mode the gateway drops every reply whose bytes are not a valid Modbus frame, which is
every reply the BCU sends.

## Verify from the Linux VM (no HA needed)

```bash
cd "/home/blc/Claude/Projects/Integrate Seplos BMS data into HA/ha-seplos-hv"
python3 tools/bcu_cli.py --host 192.168.157.155 --port 8899
```

Expected: identity strings (`HV-PACE-ALL-CA-DATA-V0.22`, `HVP-B1018-30443-1.04`, serial
`304431553990004P`), the pack summary, four modules with 32 cells each and the temperature table.

If it times out:
1. `nc -vz 192.168.157.155 8899` — port must be open. Otherwise Work Mode / port / IP is wrong.
2. Port open but no reply: Protocol is still Modbus, wrong baud, or TA/TB swapped.
3. Only one TCP client at a time in transparent mode. Close the CLI before HA polls, and vice
   versa (HA holds the connection open).

## Solis gateway is untouched

The Solis inverters keep their own Waveshare at `192.168.43.77:502` in Modbus TCP↔RTU mode. The
two serial buses run different protocols and baud rates and cannot share a gateway.

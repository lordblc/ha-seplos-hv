# Seplos HV BMS

Read-only monitoring of a **Seplos HV Master Control Box (BCU-1002C)** over its proprietary
RS485-1 protocol: pack voltage/current/SOC/SOH, every cell voltage, every temperature sensor,
relay states, protection limits and derived health numbers such as cell spread and headroom
to the alarm thresholds.

Connect through a serial-to-Ethernet gateway in **transparent** mode (e.g. Waveshare
RS232/485/422 TO POE ETH (B)) or a local USB RS485 adapter. Nothing is ever written to the BMS.

Add it under **Settings → Devices & Services → Add Integration → Seplos HV BMS**.

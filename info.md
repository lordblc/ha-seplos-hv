# Seplos HV BMS

Read-only monitoring of a **Seplos HV Master Control Box (BCU-1002C)** over its proprietary
RS485-1 protocol: pack voltage/current/SOC/SOH, every cell voltage, every temperature sensor,
relay states, protection limits and derived health numbers such as cell spread and headroom
to the alarm thresholds.

Connect through a serial-to-Ethernet gateway in **transparent** mode (e.g. Waveshare
RS232/485/422 TO POE ETH (B)) or a local USB RS485 adapter. Read-only by default.

Since v0.4.0 an off-by-default **"Enable parameter writes"** option adds a guarded,
dry-run-first way to change a protection parameter's trip/recover threshold or delay (a
`write_param` service plus disabled-by-default number entities). The write frame has never
been confirmed on the wire for this command family — read the write-up in
`docs/protocol-reference.md` before turning this on; it defaults to reporting the frame
instead of sending it, range-checks every value, and caps a single step to 20% unless
overridden.

Add it under **Settings → Devices & Services → Add Integration → Seplos HV BMS**.

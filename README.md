# nelko-spp-bridge

Print from CUPS to a Bluetooth label printer (Nelko PL70e and similar 4x6
thermal printers) *fast* — by using the transport the vendor's Windows driver
actually uses.

**Result on a Nelko PL70e: ~6 seconds per 4x6 label instead of ~29s.**

## Why this exists

"Bluetooth" label printers expose two radios:

- **BLE GATT** — what Linux Bluetooth stacks prefer. The Nelko PL70e's print
  data channel is write-with-response at a tiny ATT MTU, so CUPS-class raster
  jobs (typically 200 KB+) trickle out at **~6 KB/s → ~29 s per label**. There
  is no way to fix that from userspace; it is the controller/stack ceiling.
- **Classic Bluetooth SPP** (Serial Port Profile) — a virtual COM port. The
  Windows driver uses this and it sustains **~40 KB/s** with zero per-chunk
  acknowledgement overhead.

Linux/CUPS has no built-in Bluetooth backend, so this daemon bridges the gap:

```
CUPS (serial:/dev/nelko-pl70e)  ->  pseudo-terminal  ->  SPP/RFCOMM socket  ->  printer
```

## Requirements

- Arch / Omarchy (or any system with `bluez`, a BR/EDR-capable adapter, and
  Python 3 — stdlib only, no pip packages)
- A printer that advertises the Serial Port UUID `00001101-...`

## Install (manual)

```sh
sudo install -D -m 755 nelko_spp_bridge.py /usr/lib/nelko/nelko_spp_bridge.py
sudo install -D -m 644 systemd/nelko-spp-bridge.service /usr/lib/systemd/system/nelko-spp-bridge.service
sudo install -D -m 644 config/nelko-spp-bridge.conf.example /etc/nelko-spp-bridge.conf
sudo systemctl daemon-reload
sudo systemctl enable --now nelko-spp-bridge
```

Arch/Omarchy users can instead install from the AUR (see `aur/`).

## Configure

1. **Pair the printer** (classic side). Most such printers are "Just Works"
   pairing — no PIN:

   ```sh
   bluetoothctl
   scan on
   # wait until you see the printer's name (e.g. PL70e-BT_5D17), note the
   # address WITHOUT "-LE" in the name (that is the BLE one)
   pair <CLASSIC-ADDRESS>
   scan off
   ```

2. Edit `/etc/nelko-spp-bridge.conf`:

   - `PRINTER_ADDRESS` — the classic (BR/EDR) address.
   - `SPP_CHANNEL` — almost always `1`.
   - `PTY_SYMLINK` — leave as `/dev/nelko-pl70e`.

3. Restart the service and check the log:

   ```sh
   sudo systemctl restart nelko-spp-bridge
   journalctl -u nelko-spp-bridge -f
   # expect: [spp] connected
   ```

## CUPS setup

Create a queue that treats the bridge device as a serial printer:

```sh
sudo lpadmin -p Nelko-PL70e -E \
  -v 'serial:/dev/nelko-pl70e?baud=115200&parity=none' \
  -P /usr/share/cups/model/<your-printer.ppd>
```

For the Nelko PL70e, install the vendor driver (ships a `shippingprinter`
raster-to-TSPL filter + PPD) and keep the printer at **203x203 dpi** — lower
resolutions break required barcode scanning density. For other printers,
any serial-capable PPD (or a raw queue feeding TSPL) works: the bridge is
transport-agnostic.

Then print a 4x6 PDF from any app and select the Nelko queue. Confirm real
throughput in the bridge log:

```
[spp] burst end: 110844 bytes in 2.7s (39.6 KB/s)
```

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `[spp] connect failed: Device or resource busy` | transient; service retries every 2s |
| connects, but nothing prints | wrong RFCOMM channel — the printer has a fast data sink that accepts bytes but isn't wired to the print engine. Try `SPP_CHANNEL=2..10`, then power-cycle the printer |
| no `[spp] connected` after pairing | classic side not connectable; power-cycle the printer, re-run `bluetoothctl connect <ADDR>` once manually |
| labels slow again | something connected via BLE; the BLE path on the same printer is genuinely ~6 KB/s |

## Did you know

On the Nelko PL70e the BLE WRWR channel accepts ~48 KB/s but never prints; only
the acknowledged channel drives the engine. The "fast channel" rabbit hole is a
dead end — classic SPP is the answer on these devices.

## License

MIT
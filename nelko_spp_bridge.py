#!/usr/bin/env python3
"""
nelko-spp-bridge - expose a classic Bluetooth Serial Port (SPP) label printer
as a local pseudo-terminal that CUPS can use as a serial device.

Why this exists
---------------
Many label printers (Nelko PL70e, and comparable 4x6 thermal units from
MUNBYN/Rollo/etc.) are "Bluetooth" printers whose Windows driver connects over
classic Bluetooth Serial Port Profile (SPP) as a virtual COM port. That path
sustains tens of KB/s. The other common path - BLE GATT - is often firmware
capped to a few KB/s (acknowledged writes, tiny ATT MTU), which is why labels
take ~30s instead of a few seconds.

Linux/CUPS has no native Bluetooth backend, so this daemon bridges:

    CUPS serial device  ->  pseudo-terminal  ->  SPP/RFCOMM socket (printer)

Configuration
-------------
All settings are optional; defaults apply. Create /etc/nelko-spp-bridge.conf
with KEY=value lines, or export the environment variables:

    PRINTER_ADDRESS    BT classic address of the printer (e.g. DC:1D:30:58:5D:17)
    SPP_CHANNEL        RFCOMM channel on the printer (usually 1)
    PTY_SYMLINK        stable device path for CUPS (default /dev/nelko-pl70e)
    CUPS_UID           uid to own the pty slave so the CUPS backend can open it
                       (default 209 = the cups user on Arch/Omarchy; 0 disables)

Measure printed throughput with: journalctl -u nelko-spp-bridge (look for
"[spp] burst end: <N> bytes in <T>s (<K> KB/s)" lines).
"""

import fcntl
import os
import pty
import select
import signal
import socket
import termios
import threading
import time
import sys

DEFAULTS = {
    "PRINTER_ADDRESS": "DC:1D:30:58:5D:17",
    "SPP_CHANNEL": "1",
    "PTY_SYMLINK": "/dev/nelko-pl70e",
    "CUPS_UID": "209",
}

CONFIG_PATHS = ["/etc/nelko-spp-bridge.conf", "/usr/local/etc/nelko-spp-bridge.conf"]

MAX_BUFFER = 4 * 1024 * 1024


def load_config():
    cfg = dict(DEFAULTS)
    for path in CONFIG_PATHS:
        try:
            with open(path) as f:
                for raw in f:
                    line = raw.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    cfg[k.strip()] = v.strip()
        except OSError:
            pass
    for k in DEFAULTS:
        if k in os.environ and os.environ[k]:
            cfg[k] = os.environ[k]
    return cfg


class Bridge:
    def __init__(self, cfg):
        self.addr = cfg["PRINTER_ADDRESS"]
        self.channel = int(cfg["SPP_CHANNEL"])
        self.symlink = cfg["PTY_SYMLINK"]
        self.cups_uid = int(cfg["CUPS_UID"])
        self.lock = threading.Lock()
        self.connected = False
        self.stop = False
        self.sock = None
        self.master_fd = None
        self.slave_path = None
        self.slave_keep = None

    # ---------- pty setup ----------
    def setup_pty(self):
        master, slave = pty.openpty()
        self.master_fd = master
        self.slave_path = os.ttyname(slave)
        # keep a dup of the slave open so the pair stays active and the master
        # never sees EIO from a closed writer
        self.slave_keep = os.dup(slave)
        attrs = termios.tcgetattr(slave)
        attrs[0] = termios.IGNPAR      # raw-ish input
        attrs[1] = 0
        attrs[2] = termios.CS8 | termios.CREAD
        attrs[3] = 0
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(slave, termios.TCSANOW, attrs)
        try:
            fcntl.ioctl(slave, termios.TIOCSETCF, None)
        except Exception:
            pass
        if self.cups_uid > 0:
            try:
                os.chown(self.slave_path, self.cups_uid, self.cups_uid)
                os.chmod(self.slave_path, 0o660)
            except OSError as e:
                print(f"[spp] chown note: {e}", flush=True)
        os.close(slave)
        try:
            os.unlink(self.symlink)
        except FileNotFoundError:
            pass
        os.symlink(self.slave_path, self.symlink)
        print(f"[spp] pty master={self.master_fd} slave={self.slave_path} -> {self.symlink}",
              flush=True)
        fl = fcntl.fcntl(self.master_fd, fcntl.F_GETFL)
        fcntl.fcntl(self.master_fd, fcntl.F_SETFL, fl | os.O_NONBLOCK)

    # ---------- SPP ----------
    def connect_spp(self):
        with self.lock:
            if self.sock:
                try:
                    self.sock.close()
                except Exception:
                    pass
                self.sock = None
            try:
                s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM,
                                  socket.BTPROTO_RFCOMM)
                s.settimeout(15)
                s.connect((self.addr, self.channel))
                s.settimeout(None)
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 256 * 1024)
                except Exception:
                    pass
                self.sock = s
                self.connected = True
                print("[spp] connected", flush=True)
                return True
            except Exception as e:
                print(f"[spp] connect failed: {e}", flush=True)
                self.connected = False
                self.sock = None
                return False

    def send(self, data):
        with self.lock:
            s = self.sock
        if s is None:
            return False
        try:
            s.sendall(data)
            return True
        except Exception as e:
            print(f"[spp] send error: {e}", flush=True)
            self.connected = False
            try:
                s.close()
            except Exception:
                pass
            with self.lock:
                if self.sock is s:
                    self.sock = None
            return False

    # ---------- pty <-> SPP pump ----------
    def reader_loop(self):
        """Drain pty master -> SPP socket; also drain printer->host so the
        remote transmit window never seals up. Reconnect on socket errors."""
        buf = b""
        last_activity = time.time()
        burst = 0
        self._burst_t0 = time.time()
        while not self.stop:
            with self.lock:
                s = self.sock
            if s is None:
                time.sleep(0.2)
                continue
            try:
                r, _, x = select.select([self.master_fd, s], [], [s], 0.5)
            except OSError:
                time.sleep(0.2)
                continue
            now = time.time()
            # report throughput whenever the printer stops draining for ~1s
            if burst and now - last_activity > 1.0:
                dt = max(now - self._burst_t0, 0.001)
                print(f"[spp] burst end: {burst} bytes in {dt:.1f}s "
                      f"({burst / 1024 / dt:.1f} KB/s)", flush=True)
                burst = 0
            if s in x:
                self.connected = False
                print("[spp] socket error/hup", flush=True)
                try:
                    s.close()
                except Exception:
                    pass
                with self.lock:
                    if self.sock is s:
                        self.sock = None
                continue
            if self.master_fd in r:
                try:
                    data = os.read(self.master_fd, 16384)
                except BlockingIOError:
                    data = b""
                except OSError as e:
                    if e.errno in (5, 22):
                        buf = b""
                    data = b""
                if data and self.connected:
                    self._burst_t0 = time.time() if not burst else self._burst_t0
                    burst += len(data)
                    last_activity = time.time()
                    if not self.send(data):
                        buf += data
                elif data:
                    buf += data
                    if len(buf) > MAX_BUFFER:
                        buf = buf[-MAX_BUFFER:]
            if s in r:
                try:
                    s.recv(4096)
                except Exception:
                    pass
        print("[spp] reader stopped", flush=True)

    def watchdog(self, interval):
        if not self.connected:
            self.connect_spp()
        return True


def main():
    cfg = load_config()
    print(f"[spp] config: addr={cfg['PRINTER_ADDRESS']} "
          f"channel={cfg['SPP_CHANNEL']} pty={cfg['PTY_SYMLINK']}", flush=True)

    from gi.repository import GLib
    mainloop = GLib.MainLoop()

    b = Bridge(cfg)
    b.setup_pty()
    b.connect_spp()
    t = threading.Thread(target=b.reader_loop, daemon=True)
    t.start()

    GLib.timeout_add(2000, b.watchdog, 2000)

    def on_sig(*_a):
        b.stop = True
        mainloop.quit()

    signal.signal(signal.SIGTERM, on_sig)
    signal.signal(signal.SIGINT, on_sig)
    mainloop.run()
    print("[spp] exiting", flush=True)


if __name__ == "__main__":
    main()
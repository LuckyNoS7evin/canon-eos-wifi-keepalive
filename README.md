# canon-eos-wifi-keepalive

Keeps a Canon EOS camera awake over Wi-Fi on Linux by holding a PTP/IP "remote control" session open, as EOS
Utility does on Windows and macOS.

Use it when the camera is an HDMI video source. The Canon EOS M50, for example, powers off after about 30 minutes
even with auto power off disabled, unless a computer is connected.

Needs Python 3 only (standard library).

## How it works
- **Find the camera.** The tool looks for Canon EOS cameras on the LAN with an SSDP search for
  `ICPO-WFTEOSSystemService`, or uses `--camera IP`.
- **Connect.** It opens a PTP/IP session (TCP 15740) with a fixed client GUID. The GUID is the pairing key and is
  stored in `~/.config/eos-wifi-keepalive/guid`.
- **Keep it awake.** It switches the camera to remote/event mode, polls `GetEvent` every second and sends
  `KeepDeviceOn` every minute.
- **Recover.** It reconnects automatically after drops, and closes the session cleanly on stop.

No firewall changes are needed on Fedora Workstation: the SSDP replies arrive on a high port.

## First-time pairing
1. On the camera, open Wi-Fi, choose the computer connection (EOS Utility), and start the search for a computer
   so the camera is waiting and discoverable.
2. Run `./eos_wifi_keepalive.py -v`.
3. When the camera asks to pair or connect, press **OK**.

## Run at login
```sh
./install.sh                                  # systemd user service
journalctl --user -u eos-wifi-keepalive -f    # logs
```

## Options
- `--camera IP`: skip discovery.
- `--no-remote-mode`: skip `SetRemoteMode`. Try this if HDMI output changes while connected.
- `--once`: exit instead of reconnecting.

## Tests
`python3 tests/test_fake_camera.py` runs the tool against a fake PTP/IP camera on localhost.

## Status
- Confirmed on an EOS M50 (2026-09-26): discovery, pairing, and keep-awake. Without the tool the camera used to sleep
  after about 5 minutes; with it, it stays on.
- Once paired, the camera accepts reconnections without asking again (confirmed via the service).
- Not yet tested: runs longer than 30 minutes, and reconnecting after the camera is power-cycled.

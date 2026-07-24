# outboundswitch

A Windows packet control tool built on [WinDivert](https://reqrypt.org/windivert.html) / [PyDivert](https://github.com/ffalcinelli/pydivert) and PyQt6. Lets you selectively **block** or **delay** inbound/outbound traffic live, filtered by protocol, port, or process — with on-screen overlays and rebindable hotkeys.

Built for experimenting with your own network traffic (latency simulation, selective blocking, protocol-level testing). Not a VPN, not a firewall replacement, not for use against traffic you don't control.

## Features

- **Live traffic blocking** — toggle outbound/inbound/both traffic on or off with a hotkey
- **Live traffic delay** — hold matching packets for a configurable number of milliseconds before releasing them (latency injection for testing)
- **Filter by**:
  - Protocol (TCP / UDP / ICMP)
  - Common ports (HTTP, HTTPS, DNS) or a custom port
  - Direction (outbound only / inbound only / both)
  - A specific process by executable name (e.g. `chrome.exe`)
- **Two independent on-screen overlays** — a red "TX OFF" banner when blocking is active, and an orange "DELAY x ms" banner when delay is active — positioned so they never overlap
- **Rebindable hotkeys** — click "Edit" next to either toggle in the panel and press any key to rebind, live, no restart needed
- **Live counters** — blocked/passed packet counts and in-flight delayed packet count, so you can visually confirm the tool is actually doing something
- **Self-elevating** — prompts for admin rights on launch if not already elevated (required for the WinDivert kernel driver)

## Requirements

- Windows 10/11
- Python 3.10+ (a standard python.org install is recommended — see note below)
- Packages:
  ```bash
  pip install pydivert PyQt6 keyboard psutil
  ```

### A note on Python installs

WinDivert needs to load a signed kernel driver, which requires unrestricted filesystem/driver access. The **Microsoft Store build of Python** sandboxes the process (AppX/MSIX container) and can silently break this (`PermissionError: [WinError 5] Access is denied`) even when running as administrator.

If you hit that error, either:
- Install packages under the Store Python build consistently (this repo was last tested working that way), **or**
- Switch to the regular [python.org](https://www.python.org/downloads/) build and reinstall dependencies there instead.

Whichever interpreter you use, make sure it's the **same one** your terminal, your IDE, and your file association (double-click / Win+R) all resolve to — mismatches between "the python that has your packages" and "the python that actually runs your script" are the most common source of confusing errors here.

## Usage

Run as administrator (or let it self-elevate via the UAC prompt):

```bash
python outboundswitch.py
```

Or via Win+R:
```
python "C:\path\to\outboundswitch.py"
```

A control panel window opens with all filter options. Configure your filter (protocol, ports, direction, optional app), click **Apply Filter**, then:

- Press the **block hotkey** (default `Q`) to toggle blocking on/off — the "TX OFF" overlay appears/disappears
- Press the **delay hotkey** (default `` ` ``) to toggle packet delay on/off — the "DELAY x ms" overlay appears/disappears

Both toggles work independently — you can delay traffic without blocking it, block without delaying, or combine both.

### Rebinding hotkeys

Click **Edit** next to either hotkey in the panel, then press any key — it binds immediately, no restart required.

### Filtering to a specific app

Enter an executable name (e.g. `chrome.exe`) in the "Limit to app" field and click **Apply Filter**. A background thread polls that process's active local ports once per second via `psutil` and restricts blocking/delay to traffic on those ports.

## How it works

- [WinDivert](https://reqrypt.org/windivert.html) intercepts packets at the Windows Filtering Platform layer before they reach the normal TCP/IP stack, letting the app inspect, drop, or reinject them.
- **Blocking**: matching packets are simply never reinjected (`w.send()` is skipped).
- **Delay**: matching packets are handed to a short-lived thread that sleeps for the configured duration, then sends the packet — so the main capture loop keeps processing new packets immediately instead of stalling.
- Changing the filter closes and reopens the WinDivert handle live, so updates take effect without restarting the script.

## Known limitations

- Delay does not guarantee strict packet ordering under heavy concurrent load, since each delayed packet is released independently by its own timer.
- The app-name filter matches on locally-bound ports polled once per second, so very short-lived connections may be missed between polls.
- `keyboard`-based global hotkeys require administrator privileges to hook system-wide, same as WinDivert itself.

## Disclaimer

For personal experimentation and learning on your own network traffic only. Modifying or blocking traffic on networks or systems you don't own or have explicit permission to test may violate terms of service, acceptable use policies, or local law depending on context. Use responsibly.

## License

MIT (or your preferred license — add a `LICENSE` file before publishing)

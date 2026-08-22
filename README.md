<img src="https://github.com/noitavoo/SteamyLAN/blob/main/SteamyLan/steamylan.png" align="left" width="80" alt="SteamyLAN Logo"/>

# SteamyLAN

<br clear="left"/>

SteamyLAN enables you to play LAN games with your friends! Create game servers with other Steam users without router port forwarding.
[Download the latest SteamyLAN (Windows 64bit)](https://github.com/noitavoo/SteamyLAN/releases/latest)

### Screenshots
<img width="1370" height="862" alt="image" src="https://github.com/noitavoo/SteamyLAN/blob/main/media/screenshot1.png" />

## Basic Use

### Host

Open **Create Server**, select a running program or add one manually, choose the ports to share, configure the lobby, and start hosting.

Copy the generated share code or invite players through Steam.

### Join

Open **Lobbies** and select a server, accept a Steam invite, or paste a SteamyLAN share code.

Once connected, SteamyLAN displays the local `localhost:<port>` address to use in the game.

For games whose LAN browser relies on UDP broadcasts, such as Titan Quest,
join the lobby and click **Enable LAN discovery** on the Server page. Windows
asks for administrator approval, then SteamyLAN remembers the feature for that
session. Automatic activation is available as an opt-in setting.
The elevated helper redirects broadcasts only for the exact ports in the
active lobby to `127.0.0.1`; it does not expose those ports to the physical
LAN, install a virtual adapter, or capture traffic outside those discovery
ports. The helper and its packet filter stop when you disconnect. Automatic
activation can be disabled in Settings.

Some anti-cheat or endpoint-security products may block packet-diversion
drivers. If that happens, SteamyLAN leaves the normal localhost tunnels active
and reports that LAN discovery compatibility is unavailable.

## Notes
SteamyLAN uses the Spacewar appid to function.

The **Server** page shows each member's live P2P state. A connection is ready when it reports **P2P online**; **P2P connecting** means Steam is still finding a direct or relay route, and **P2P no response** means SteamyLAN is retrying the peer session.

## Requirements

* Windows 64-bit
* Python 3.14.7+ within the Python 3.14 series
* Steam running and signed in
* Official 64-bit `steam_api64.dll`
* Dependencies from `requirements.txt`

The Windows release bundles the official x64 WinDivert 2.2.2 driver for
broadcast-only LAN discovery compatibility. Its license and attribution are
included under `third_party/windivert`.

## Run From Source

Place `steam_api64.dll` in the project directory:

```bash
pip install -r requirements.txt
python run.py
```

Or:

```bat
run.bat
```

## Build for Windows

```bat
build_windows.bat
```

Build output is created under `dist`, with `steam_api64.dll` copied into the required build locations automatically.

## Tests

```bash
python -m unittest discover -s tests -v
```

## Donate ❤

If you find SteamyLAN useful and want to support development, Bitcoin donations are appreciated.

**Bitcoin (BTC):**

```text
bc1qsp9tuke9ftw7xlr9nsndhhyl5fhu9q8a5mfvzt
```

## Disclaimer

SteamyLAN is an independent project and is not affiliated with or endorsed by Valve Corporation or Steam.

AI was used to accelerate the development of SteamyLAN.

© 2026 noitavoo. All rights reserved.

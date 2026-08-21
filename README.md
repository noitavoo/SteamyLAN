<img src="https://github.com/noitavoo/SteamyLAN/blob/main/SteamyLan/steamylan.png" align="left" width="80" alt="SteamyLAN Logo"/>

# SteamyLAN

<br clear="left"/>

SteamyLAN enables you to play LAN games with your friends! Create game servers with other Steam users without router port forwarding.
[Download SteamyLAN (Windows 64bit)](https://github.com/noitavoo/SteamyLAN/releases/latest)

### Screenshots
<img width="1370" height="862" alt="image" src="https://github.com/user-attachments/assets/63fc5387-0fec-4cef-a851-ae77e65e4aeb" />

## Basic Use

### Host

Open **Create Server**, select a running program or add one manually, choose the ports to share, configure the lobby, and start hosting.

Copy the generated share code or invite players through Steam.

### Join

Open **Lobbies** and select a server, accept a Steam invite, or paste a SteamyLAN share code.

Once connected, SteamyLAN displays the local `localhost:<port>` address to use in the game.

The **Server** page shows each member's live P2P state, round-trip ping, transfer rates, and Steam ID. A connection is ready when it reports **P2P online**; **P2P connecting** means Steam is still finding a direct or relay route, and **P2P no response** means SteamyLAN is retrying the peer session.

Chat follows new messages only while you are already at the latest message. If you scroll back, the viewport stays in place and the **Latest** button shows how many new messages arrived.

## Requirements

* Windows 64-bit
* Python 3.14.7+ within the Python 3.14 series
* Steam running and signed in
* Official 64-bit `steam_api64.dll`
* Dependencies from `requirements.txt`

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

## Donate

If you find SteamyLAN useful and want to support development, Bitcoin donations are appreciated.

**Bitcoin (BTC):**

```text
bc1qsp9tuke9ftw7xlr9nsndhhyl5fhu9q8a5mfvzt
```

## Disclaimer

SteamyLAN is an independent project and is not affiliated with or endorsed by Valve Corporation or Steam.

© 2026 noitavoo. All rights reserved.

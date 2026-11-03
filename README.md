# VizioControl

Python tools for controlling Vizio SmartCast TVs, finding their changing Chrome DevTools ports, and adding temporary image or animated GIF overlays to running web apps.

Tested on a **Vizio V505-G9** with firmware **2.600.596.0-10**. Compatibility depends on the model, firmware, and app.

![Matrix GIF overlay on the TV app](https://raw.githubusercontent.com/jsammarco/VizioControl/f69691dd72b3d1983db79eee812dedc889c2b489/MatrixGifAdded-Screenshot.PNG)

*Animated Matrix overlay. The screenshot is hosted in this repository and referenced through an immutable GitHub raw URL.*

![Meme image sent as base64 and displayed over the TV app](https://raw.githubusercontent.com/jsammarco/VizioControl/7b2b6e6666ee8c46333fff92d28ca0eb3bab3d3a/MemeAdded-Screenshot.PNG)

*A second example: the image bytes are base64-encoded on the computer and sent
inline through DevTools as an image data URL. The TV displays the temporary
overlay without fetching the original image URL. Both local files and downloaded
web images can use this path.*

## Tools

| Script | Purpose |
| --- | --- |
| `vizio.py` | Pair, read device information, control power and inputs, and send remote buttons. |
| `rediscover_devtools.py` | Find an existing DevTools listener, cache its port, and watch for changes. |
| `meme_img_plug.py` | Add/remove an image or GIF through a direct DevTools WebSocket connection, without opening Chrome. |

Use with devices and apps you own or are authorized to test. This is an independent project, not an official Vizio tool.

## Install

Requires **Python 3.9+** and network access to the TV.

```powershell
git clone https://github.com/jsammarco/VizioControl.git
cd VizioControl
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Examples use `python`; activate your environment or substitute `.\.venv\Scripts\python.exe`. The overlay requires `websocket-client`; the other scripts use the standard library.

All scripts accept `--host YOUR_TV_IP`. Override the development setup's default LAN address with your TV's address.

## Discover DevTools

Start an app or video player on the TV, then run:

```powershell
python rediscover_devtools.py --host YOUR_TV_IP --open
```

Cached and historical ports are checked first. The scanner then searches high ports before lower ones, checks each open port for actual DevTools targets immediately, and stops on the first confirmed debugger. `--open` opens its discovery page in the default browser.

```powershell
# Rediscover when an app changes ports:
python rediscover_devtools.py --host YOUR_TV_IP --watch --open

# Also repeat the historical discovery/status requests before every new search:
python rediscover_devtools.py --host YOUR_TV_IP --watch --replay --open

# Thorough full-range inventory, continuing after discovery:
python rediscover_devtools.py --host YOUR_TV_IP --full --replay

# Recheck one known port:
python rediscover_devtools.py --host YOUR_TV_IP --ports 45667
```

| Option | Behavior |
| --- | --- |
| Default fast search | 256 workers, 0.2-second TCP timeout, early exit on discovery. |
| `--full` | 128 workers, 0.5-second timeout, scans the whole requested range. |
| `--workers`, `--timeout` | Override either mode's defaults. |
| `--watch` | Poll the active debugger every two seconds; rediscover after it disappears. Ctrl+C stops. |
| `--interval` | Set the watch check interval. |
| `--replay` | SSDP discovery followed by Netflix, Cast, device, power, and AirPlay reads before each search. |
| `--label`, `--output` | Annotate reports or choose their JSON destination. |

The scanner **does not enable debugging**. Existing listeners have been observed for Paramount+, Peacock, and Vizio's web video player. Ports and target IDs can change when app instances restart; static Home may expose none. The exact trigger remains unconfirmed, and replaying status requests does not reliably create a listener.

Watch mode opens a fresh discovery page when it finds a new listener; it does not repair a disconnected inspector window. Fast successes are partial scans, explicitly recorded with `scan_complete`, `ports_scanned`, and `ports_in_range`.

## Image and GIF overlays without Chrome

```powershell
python meme_img_plug.py --host YOUR_TV_IP
python meme_img_plug.py --host YOUR_TV_IP --remove
```

Pass a local image or animated GIF to encode and display it automatically:

```powershell
python meme_img_plug.py --host YOUR_TV_IP --image-file "C:\Pictures\matrix.gif"
python meme_img_plug.py --host YOUR_TV_IP --image-file "C:\Pictures\my image.png"
```

`--image-file` supports GIF, PNG, JPEG, and WebP. It reads the file, detects the
format from its contents, base64-encodes the original bytes, and sends a data URL
directly to the TV. GIF frames and transparency are preserved; the image is not
uploaded to a hosting service or added to the repository. Choose either
`--image-file` or `--image-url`. Missing or unsupported files fail before scanning
or connecting to the TV.

The script discovers the debugger, connects directly to its page WebSocket, and inserts one uniquely identified image. It uses fixed positioning, **100% viewport height and width**, `object-fit: contain`, and **z-index 2147483647**. Pointer events pass through, and successful image loading and element dimensions are checked.

The default `IMAGE_URL` near the top of the script contains an embedded animated GIF. Accepted sources are:

- HTTP or HTTPS image URLs: downloaded on your computer and automatically encoded as base64.
- Complete data URLs such as `data:image/gif;base64,...`.
- Raw base64: validated and wrapped with the detected GIF, PNG, JPEG, or WebP MIME type.

```powershell
python meme_img_plug.py --host YOUR_TV_IP --image-url "https://example.com/image.gif"
python meme_img_plug.py --host YOUR_TV_IP --port 45667
python meme_img_plug.py --host YOUR_TV_IP --target Paramount
python meme_img_plug.py --host YOUR_TV_IP --replay
```

For long base64 values, edit the quoted `IMAGE_URL` string rather than passing it through Windows command-line arguments. Web images are downloaded on your computer, validated, base64-encoded, and sent inline to the TV, just like local files. The TV does not need access to the original image host. Original bytes, including GIF frames and transparency, are preserved.

Web downloads follow ordinary HTTP redirects, use a 20-second socket timeout and
a 20 MiB size limit, and verify HTTPS certificates normally. Browser cookies and
TV/app credentials are not sent to the image host. Use a direct image URL, not an
HTML page containing an image. GIF animation is handled by the TV browser, and
the page must allow `data:` images.

The overlay disappears on page reload/app restart. Re-running reuses its element; `--remove` removes only that element. Multiple exposed pages require `--target`. Omit `--port` to rediscover after an app restart.

Page policies can block images. CSS z-index cannot guarantee placement above native/protected video planes or browser top-layer UI. No persistent TV settings are changed.

## Pair and control the TV

Normal SmartCast control uses PIN pairing separately from DevTools discovery.

```powershell
python vizio.py --host YOUR_TV_IP pair-start
python vizio.py pair-finish
```

Read the four-digit PIN on the TV and enter it at the prompt; input is not echoed. Use `pair-cancel` to cancel a pending challenge. If the code expires, cancel and start again. Pairing can temporarily interrupt the app display.

```powershell
# Read information:
python vizio.py info
python vizio.py status
python vizio.py inputs
python vizio.py current-input
python vizio.py current-app
python vizio.py --output data\snapshot.json snapshot

# Control:
python vizio.py input HDMI-1
python vizio.py power off
python vizio.py power on
python vizio.py key volume-up
python vizio.py key mute-toggle
python vizio.py key home
python vizio.py key --help
```

Use exact input names returned by `inputs`. The script validates names and reads a fresh input hash before writing. Power-on requires network standby/Quick Start support; the script does not change that setting automatically.

Global `vizio.py` options precede the command. The paired host is saved; changing it clears the previous token and challenge. `get /relative/path` reads known API endpoints. `snapshot` reads device, power, input, app, audio, system, and Cast metadata, recording unsupported sections as errors.

## Local data and credentials

These locations are excluded from Git:

| Location | Contents |
| --- | --- |
| `.local/tv.json` | Plaintext pairing credential/pending challenge. |
| `.local/devtools-ports.json` | Per-TV debugger-port cache. |
| `data/` | Device snapshots and scan reports. |
| `*.log`, `.venv/`, `__pycache__/` | Logs, local environments, and Python cache files. |

Do not upload these files or unredacted app console/network exports. Logs can contain session tokens; snapshots can contain device identifiers. Token/password/secret fields are redacted in `vizio.py` output, but not every report or third-party app log is safe to share.

The TV's vendor HTTPS certificate does not match its LAN IP. The local API client uses a dedicated context with certificate verification disabled. HTTP requests bypass machine-wide proxies; browser and OS security settings are unchanged.

## Troubleshooting

| Symptom | What to check |
| --- | --- |
| No debugger | TV on, app/video player running, correct address. Try `--full`; the current state may expose no listener. |
| Inspector disconnected | App instances may restart with a new port and target ID. Rediscover or use watch mode. |
| HTTP 403 for inputs/settings | Complete PIN pairing for normal control. |
| Image fails to load | Source format, network access, and the page's image policy. |
| Black video in the preview | Native/protected video may be absent from DevTools screenshots even while it plays on the TV. |

## Tests

```powershell
python -m unittest -v
```

Tests cover pairing prerequisites, input validation/hashes, redaction, cached/early-exit discovery, watch replay/reconnection, CDP response handling, target selection, and image validation. They do not send live control commands or modify a TV.

## References

- [SmartCast API research](https://github.com/exiva/Vizio_SmartCast_API)
- [pyvizio](https://github.com/raman325/pyvizio)
- [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/)

# open-lights

Local control for Govee lights, without the Govee app. It's one Python server with **no dependencies** that talks to your lights over the LAN.

- **Fast LAN control:** power, brightness, colour and white temperature in about 50 ms. Lights that don't support LAN control fall back to the Govee cloud API.
- **Rooms and group controls**, plus soft fades on everything: no white flashes and no hard cuts.
- **Effects** streamed at 50 fps to segmented lights, such as the H60B2 floor lamp (3 segments) and the H6046 TV bars (10). For lights you'd rather keep on Govee's own animations, each effect maps to a matching built-in Govee scene.
- **Modes, a weekly schedule and overrides.** Any setup can be captured as a mode. A manual override lasts until the next scheduled change.
- **Watching (screen sync):** the lights follow your monitor. Each light samples its own spot on the screen based on where it sits in the room.
- **Phone app:** an installable web app at `/m`.
- **Stream Deck and automation hooks:** plain GET URLs for cycling or toggling modes.

## Quick start

1. In the Govee app, open each light's settings and turn on **LAN Control** for every light that offers it.
2. Run the server on any machine on the same network as the lights:

   ```sh
   python3 server.py            # http://127.0.0.1:8420
   python3 server.py --host 0.0.0.0   # reachable from other devices on your network
   ```

3. Open the page and press **Scan**. Then name your lights and assign them to rooms.

**Optional: lights without LAN Control.** Get an API key in the Govee app (Profile → Settings → Apply for API Key). Put it in `.env` as `GOVEE_API_KEY=...`, either next to `server.py` or in `~/.env`; see `.env.example`.

Your lights, modes and schedule are saved to `devices.json` and `modes.json`. Neither file is tracked by git.

## Running it on a Raspberry Pi

```sh
git clone <this repo> ~/open-lights
sudo cp ~/open-lights/deploy/open-lights.service /etc/systemd/system/   # edit User and paths first
sudo systemctl enable --now open-lights
```

To reach it from your phone anywhere, use [Tailscale](https://tailscale.com):

```sh
sudo tailscale serve --bg 8420
```

This gives you `https://<pi>.<tailnet>.ts.net`. Open `/m` on your phone and choose **Add to Home Screen** or **Install**.

## Watching (screen sync)

Each light picks a **screen zone** on its card: left, right, top, bottom, sides, left-half, right-half or all.

- Edge zones sample about 100 px inside the edge, so taskbars and window borders don't leak in.
- Colours are chosen by **dominant hue** rather than a plain average, which would turn everything grey.
- Blacks are crushed, so a dark screen means dark lights.

Screen capture is Windows only and uses ctypes GDI, so still no dependencies. Run the server and capture in one of two ways:

- **Server on the same Windows PC:** it samples the screen itself.
- **Server on a Pi (or anywhere else):** run the screen agent on the PC. It sits idle, and streams a 64×36 grid only while Watching is active:

  ```sh
  pythonw screen_agent.py --server https://<pi>.<tailnet>.ts.net --log screen_agent.log
  ```

  To start it automatically, add it to Task Scheduler with the trigger "At log on".

## Stream Deck and automation

Plain GET requests, so they work with a "Website" or "Open URL" action:

| URL | Does |
| --- | --- |
| `/api/mode/cycle?modes=office,chill,off` | steps through those modes |
| `/api/mode/toggle/watching` | toggles a mode on, or back to what was on before |
| `/api/mode/<id>` | switches to a mode |
| `/api/mode/resume` | ends the override and goes back to the schedule |

## Files

| File | What it is |
| --- | --- |
| `server.py` | HTTP server, LAN/cloud control, fader, effects engine |
| `effects.py` | Effects, plus their matching Govee scenes (reloads live while the server runs) |
| `modes.py` | Modes, schedule and overrides |
| `sync.py` | Turns screen frames into per-light colours |
| `screen.py` | Windows screen sampler (ctypes GDI) |
| `screen_agent.py` | Streams screen frames from a PC to a remote server |
| `index.html`, `m.html`, `static/` | Desktop UI, phone UI, PWA files |

## Govee LAN protocol notes

These were learned the hard way:

- **Discovery** is a multicast `scan` to `239.255.255.250:4001`. Lights listen on `4003` and reply to `4002`. VPN interfaces such as Tailscale can swallow the multicast, so the server binds discovery to the LAN interface; set `GOVEE_IFACE=192.168.x.y` to force one.
- **A light ignores `colorwc` when the value equals its stored value.** If a Govee scene is running, the light stays in that scene. The fix is to send a slightly different value first.
- **Streaming ("razer"/DreamView):** frames are `BB 00 len B0 grad n rgb… xor`, base64-encoded.
  - The H60B2 needs exactly 3 segments.
  - Re-sending the enable packet mid-stream blanks a frame.
  - About 50 fps looks smoothest.
- **To leave streaming without a flash:** fade the frames into the target colour, send `colorwc` while still streaming, then turn streaming off.
- **The H6046 stops answering `devStatus` while streaming.**

## License

MIT

# Changelog

All notable changes to open-lights. Dates are YYYY-MM-DD.

## [0.4.0] - 2026-11-25

### Added
- **Slow fades.** Schedule times and modes take a fade length, from 1 minute to 1 hour.
  - Ease into work light over 30 minutes, or dim to off over 20 at night.
  - A step's own fade still wins.
  - Picking a mode by hand stays quick unless the mode sets its own fade.
- **Live fade progress.** Lights show it on their tiles ("Fading out · 35% · 12 min"), and brightness reports where the fade actually is.
- **Interrupting a fade.** Touching a light mid-fade takes over: turning it on stops the fade, and turning it off goes off now.

### Changed
- Rooms show as tiles by default. The room map is opt-in per room.

## [0.3.0] - 2026-11-25

### Added
- **Room map.** Lights sit where they are in the room and cast glows in their real colour. Arrange mode lets you drag them into place. Positions are saved per light and shared across devices.
- **Screen sync picker.** A drawing of the monitor: pick a light, then click the part of the screen it follows.

### Changed
- The phone layout hides the room map and screen-sync setup, which are desktop-only.
- Phone polish:
  - Room headers fit on one line.
  - Settings rows give names the full width.
  - An odd last mode card spans the row.
  - Room brightness bars are tinted with the room's light.
  - "3:00 AM tomorrow" instead of the weekday.

## [0.2.0] - 2026-11-25

### Added
- **New interface: one responsive app** for desktop (sidebar) and phone (bottom tabs). The installed phone app uses the same page.
- **Live updates.** The server pushes state over server-sent events (`/api/events`), so no page polls and every change shows up everywhere at once.
- **Home:**
  - The current mode and what comes next.
  - Mode cards tinted with each mode's colours.
  - Today's schedule as a timeline.
  - Rooms of light tiles: tap to toggle, long-press for controls.
- **Controls panel** for a light or a whole room:
  - A big brightness bar.
  - Colour wheel, white-temperature strip and presets.
  - Effect cards.
  - Searchable Govee scenes.
- **Schedule page** with a week view. **Settings page** for per-light setup.

### Removed
- The separate phone page (`m.html`). `/m` now serves the app.

## [0.1.0] - 2026-11-25

First public version.

### Added
- **Control:**
  - LAN control over UDP: power, brightness, colour and white temperature. Discovery skips VPN interfaces.
  - Cloud API fallback for lights without LAN Control.
  - Rooms and group control, with soft software fades on every change.
- **Effects:**
  - Streamed at 50 fps to segmented lights.
  - Matching built-in Govee scenes for lights set to "Govee scenes only".
  - Subtle by default.
- **Modes and schedule:** modes, a weekly schedule, manual overrides, and Stream Deck-friendly GET endpoints for cycle, toggle and resume.
- **Screen sync (Watching):**
  - Per-light screen zones.
  - Dominant-hue colour picking and crushed blacks.
  - A Windows screen agent that streams to a remote server only while Watching is on.
- **Running it:**
  - Installable phone web app.
  - A systemd unit for running on a Raspberry Pi.
  - Crash-safe state files.

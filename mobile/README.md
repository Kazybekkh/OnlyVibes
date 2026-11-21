# OnlyVibes — mobile (Expo)

iOS app for the OnlyVibes hub, designed around Apple's latest design language
(**Liquid Glass**, iOS 26/27): floating glass tab-bar pill, blurred translucent
nav bars with collapsing large titles, frosted glass cards with edge light,
SF Symbols throughout, dark-mode system colors, capsule buttons.

It renders the same live snapshot as the web 3D view: one glass card per
laptop, each with its running AI agents (state chip, CPU meter, one-line
summary), the team headline, and collision alerts.

## Run it

```bash
cd mobile
npm install
npx expo start          # then press "i" for the iOS simulator (or scan QR in Expo Go)
```

The app defaults to the team hub **`http://100.107.184.10:8000`** (Kazybek's
MacBook over Tailscale, per `shared/PROTOCOL.md`). Change it in **Settings**
(any `http(s)://host:port` — the Tailscale IP of the laptop running the hub),
or toggle **Demo mode** for fake data. Override the default at start with
`EXPO_PUBLIC_SERVER_URL=http://<hub>:8000 npx expo start`.

```bash
# hub (on one laptop)
cd ../server && uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

Set a default hub without typing it in the app:

```bash
EXPO_PUBLIC_SERVER_URL=http://100.64.0.11:8000 npx expo start
```

## How it connects (orchestra protocol v2, `shared/PROTOCOL.md`)

- `WS <hub>/view` — 2 Hz `{type:"state", ts, laptops}` laptop fleet
  (`shared/schema.json` agents: name/pid/cpu/cwd/last_log/summary/kind/task).
- `GET <hub>/api/state` — polled every 2 s for the board fields
  (`projects`, `tasks`, `workers`, `messages`) and merged with the WS fleet.
- Agent `state` (working/idle/error/stuck) and the team headline are derived
  client-side from cpu + log-line changes (orchestra servers send raw agents).
- Fallback: REST-only polling when the WS can't connect. Demo mode runs fully
  offline with a fake fleet + board.

## Layout

```
mobile/
├── app/
│   ├── _layout.tsx            # root stack, dark, safe-area
│   ├── (tabs)/_layout.tsx     # floating Liquid Glass tab bar (expo-blur pill)
│   ├── (tabs)/fleet/          # Fleet: team headline + laptop cards (large title)
│   ├── (tabs)/board/          # Board: projects, tasks (status+progress), workers
│   ├── (tabs)/alerts/         # failed tasks, legacy collisions, agent messages
│   ├── (tabs)/settings/       # hub URL, demo toggle, reconnect
│   └── laptop/[key].tsx       # per-laptop detail: every agent, full log lines
├── components/                # Glass, Background, LaptopCard, AgentRow, StateChip, TeamHeader
├── lib/
│   ├── types.ts               # mirrors shared/schema.json + server snapshot()
│   ├── theme.ts               # Liquid Glass tokens (materials, radii, type, colors)
│   ├── useOnlyVibes.ts        # WS /view + REST fallback + demo hook
│   ├── server.ts              # hub URL config store (useSyncExternalStore)
│   └── demo.ts                # fake fleet (same as web fallback)
└── package.json               # Expo SDK 58, expo-router, new arch
```

## What to show in the demo

Open the app next to the web 3D view with the mock reporter running: the Fleet
tab's glass cards update live as agents start/stop on each laptop, the team
headline summarizes the fleet, and when two agents edit the same file the
Alerts tab lights up red. On iOS 26+ the tab bar floats as a glass pill and
the large title collapses into the blurred nav bar on scroll.

## Apple design resources used

- **SF Symbols** (via `expo-symbols`): on iOS these render the real system
  symbols — no bundling needed, licensed for Apple-platform apps.
- **SF Pro**: the system font — all `fontSize`/`fontWeight` styles render SF Pro
  automatically on device; `lib/theme.ts` encodes the HIG type scale (34/41
  large title … 11/13 caption2) and dark-mode system colors (`#0A84FF` accent,
  `#EBEBF5` label ramp, `rgba(84,84,88,0.6)` separator).
- **Materials**: chrome (tab bar pill, nav bars) uses Liquid Glass — native
  headers get it automatically from the iOS 26+ SDK, the tab bar is an
  expo-blur capsule; content cards use the standard *thin* material per HIG
  (Liquid Glass is for the functional/chrome layer only).
- **App icon**: full-bleed 1024×1024 PNG (`assets/icon.png`, system applies the
  squircle mask — never bake it in), generated in the iOS 27 Liquid Glass
  spirit (dark glass gradient + glows + laptop/sparkle glyph); splash, Android
  adaptive-icon layers and favicon regenerated to match.

## Notes / limits

- expo-blur approximates `UIVisualEffectView` for custom surfaces; native
  headers get real materials via the iOS 26+ SDK (`headerBlurEffect`).
- The tab-bar pill is a static capsule (React Navigation doesn't expose
  `tabBarMinimizeBehavior` minimize-on-scroll; height/safe-area are left to
  the navigator so the home-indicator inset is correct).
- Server URL isn't persisted across app restarts yet (in-memory store).

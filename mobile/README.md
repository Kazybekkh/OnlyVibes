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

In **Settings** set the hub URL (e.g. `http://100.64.0.11:8000` — the Tailscale
IP of the laptop running the server) or toggle **Demo mode** for fake data.

```bash
# hub (on one laptop)
cd ../server && uv run uvicorn main:app --host 0.0.0.0 --port 8000
```

Set a default hub without typing it in the app:

```bash
EXPO_PUBLIC_SERVER_URL=http://100.64.0.11:8000 npx expo start
```

## How it connects

- `WS <hub>/view` — the server's 2 Hz broadcast of the full snapshot
  (`shared/schema.json` laptops + `team_summary` + `alerts` + `voice`).
- Fallback: polls `GET <hub>/api/state` once per second (e.g. when the WS
  can't upgrade over some networks).
- Demo mode: built-in fake fleet, same shape as `web/index.html`'s fallback.

## Layout

```
mobile/
├── app/
│   ├── _layout.tsx            # root stack, dark, safe-area
│   ├── (tabs)/_layout.tsx     # floating Liquid Glass tab bar (expo-blur pill)
│   ├── (tabs)/fleet/          # Fleet: team headline + laptop cards (large title)
│   ├── (tabs)/alerts/         # collision alerts + voice turn
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

## Notes / limits

- expo-blur approximates `UIVisualEffectView`; on iOS 26 devices the native
  headers/tab bar still use real materials via `headerBlurEffect`.
- Native minimize-on-scroll tab pill (`tabBarMinimizeBehavior`) isn't exposed
  by the installed React Navigation version, so the pill is static.
- Server URL isn't persisted across app restarts yet (in-memory store).

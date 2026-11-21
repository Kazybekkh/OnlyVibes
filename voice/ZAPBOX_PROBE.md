# Zapbox capability probe — results

Fill in after running `/probe` in Zapbox Browser (see `voice/ZAPBOX_VOICE_PLAN.md` §2):

- iOS version: _
- Zapbox Browser version: _
- User agent: _
- `isSecureContext`: _
- Mic (`getUserMedia`) outside AR: _ ok / error _
- Mic RMS inside AR (>0 = alive): _
- Mic from `selectstart` (fresh getUserMedia): _
- Audio beep audible inside AR: _
- `immersive-ar` supported / enabledFeatures: _
- hit-test supported: _
- Gamepad button index ↔ physical button (expected xr-standard: 0 trigger, 1 grip, 3 stick, 4 A/X, 5 B/Y): _

Decision (per plan §2.4): `VOICE_MIC` default stays `browser` only if mic RMS > 0 inside AR; otherwise `web`.

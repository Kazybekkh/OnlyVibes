// iOS "Liquid Glass" (iOS 26/27 design language) tokens, dark mode.
// Approximates native UIVisualEffectView materials with expo-blur + translucency.

export const colors = {
  // app backdrop (matches web/index.html scene background)
  bg: '#0b0e14',
  bgElevated: '#101728',

  // label colors on dark backgrounds (system dark-mode values, #EBEBF5 family)
  label: '#ffffff',
  labelSecondary: 'rgba(235,235,245,0.60)',
  labelTertiary: 'rgba(235,235,245,0.30)',
  labelQuaternary: 'rgba(235,235,245,0.18)',

  // system accent + semantics (dark mode values)
  accent: '#0a84ff',
  accentSoft: 'rgba(10,132,255,0.16)',
  green: '#30d158',
  yellow: '#ffd60a',
  orange: '#ff9f0a',
  red: '#ff453a',
  gray: '#8e8e93', // systemGray, dark mode

  // glass
  glassBorder: 'rgba(255,255,255,0.10)',
  glassBorderStrong: 'rgba(255,255,255,0.18)',
  glassEdgeLight: 'rgba(255,255,255,0.16)', // inset top highlight faking the glass edge

  separator: 'rgba(84,84,88,0.60)',
} as const;

// Liquid Glass / material hierarchy (Apple HIG materials, dark mode).
// `intensity` = expo-blur 1-100, `fill` = translucent overlay under the blur
// (also the Android/web fallback where no real blur exists).
export const glass = {
  clear:   { intensity: 20, fill: 'rgba(0,0,0,0.15)' },  // over media
  regular: { intensity: 70, fill: 'rgba(28,28,30,0.45)' }, // tab bar / nav bar / toolbars
  thin:    { intensity: 35, fill: 'rgba(28,28,30,0.55)' }, // glass cards in content
  thick:   { intensity: 95, fill: 'rgba(44,44,46,0.70)' }, // sheets / popovers
} as const;

// Continuous ("concentric") corner radii, iOS 26 style
export const radius = {
  card: 28,
  sheet: 38,
  button: 20,
  pill: 999,
  iconTile: 12,
} as const;

// SF Pro text style scale — sizes/leading per HIG typography spec (iOS Large).
// Large Title renders Bold inside nav bars; weights below are the regular variants.
export const type = {
  largeTitle: { fontSize: 34, lineHeight: 41, fontWeight: '400' as const, letterSpacing: 0.36 },
  title1: { fontSize: 28, lineHeight: 34, fontWeight: '400' as const, letterSpacing: 0.34 },
  title2: { fontSize: 22, lineHeight: 28, fontWeight: '400' as const, letterSpacing: 0.35 },
  title3: { fontSize: 20, lineHeight: 25, fontWeight: '400' as const, letterSpacing: 0.38 },
  headline: { fontSize: 17, lineHeight: 22, fontWeight: '600' as const, letterSpacing: -0.41 },
  body: { fontSize: 17, lineHeight: 22, fontWeight: '400' as const, letterSpacing: -0.41 },
  callout: { fontSize: 16, lineHeight: 21, fontWeight: '400' as const, letterSpacing: -0.32 },
  subhead: { fontSize: 15, lineHeight: 20, fontWeight: '400' as const, letterSpacing: -0.24 },
  footnote: { fontSize: 13, lineHeight: 18, fontWeight: '400' as const, letterSpacing: -0.08 },
  caption1: { fontSize: 12, lineHeight: 16, fontWeight: '400' as const, letterSpacing: 0 },
  caption2: { fontSize: 11, lineHeight: 13, fontWeight: '400' as const, letterSpacing: 0.07 },
} as const;

// Per-laptop accent palette — same cycle as web/index.html LAPTOP_COLORS
export const laptopAccents = [
  '#7dd3fc', '#f0abfc', '#86efac', '#fcd34d', '#fca5a5', '#c4b5fd',
] as const;

// Per-agent colors
export const agentColors: Record<string, string> = {
  'vibe': '#7dd3fc',
  'claude-code': '#f0abfc',
  'codex': '#86efac',
  'cursor': '#fcd34d',
};
export const agentColor = (name: string) => agentColors[name] ?? '#e8ecf3';

// Per-state colors (server fill_agent() states)
export const stateColors: Record<string, string> = {
  working: colors.green,
  idle: colors.gray,
  error: colors.orange,
  stuck: colors.red,
};

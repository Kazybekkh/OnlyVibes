// iOS "Liquid Glass" (iOS 26/27 design language) tokens, dark mode.
// Approximates native UIVisualEffectView materials with expo-blur + translucency.

export const colors = {
  // app backdrop — light mode (systemGroupedBackground family)
  bg: '#f2f2f7',
  bgElevated: '#ffffff',

  // label colors, light mode (system values, #3C3C43 family).
  // Tertiary/quaternary alphas raised above Apple's 0.30/0.18 so small caption
  // text still clears ~3:1 on the light background.
  label: '#000000',
  labelSecondary: 'rgba(60,60,67,0.62)',
  labelTertiary: 'rgba(60,60,67,0.55)',
  labelQuaternary: 'rgba(60,60,67,0.38)',

  // system accent + semantics (light mode values)
  accent: '#007aff',
  accentSoft: 'rgba(0,122,255,0.12)',
  green: '#248a3d',
  yellow: '#ffcc00',
  orange: '#ff9500',
  red: '#ff3b30',
  gray: '#8e8e93',

  // glass (light)
  glassBorder: 'rgba(0,0,0,0.08)',
  glassBorderStrong: 'rgba(0,0,0,0.14)',
  glassEdgeLight: 'rgba(255,255,255,0.70)', // inset top highlight faking the glass edge

  separator: 'rgba(60,60,67,0.29)',

  // neutral fills used by meters / inputs
  track: 'rgba(0,0,0,0.08)',
  inputBg: 'rgba(0,0,0,0.05)',
} as const;

// Liquid Glass / material hierarchy (Apple HIG materials, light mode).
// `intensity` = expo-blur 1-100, `fill` = translucent overlay under the blur
// (also the Android/web fallback where no real blur exists).
export const glass = {
  clear:   { intensity: 20, fill: 'rgba(255,255,255,0.25)' }, // over media
  regular: { intensity: 70, fill: 'rgba(255,255,255,0.55)' }, // tab bar / nav bar / toolbars
  thin:    { intensity: 35, fill: 'rgba(255,255,255,0.65)' }, // glass cards in content
  thick:   { intensity: 95, fill: 'rgba(255,255,255,0.80)' }, // sheets / popovers
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

// Per-laptop accent palette — same hue cycle as web/index.html LAPTOP_COLORS,
// darkened to system-color values that keep >=4.5:1 text contrast on light bg
export const laptopAccents = [
  '#007aff', '#af52de', '#248a3d', '#c77400', '#ff3b30', '#5e5ce6',
] as const;

// Per-agent colors, keyed by base id ("claude-code · OnlyVibes › Explore" -> "claude-code")
// (darkened from the web pastels for light-mode text contrast)
export const agentColors: Record<string, string> = {
  'vibe': '#0891b2',
  'claude-code': '#9333ea',
  'codex': '#15803d',
  'cursor': '#ca8a04',
};
export const agentColor = (name: string) =>
  agentColors[name.split(/[ ·›#]/)[0].trim()] ?? colors.labelSecondary;

// Orchestra task/project status colors (light-mode readable)
export const taskStatusColors: Record<string, string> = {
  backlog: '#6e6e73',
  todo: '#007aff',
  claimed: '#c77400',
  in_progress: '#c77400',
  review: '#8944c8',
  done: '#1a7f37',
  failed: '#d70015',
  cancelled: '#6e6e73',
};
export const projectStatusColors: Record<string, string> = {
  planning: '#b25000',
  active: '#007aff',
  done: '#1a7f37',
  failed: '#d70015',
};

// Per-state colors (light-mode readable)
export const stateColors: Record<string, string> = {
  working: '#1a7f37',
  idle: '#6e6e73',
  error: '#b25000',
  stuck: '#d70015',
};

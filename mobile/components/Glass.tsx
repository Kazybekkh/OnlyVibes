// Liquid Glass container: expo-blur over a translucent fill + hairline border,
// with iOS-26 continuous-corner radius. On Android/web the BlurView degrades
// to the translucent fill, which still reads as "frosted".
import { BlurView } from 'expo-blur';
import { StyleSheet, View, type StyleProp, type ViewStyle } from 'react-native';
import { colors, glass, radius } from '../lib/theme';

export function Glass({
  children,
  style,
  material = 'thin',
  tint = 'dark',
  radius: r = radius.card,
  border = true,
}: {
  children?: React.ReactNode;
  style?: StyleProp<ViewStyle>;
  /** Liquid Glass material preset (see lib/theme.ts `glass`) */
  material?: keyof typeof glass;
  tint?: 'light' | 'dark' | 'default' | 'systemChromeMaterialDark';
  radius?: number;
  border?: boolean;
}) {
  const m = glass[material];
  return (
    <View
      style={[
        styles.wrap,
        { borderRadius: r, borderColor: border ? colors.glassBorder : 'transparent' },
        style,
      ]}
    >
      <BlurView intensity={m.intensity} tint={tint} style={[StyleSheet.absoluteFill, { borderRadius: r }]} />
      <View style={[StyleSheet.absoluteFill, { borderRadius: r, backgroundColor: m.fill }]} />
      {/* inset top highlight — the "edge light" that makes glass read as glass */}
      <View style={[styles.edge, { borderRadius: r }]} pointerEvents="none" />
      <View style={[styles.content, { borderRadius: r }]}>{children}</View>
    </View>
  );
}

const styles = StyleSheet.create({
  wrap: {
    borderWidth: StyleSheet.hairlineWidth,
    overflow: 'hidden',
  },
  edge: {
    position: 'absolute',
    top: 0, left: 0, right: 0, height: 1,
    backgroundColor: colors.glassEdgeLight,
  },
  content: {
    overflow: 'hidden',
  },
});

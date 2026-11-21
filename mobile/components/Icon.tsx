// SF Symbol on iOS, matching Material Symbol on Android/web (expo-symbols
// renders nothing off-iOS unless given a per-platform name).
import { SymbolView, type SFSymbol, type SymbolViewProps } from 'expo-symbols';

type AndroidName = Extract<SymbolViewProps['name'], { android?: unknown }>['android'];

const MATERIAL: Partial<Record<SFSymbol, AndroidName>> = {
  'laptopcomputer': 'laptop_mac',
  'rectangle.grid.1x2.fill': 'view_agenda',
  'exclamationmark.triangle.fill': 'warning',
  'gearshape.fill': 'settings',
  'bolt.fill': 'bolt',
  'pause.fill': 'pause',
  'exclamationmark.circle.fill': 'error',
  'flame.fill': 'local_fire_department',
  'arrow.clockwise': 'refresh',
  'checkmark.shield': 'verified_user',
  'xmark.octagon.fill': 'cancel',
  'sparkles': 'auto_awesome',
  'asterisk.circle': 'auto_awesome',
  'chevron.left.forwardslash.chevron.right': 'code',
  'cursorarrow.rays': 'ads_click',
};

export function Icon({ name, size, color }: { name: SFSymbol; size: number; color: string }) {
  const m = MATERIAL[name];
  return <SymbolView name={{ ios: name, android: m, web: m }} size={size} tintColor={color} />;
}

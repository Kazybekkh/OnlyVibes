// Pill chip for an agent's server-computed state (working / idle / error / stuck).
import { SymbolView, type SFSymbol } from 'expo-symbols';
import { StyleSheet, Text, View } from 'react-native';
import { colors, stateColors, type } from '../lib/theme';
import type { AgentState } from '../lib/types';

const ICONS: Record<AgentState, SFSymbol> = {
  working: 'bolt.fill',
  idle: 'pause.fill',
  error: 'exclamationmark.circle.fill',
  stuck: 'flame.fill',
};

export function StateChip({ state }: { state: AgentState | undefined }) {
  const s = state ?? 'idle';
  const c = stateColors[s] ?? colors.gray;
  return (
    <View style={[styles.chip, { backgroundColor: `${c}26` }]}>
      <SymbolView name={ICONS[s]} size={11} tintColor={c} />
      <Text style={[styles.label, { color: c }]}>{s}</Text>
    </View>
  );
}

const styles = StyleSheet.create({
  chip: {
    flexDirection: 'row',
    alignItems: 'center',
    gap: 4,
    paddingHorizontal: 8,
    paddingVertical: 3,
    borderRadius: 999,
  },
  label: {
    ...type.caption2,
    textTransform: 'uppercase',
  },
});

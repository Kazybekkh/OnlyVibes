// Laptop detail — every agent on the machine, with full log lines.
import { Stack, useLocalSearchParams } from 'expo-router';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { AgentRow } from '../../components/AgentRow';
import { Background } from '../../components/Background';
import { Glass } from '../../components/Glass';
import { colors, laptopAccents, type } from '../../lib/theme';
import { laptopKey } from '../../lib/types';
import { useServerConfig } from '../../lib/server';
import { useOnlyVibes } from '../../lib/useOnlyVibes';

export default function LaptopDetail() {
  const { key } = useLocalSearchParams<{ key: string }>();
  const cfg = useServerConfig();
  const { snapshot } = useOnlyVibes(cfg.url, cfg.demo, cfg.nonce);
  const laptop = (snapshot?.laptops ?? []).find((l) => laptopKey(l) === key);

  if (!laptop) {
    return (
      <View style={styles.root}>
        <Background />
        <Stack.Screen options={{ title: 'Not found' }} />
        <Text style={styles.missing}>This laptop went stale or is no longer reporting.</Text>
      </View>
    );
  }

  const idx = (snapshot?.laptops ?? []).findIndex((l) => laptopKey(l) === key);
  const accent = laptopAccents[(idx < 0 ? 0 : idx) % laptopAccents.length];

  return (
    <View style={styles.root}>
      <Background />
      <Stack.Screen options={{ title: laptop.laptop, headerTintColor: colors.label }} />
      <ScrollView
        style={styles.scroll}
        contentInsetAdjustmentBehavior="automatic"
        contentContainerStyle={styles.content}
        showsVerticalScrollIndicator={false}
      >
        <Glass style={styles.card}>
          <Text style={[styles.name, { color: accent }]}>{laptop.laptop}</Text>
          <Text style={styles.meta}>owner {laptop.owner}</Text>
          <Text style={styles.meta}>tailscale {laptop.tailscale_ip}</Text>
          <Text style={styles.meta}>
            last report {laptop.ts ? new Date(laptop.ts * 1000).toLocaleTimeString() : '—'}
          </Text>
        </Glass>

        <Text style={styles.sectionTitle}>Agents ({laptop.agents?.length ?? 0})</Text>
        <Glass style={[styles.card, styles.list]}>
          {(laptop.agents ?? []).length === 0 && (
            <Text style={styles.empty}>No agents running on this laptop right now.</Text>
          )}
          {(laptop.agents ?? []).map((a, i) => (
            <View key={`${a.name}:${a.pid}`} style={i > 0 && styles.divider}>
              <AgentRow agent={a} showLog />
            </View>
          ))}
        </Glass>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  scroll: { flex: 1 },
  content: { paddingHorizontal: 16, paddingBottom: 60, gap: 12 },
  card: { padding: 16, gap: 8 },
  list: { paddingVertical: 6, gap: 0 },
  name: { ...type.title2, fontWeight: '700' },
  meta: { ...type.subhead, color: colors.labelSecondary },
  sectionTitle: { ...type.title2, fontWeight: '700', color: colors.label, marginTop: 8, paddingHorizontal: 4 },
  divider: { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.separator },
  empty: { ...type.subhead, color: colors.labelTertiary },
  missing: { ...type.subhead, color: colors.labelSecondary, textAlign: 'center', marginTop: 60, paddingHorizontal: 30, lineHeight: 22 },
});

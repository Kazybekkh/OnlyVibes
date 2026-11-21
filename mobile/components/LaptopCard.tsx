// Glass card for one laptop on the Fleet tab. Tapping opens /laptop/[key].
import { Link } from 'expo-router';
import { SymbolView } from 'expo-symbols';
import { Pressable, StyleSheet, Text, View } from 'react-native';
import { colors, laptopAccents, radius, type } from '../lib/theme';
import { laptopKey, type Laptop } from '../lib/types';
import { AgentRow } from './AgentRow';
import { Glass } from './Glass';

export function LaptopCard({ laptop, index }: { laptop: Laptop; index: number }) {
  const accent = laptopAccents[index % laptopAccents.length];
  const agents = laptop.agents ?? [];
  const busy = agents.filter((a) => a.state === 'working' || a.state === 'stuck' || a.state === 'error').length;
  return (
    <Link href={`/laptop/${encodeURIComponent(laptopKey(laptop))}`} asChild>
      <Pressable style={({ pressed }) => [{ opacity: pressed ? 0.75 : 1 }, styles.press]}>
        <Glass style={styles.card}>
          <View style={styles.head}>
            <View style={[styles.iconTile, { backgroundColor: `${accent}22` }]}>
              <SymbolView name="laptopcomputer" size={18} tintColor={accent} />
            </View>
            <View style={styles.headText}>
              <Text style={styles.name} numberOfLines={1}>{laptop.laptop}</Text>
              <Text style={styles.sub} numberOfLines={1}>
                {laptop.owner} · {laptop.tailscale_ip}
              </Text>
            </View>
            <View style={[styles.countPill, { backgroundColor: `${accent}1f` }]}>
              <Text style={[styles.count, { color: accent }]}>
                {agents.length} agent{agents.length === 1 ? '' : 's'}{busy ? ` · ${busy} active` : ''}
              </Text>
            </View>
          </View>
          <View style={styles.agents}>
            {agents.length === 0 && (
              <Text style={styles.idle}>No agents running — laptop idle</Text>
            )}
            {agents.slice(0, 3).map((a) => (
              <AgentRow key={`${a.name}:${a.pid}`} agent={a} />
            ))}
            {agents.length > 3 && (
              <Text style={styles.more}>+{agents.length - 3} more — tap to view</Text>
            )}
          </View>
        </Glass>
      </Pressable>
    </Link>
  );
}

const styles = StyleSheet.create({
  press: { borderRadius: radius.card },
  card: { padding: 16 },
  head: { flexDirection: 'row', alignItems: 'center', gap: 12 },
  iconTile: {
    width: 40, height: 40, borderRadius: 12,
    alignItems: 'center', justifyContent: 'center',
  },
  headText: { flex: 1 },
  name: { ...type.title3, fontWeight: '600', color: colors.label },
  sub: { ...type.caption1, color: colors.labelSecondary, marginTop: 2 },
  countPill: { paddingHorizontal: 10, paddingVertical: 5, borderRadius: 999 },
  count: { ...type.caption2 },
  agents: { marginTop: 8, gap: 2, borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.separator, paddingTop: 4 },
  idle: { ...type.subhead, color: colors.labelTertiary, paddingVertical: 10 },
  more: { ...type.caption1, color: colors.labelSecondary, paddingTop: 4, textAlign: 'center' },
});

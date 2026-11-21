// Headline card under the large title: team summary + connection status pill.
import { SymbolView } from 'expo-symbols';
import { useEffect, useState } from 'react';
import { StyleSheet, Text, View } from 'react-native';
import { colors, type } from '../lib/theme';
import type { ConnStatus } from '../lib/useOnlyVibes';
import { Glass } from './Glass';

const STATUS: Record<ConnStatus, { label: string; color: string; icon: string }> = {
  live: { label: 'LIVE', color: colors.green, icon: 'dot.radiowaves.left.and.right' },
  polling: { label: 'POLLING', color: colors.orange, icon: 'arrow.2.circlepath' },
  demo: { label: 'DEMO', color: colors.accent, icon: 'play.circle' },
  connecting: { label: 'CONNECTING', color: colors.yellow, icon: 'hourglass' },
  offline: { label: 'OFFLINE', color: colors.red, icon: 'wifi.slash' },
};

export function TeamHeader({
  summary,
  status,
  lastSeen,
}: {
  summary: string;
  status: ConnStatus;
  lastSeen: number | null;
}) {
  const [ago, setAgo] = useState('');
  useEffect(() => {
    const f = () => {
      if (!lastSeen) return setAgo('');
      const s = Math.max(0, Math.round((Date.now() - lastSeen) / 1000));
      setAgo(s < 2 ? 'just now' : `${s}s ago`);
    };
    f();
    const t = setInterval(f, 1000);
    return () => clearInterval(t);
  }, [lastSeen]);

  const st = STATUS[status];
  return (
    <Glass style={styles.card} material="regular">
      <View style={styles.row}>
        <View style={[styles.dot, { backgroundColor: st.color }]} />
        <Text style={[styles.status, { color: st.color }]}>{st.label}</Text>
        {!!ago && <Text style={styles.ago}>{ago}</Text>}
        <View style={{ flex: 1 }} />
        <SymbolView name={st.icon as never} size={14} tintColor={st.color} />
      </View>
      <Text style={styles.summary}>{summary || 'Waiting for the hub…'}</Text>
    </Glass>
  );
}

const styles = StyleSheet.create({
  card: { padding: 16, gap: 8 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 7 },
  dot: { width: 8, height: 8, borderRadius: 999 },
  status: { ...type.caption2, letterSpacing: 0.6 },
  ago: { ...type.caption2, color: colors.labelTertiary },
  summary: { ...type.headline, color: colors.label, lineHeight: 23 },
});

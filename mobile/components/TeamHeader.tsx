// Headline card under the large title: team summary + connection status pill.
import { Image, StyleSheet, Text, View } from 'react-native';
import { colors, type } from '../lib/theme';
import { useAgo } from '../lib/useAgo';
import type { ConnStatus } from '../lib/useOnlyVibes';
import { Glass } from './Glass';

// pixel mark from web/logo/mark.svg
const MARK = require('../assets/brand/mark.png');

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
  const ago = useAgo(lastSeen);
  const st = STATUS[status];
  return (
    <Glass style={styles.card} material="regular">
      <View style={styles.row}>
        <View style={[styles.dot, { backgroundColor: st.color }]} />
        <Text style={[styles.status, { color: st.color }]}>{st.label}</Text>
        {!!ago && <Text style={styles.ago}>{ago}</Text>}
        <View style={{ flex: 1 }} />
        <Image source={MARK} style={styles.mark} resizeMode="contain" />
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
  mark: { width: 26, height: 16, opacity: 0.9 },
  summary: { ...type.headline, color: colors.label, lineHeight: 23 },
});

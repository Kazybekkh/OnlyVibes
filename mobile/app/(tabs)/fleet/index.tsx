// Fleet tab — the main view: team headline + one glass card per laptop.
import { useMemo } from 'react';
import { Image, ScrollView, StyleSheet, Text, View } from 'react-native';
import { Background } from '../../../components/Background';
import { LaptopCard } from '../../../components/LaptopCard';
import { TeamHeader } from '../../../components/TeamHeader';
import { colors, type } from '../../../lib/theme';
import { useServerConfig } from '../../../lib/server';
import { useOnlyVibes } from '../../../lib/useOnlyVibes';

// official wordmark (web/logo/wordmark.svg) as the in-page heading
const WORDMARK = require('../../../assets/brand/wordmark.png');

export default function FleetScreen() {
  const { url, demo, nonce } = useServerConfig();
  const { snapshot, status, lastSeen } = useOnlyVibes(url, demo, nonce);

  const laptops = useMemo(
    () => [...(snapshot?.laptops ?? [])].sort((a, b) => a.laptop.localeCompare(b.laptop)),
    [snapshot],
  );

  return (
    <View style={styles.root}>
      <Background />
      <ScrollView
        style={styles.scroll}
        contentInsetAdjustmentBehavior="automatic"
        contentContainerStyle={styles.content}
        showsVerticalScrollIndicator={false}
      >
        <Image source={WORDMARK} style={styles.wordmark} resizeMode="contain" />
        <TeamHeader
          summary={snapshot?.team_summary ?? ''}
          status={status}
          lastSeen={lastSeen}
        />

        {!!snapshot?.alerts?.length && (
          <View style={styles.alertBanner}>
            <Text style={styles.alertText}>
              ⚠ {snapshot.alerts.map((a) => a.text).join(' · ')}
            </Text>
          </View>
        )}

        <View style={styles.sectionHead}>
          <Text style={styles.sectionTitle}>Laptops</Text>
          <Text style={styles.sectionCount}>{laptops.length}</Text>
        </View>

        {laptops.length === 0 && (
          <Text style={styles.empty}>
            {status === 'demo'
              ? 'No demo laptops yet…'
              : 'No laptops reporting. Start a reporter on a teammate machine, or enable Demo mode in Settings.'}
          </Text>
        )}

        {laptops.map((l, i) => (
          <LaptopCard key={l.tailscale_ip || l.laptop} laptop={l} index={i} />
        ))}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  scroll: { flex: 1 },
  content: { paddingHorizontal: 16, paddingBottom: 120, gap: 14 },
  wordmark: { width: 132, height: 28, marginTop: 6, marginLeft: 4, marginBottom: -4 },
  sectionHead: {
    flexDirection: 'row', alignItems: 'baseline', gap: 8, marginTop: 10, marginBottom: -4, paddingHorizontal: 4,
  },
  sectionTitle: { ...type.title2, fontWeight: '700', color: colors.label },
  sectionCount: { ...type.subhead, color: colors.labelSecondary },
  alertBanner: {
    backgroundColor: 'rgba(255,59,48,0.10)',
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: 'rgba(255,59,48,0.35)',
    borderRadius: 18,
    padding: 12,
  },
  alertText: { ...type.subhead, color: '#d70015', fontWeight: '600' },
  empty: { ...type.subhead, color: colors.labelSecondary, textAlign: 'center', marginTop: 40, lineHeight: 22 },
});

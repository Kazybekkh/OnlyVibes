// Alerts tab — failed orchestra tasks + recent inter-agent messages
// (and collision alerts when talking to a legacy pre-orchestra hub).
import { SymbolView } from 'expo-symbols';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { Background } from '../../../components/Background';
import { Glass } from '../../../components/Glass';
import { colors, type } from '../../../lib/theme';
import { useServerConfig } from '../../../lib/server';
import { useOnlyVibes } from '../../../lib/useOnlyVibes';

export default function AlertsScreen() {
  const cfg = useServerConfig();
  const { snapshot, status } = useOnlyVibes(cfg.url, cfg.demo, cfg.nonce);
  const alerts = snapshot?.alerts ?? [];
  const failed = (snapshot?.tasks ?? []).filter((t) => t.status === 'failed');
  const messages = [...(snapshot?.messages ?? [])].slice(-8).reverse();

  return (
    <View style={styles.root}>
      <Background />
      <ScrollView
        style={styles.scroll}
        contentInsetAdjustmentBehavior="automatic"
        contentContainerStyle={styles.content}
        showsVerticalScrollIndicator={false}
      >
        <Text style={styles.intro}>
          Failed tasks and agent traffic from the hub. Legacy collision alerts appear here too.
        </Text>

        {alerts.length === 0 && failed.length === 0 ? (
          <Glass style={styles.card}>
            <View style={styles.emptyRow}>
              <SymbolView name="checkmark.shield" size={22} tintColor={colors.green} />
              <Text style={styles.emptyText}>Nothing failing — all tasks are on track.</Text>
            </View>
          </Glass>
        ) : (
          <>
            {alerts.map((a, i) => (
              <Glass key={`legacy:${i}`} style={styles.card}>
                <View style={styles.alertHead}>
                  <SymbolView name="exclamationmark.triangle.fill" size={18} tintColor={colors.red} />
                  <Text style={styles.alertKind}>collision</Text>
                </View>
                <Text style={styles.alertText}>{a.text}</Text>
                {!!a.file && <Text style={styles.alertFile}>{a.file}</Text>}
              </Glass>
            ))}
            {failed.map((t) => (
              <Glass key={t.id} style={styles.card}>
                <View style={styles.alertHead}>
                  <SymbolView name="xmark.octagon.fill" size={18} tintColor={colors.red} />
                  <Text style={styles.alertKind}>{t.key} failed</Text>
                </View>
                <Text style={styles.alertText}>{t.title}</Text>
                {!!t.error && <Text style={styles.alertFile}>{t.error}</Text>}
                {!!t.assignee && (
                  <Text style={styles.alertFile}>
                    {t.assignee.agent}@{t.assignee.laptop}
                  </Text>
                )}
              </Glass>
            ))}
          </>
        )}

        <View style={styles.sectionHead}>
          <Text style={styles.sectionTitle}>Messages</Text>
        </View>
        <Glass style={styles.card}>
          {messages.length === 0 ? (
            <Text style={styles.emptyText}>
              {status === 'demo' ? 'Demo messages below soon.' : 'No messages yet.'}
            </Text>
          ) : (
            messages.map((m, i) => (
              <View key={m.id} style={i > 0 && styles.divider}>
                <Text style={styles.msgFrom}>{m.from?.name ?? m.from?.kind ?? '?'}</Text>
                <Text style={styles.msgText}>{m.text}</Text>
              </View>
            ))
          )}
        </Glass>
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  scroll: { flex: 1 },
  content: { paddingHorizontal: 16, paddingBottom: 120, gap: 14 },
  intro: { ...type.subhead, color: colors.labelSecondary, paddingHorizontal: 4 },
  card: { padding: 16, gap: 8 },
  emptyRow: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  emptyText: { ...type.subhead, color: colors.labelSecondary, flex: 1, lineHeight: 21 },
  alertHead: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  alertKind: { ...type.caption2, color: colors.red, textTransform: 'uppercase', letterSpacing: 0.6 },
  alertText: { ...type.headline, color: colors.label, lineHeight: 22 },
  alertFile: { ...type.caption1, color: colors.labelTertiary, fontFamily: 'Menlo' },
  sectionHead: { marginTop: 10, marginBottom: -4, paddingHorizontal: 4 },
  sectionTitle: { ...type.title2, fontWeight: '700', color: colors.label },
  divider: { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.separator, paddingTop: 8, marginTop: 8 },
  msgFrom: { ...type.caption2, color: colors.accent, fontWeight: '700' },
  msgText: { ...type.subhead, color: colors.label, marginTop: 2 },
});

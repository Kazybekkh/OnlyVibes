// Alerts tab — collision alerts from the server + the voice turn card.
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { Background } from '../../../components/Background';
import { Glass } from '../../../components/Glass';
import { colors, type } from '../../../lib/theme';
import { useServerConfig } from '../../../lib/server';
import { useOnlyVibes } from '../../../lib/useOnlyVibes';
import { SymbolView } from 'expo-symbols';

export default function AlertsScreen() {
  const { url, demo, nonce } = useServerConfig();
  const { snapshot, status } = useOnlyVibes(url, demo, nonce);
  const alerts = snapshot?.alerts ?? [];
  const voice = snapshot?.voice;

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
          The hub flags when two agents edit the same file at once.
        </Text>

        {alerts.length === 0 ? (
          <Glass style={styles.card}>
            <View style={styles.emptyRow}>
              <SymbolView name="checkmark.shield" size={22} tintColor={colors.green} />
              <Text style={styles.emptyText}>No collisions — everyone is clear.</Text>
            </View>
          </Glass>
        ) : (
          alerts.map((a, i) => (
            <Glass key={`${a.file ?? a.kind}:${i}`} style={styles.card}>
              <View style={styles.alertHead}>
                <SymbolView name="exclamationmark.triangle.fill" size={18} tintColor={colors.red} />
                <Text style={styles.alertKind}>collision</Text>
              </View>
              <Text style={styles.alertText}>{a.text}</Text>
              {!!a.file && <Text style={styles.alertFile}>{a.file}</Text>}
            </Glass>
          ))
        )}

        <View style={styles.sectionHead}>
          <Text style={styles.sectionTitle}>Voice</Text>
        </View>
        <Glass style={styles.card}>
          {voice ? (
            <>
              <Text style={styles.voiceState}>{voice.state ?? 'active'}</Text>
              {!!voice.heard && <Text style={styles.voiceLine}>you: {String(voice.heard)}</Text>}
              {!!voice.reply && <Text style={styles.voiceLine}>→ {String(voice.reply)}</Text>}
            </>
          ) : (
            <Text style={styles.emptyText}>
              {status === 'demo' ? 'Demo mode — voice is not connected.' : 'No voice turn in progress.'}
            </Text>
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
  sectionTitle: { ...type.title2, color: colors.label },
  voiceState: { ...type.caption2, color: colors.accent, textTransform: 'uppercase', letterSpacing: 0.6 },
  voiceLine: { ...type.subhead, color: colors.label },
});

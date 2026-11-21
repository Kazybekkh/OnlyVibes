// Settings tab — hub server URL, demo mode, reconnect, about.
import * as Haptics from 'expo-haptics';
import { Icon } from '../../../components/Icon';
import { useState } from 'react';
import {
  Image,
  Pressable,
  ScrollView,
  StyleSheet,
  Switch,
  Text,
  TextInput,
  View,
} from 'react-native';
import { Background } from '../../../components/Background';
import { Glass } from '../../../components/Glass';
import { colors, radius, type } from '../../../lib/theme';
import { normalizeUrl, setServerConfig, useServerConfig } from '../../../lib/server';
import { useAgo } from '../../../lib/useAgo';
import { useOnlyVibes } from '../../../lib/useOnlyVibes';

export default function SettingsScreen() {
  const cfg = useServerConfig();
  const { status, lastSeen } = useOnlyVibes(cfg.url, cfg.demo, cfg.nonce);
  const ago = useAgo(lastSeen);
  const [draft, setDraft] = useState(cfg.url);
  const norm = normalizeUrl(draft);

  const apply = () => {
    Haptics.impactAsync(Haptics.ImpactFeedbackStyle.Light);
    setServerConfig({ url: draft.trim(), nonce: cfg.nonce + 1 });
  };

  return (
    <View style={styles.root}>
      <Background />
      <ScrollView
        style={styles.scroll}
        contentInsetAdjustmentBehavior="automatic"
        contentContainerStyle={styles.content}
        keyboardShouldPersistTaps="handled"
        showsVerticalScrollIndicator={false}
      >
        <Text style={styles.sectionTitle}>Hub server</Text>
        <Glass style={styles.card}>
          <Text style={styles.label}>Server URL</Text>
          <TextInput
            style={styles.input}
            value={draft}
            onChangeText={setDraft}
            onSubmitEditing={apply}
            placeholder="http://100.64.0.11:8000"
            placeholderTextColor={colors.labelTertiary}
            autoCapitalize="none"
            autoCorrect={false}
            keyboardType="url"
            returnKeyType="go"
          />
          <Text style={styles.hint}>
            {norm
              ? `ws ${norm.ws}/view · rest ${norm.http}/api/state`
              : 'Enter a valid URL, e.g. http://100.64.0.11:8000 (Tailscale IP of the hub)'}
          </Text>
          <Pressable
            onPress={apply}
            style={({ pressed }) => [styles.button, pressed && { opacity: 0.7 }]}
          >
            <Icon name="arrow.clockwise" size={16} color="#fff" />
            <Text style={styles.buttonText}>Reconnect</Text>
          </Pressable>
          {/* glass-prominent style: accent-tinted translucent fill + rim */}
          <View style={styles.statusRow}>
            <View style={[styles.dot, { backgroundColor: statusColor(status) }]} />
            <Text style={styles.statusText}>
              {status}
              {!!ago && ` · updated ${ago}`}
            </Text>
          </View>
        </Glass>

        <Text style={styles.sectionTitle}>Data</Text>
        <Glass style={styles.card}>
          <View style={styles.switchRow}>
            <View style={{ flex: 1 }}>
              <Text style={styles.switchLabel}>Demo mode</Text>
              <Text style={styles.hint}>Fake laptops, no server needed (like the web fallback)</Text>
            </View>
            <Switch
              value={cfg.demo}
              onValueChange={(demo) => setServerConfig({ demo, nonce: cfg.nonce + 1 })}
              trackColor={{ false: 'rgba(0,0,0,0.15)', true: colors.accent }}
              thumbColor="#fff"
            />
          </View>
        </Glass>

        <Text style={styles.sectionTitle}>About</Text>
        <Glass style={styles.card}>
          <Image
            source={require('../../../assets/brand/wordmark.png')}
            style={styles.wordmark}
            resizeMode="contain"
          />
          <Text style={styles.aboutTitle}>OnlyVibes</Text>
          <Text style={styles.aboutBody}>
            Live view of which AI coding agents (Vibe, Claude Code, Codex, Cursor) are running on each
            teammate&apos;s laptop over Tailscale. This app connects to the hub server&apos;s /view WebSocket
            (falling back to /api/state polling) and renders the same snapshot as the web 3D view.
          </Text>
          <Text style={styles.aboutFoot}>
            Hub: cd server && uv run uvicorn main:app --host 0.0.0.0 --port 8000
          </Text>
        </Glass>
      </ScrollView>
    </View>
  );
}

function statusColor(status: string) {
  switch (status) {
    case 'live': return '#1a7f37';
    case 'polling': return '#c77400';
    case 'demo': return colors.accent;
    case 'connecting': return '#b25000';
    default: return '#d70015';
  }
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  scroll: { flex: 1 },
  content: { paddingHorizontal: 16, paddingBottom: 120, gap: 12 },
  sectionTitle: { ...type.title2, fontWeight: '700', color: colors.label, marginTop: 10, marginBottom: -2, paddingHorizontal: 4 },
  card: { padding: 16, gap: 10 },
  label: { ...type.subhead, fontWeight: '600', color: colors.label },
  input: {
    ...type.body,
    // web renders <input> unpositioned, which would paint under Glass's blur layers
    position: 'relative',
    color: colors.label,
    backgroundColor: colors.inputBg,
    borderRadius: 14,
    paddingHorizontal: 14,
    paddingVertical: 11,
    borderWidth: StyleSheet.hairlineWidth,
    borderColor: colors.glassBorder,
  },
  hint: { ...type.caption1, color: colors.labelTertiary, lineHeight: 17 },
  button: {
    flexDirection: 'row', alignItems: 'center', justifyContent: 'center', gap: 8,
    // prominent (filled) button — solid accent, white label, capsule
    backgroundColor: colors.accent,
    borderRadius: radius.pill,
    paddingVertical: 12, marginTop: 2,
  },
  buttonText: { ...type.subhead, fontWeight: '700', color: '#fff' },
  statusRow: { flexDirection: 'row', alignItems: 'center', gap: 8, marginTop: 2 },
  dot: { width: 8, height: 8, borderRadius: 999 },
  statusText: { ...type.caption2, color: colors.labelSecondary, textTransform: 'uppercase', letterSpacing: 0.5 },
  switchRow: { flexDirection: 'row', alignItems: 'center', gap: 14 },
  switchLabel: { ...type.headline, color: colors.label },
  wordmark: { width: 93, height: 20, marginBottom: 2 },
  aboutTitle: { ...type.title3, fontWeight: '600', color: colors.label },
  aboutBody: { ...type.subhead, color: colors.labelSecondary, lineHeight: 21 },
  aboutFoot: { ...type.caption1, color: colors.labelTertiary, fontFamily: 'Menlo' },
});

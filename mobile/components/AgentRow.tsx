// One agent on a laptop: icon tile, base name + session suffix (wraps, never
// clips), state chip, pid/cpu meta, cpu meter, summary + raw log.
import type { SFSymbol } from 'expo-symbols';
import { Icon } from './Icon';
import { Image, StyleSheet, Text, View, type ImageSourcePropType } from 'react-native';
import { agentColor, colors, type } from '../lib/theme';
import { agentBase, type Agent } from '../lib/types';
import { StateChip } from './StateChip';

// Official agent logos from web/logos/ (copied to assets/brand/logos/)
const AGENT_LOGOS: Record<string, ImageSourcePropType> = {
  'vibe': require('../assets/brand/logos/vibe.png'),
  'claude-code': require('../assets/brand/logos/claude-code.png'),
  'codex': require('../assets/brand/logos/codex.png'),
  'cursor': require('../assets/brand/logos/cursor.png'),
};

const AGENT_ICONS: Record<string, SFSymbol> = {
  'vibe': 'sparkles',
  'claude-code': 'asterisk.circle',
  'codex': 'chevron.left.forwardslash.chevron.right',
  'cursor': 'cursorarrow.rays',
};

export function AgentRow({ agent, showLog = false }: { agent: Agent; showLog?: boolean }) {
  const base = agentBase(agent.name);
  const suffix = agent.name.length > base.length ? agent.name.slice(base.length).trim() : '';
  const c = agentColor(base);
  const cpu = Math.min(100, Math.max(0, agent.cpu || 0));
  return (
    <View style={styles.row}>
      <View style={[styles.iconTile, { backgroundColor: `${c}22` }]}>
        {AGENT_LOGOS[base] ? (
          <Image source={AGENT_LOGOS[base]} style={styles.logo} resizeMode="contain" />
        ) : (
          <Icon name={AGENT_ICONS[base] ?? 'sparkles'} size={15} color={c} />
        )}
      </View>
      <View style={styles.body}>
        <View style={styles.head}>
          <View style={styles.nameWrap}>
            <Text style={[styles.name, { color: c }]}>{base}</Text>
            {!!suffix && (
              <Text style={styles.suffix} numberOfLines={1}>{suffix}</Text>
            )}
          </View>
          <StateChip state={agent.state} />
        </View>
        <View style={styles.metaRow}>
          <Text style={styles.pid}>{agent.kind === 'task' ? 'task' : `pid ${agent.pid}`}</Text>
          <Text style={styles.pid}>{cpu.toFixed(1)}% cpu</Text>
        </View>
        <View style={styles.cpuTrack}>
          <View style={[styles.cpuFill, { width: `${cpu}%`, backgroundColor: c }]} />
        </View>
        {!!(agent.summary || agent.last_log) && (
          <Text style={styles.summary} numberOfLines={showLog ? undefined : 2}>
            {agent.summary || agent.last_log}
          </Text>
        )}
        {showLog && !!agent.last_log && !!agent.summary && (
          <Text style={styles.log} numberOfLines={3}>{agent.last_log}</Text>
        )}
        {!!agent.cwd && <Text style={styles.cwd} numberOfLines={1}>{agent.cwd}</Text>}
      </View>
    </View>
  );
}

const styles = StyleSheet.create({
  row: { flexDirection: 'row', gap: 12, paddingVertical: 10 },
  iconTile: {
    width: 32, height: 32, borderRadius: 10,
    alignItems: 'center', justifyContent: 'center',
    marginTop: 1,
    flexShrink: 0,
  },
  logo: { width: 18, height: 18, borderRadius: 4 },
  body: { flex: 1, minWidth: 0 },
  head: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  nameWrap: {
    flex: 1, minWidth: 0,
    flexDirection: 'row', flexWrap: 'wrap', alignItems: 'baseline', columnGap: 6,
  },
  name: { ...type.subhead, fontWeight: '700' },
  suffix: { ...type.caption1, color: colors.labelTertiary, flexShrink: 1 },
  metaRow: { flexDirection: 'row', gap: 10, marginTop: 3 },
  pid: { ...type.caption1, color: colors.labelTertiary },
  cpuTrack: {
    marginTop: 6, height: 5, borderRadius: 999,
    backgroundColor: colors.track, overflow: 'hidden',
  },
  cpuFill: { height: '100%', borderRadius: 999, opacity: 0.9 },
  summary: { ...type.subhead, color: colors.label, marginTop: 6, lineHeight: 20 },
  log: { ...type.footnote, color: colors.labelTertiary, marginTop: 3, fontFamily: 'Menlo' },
  cwd: { ...type.caption2, color: colors.labelTertiary, marginTop: 3 },
});

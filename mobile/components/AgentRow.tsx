// One agent on a laptop: state chip, name/pid/cpu, cpu meter, summary + raw log.
import { SymbolView } from 'expo-symbols';
import { StyleSheet, Text, View } from 'react-native';
import { agentColor, colors, type } from '../lib/theme';
import type { Agent } from '../lib/types';
import { StateChip } from './StateChip';

const AGENT_ICONS: Record<string, string> = {
  'vibe': 'sparkles',
  'claude-code': 'asterisk.circle',
  'codex': 'chevron.left.forwardslash.chevron.right',
  'cursor': 'cursorarrow.rays',
};

export function AgentRow({ agent, showLog = false }: { agent: Agent; showLog?: boolean }) {
  const c = agentColor(agent.name);
  const cpu = Math.min(100, Math.max(0, agent.cpu || 0));
  return (
    <View style={styles.row}>
      <View style={[styles.iconTile, { backgroundColor: `${c}22` }]}>
        <SymbolView name={(AGENT_ICONS[agent.name] ?? 'sparkles') as never} size={15} tintColor={c} />
      </View>
      <View style={styles.body}>
        <View style={styles.head}>
          <Text style={[styles.name, { color: c }]} numberOfLines={1}>{agent.name}</Text>
          <Text style={styles.pid}>pid {agent.pid}</Text>
          <StateChip state={agent.state} />
        </View>
        <View style={styles.cpuTrack}>
          <View style={[styles.cpuFill, { width: `${cpu}%`, backgroundColor: c }]} />
          <Text style={styles.cpuText}>{cpu.toFixed(1)}% cpu</Text>
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
  row: { flexDirection: 'row', gap: 10, paddingVertical: 8 },
  iconTile: {
    width: 30, height: 30, borderRadius: 9,
    alignItems: 'center', justifyContent: 'center',
    marginTop: 1,
  },
  body: { flex: 1 },
  head: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  name: { ...type.subhead, fontWeight: '700', flexShrink: 1 },
  pid: { ...type.caption1, color: colors.labelTertiary },
  cpuTrack: {
    marginTop: 5, height: 14, borderRadius: 999,
    backgroundColor: 'rgba(255,255,255,0.07)',
    justifyContent: 'center', overflow: 'hidden',
  },
  cpuFill: { position: 'absolute', left: 0, top: 0, bottom: 0, borderRadius: 999, opacity: 0.85 },
  cpuText: {
    ...type.caption2, color: colors.labelSecondary,
    textAlign: 'center', overflow: 'hidden',
  },
  summary: { ...type.subhead, color: colors.label, marginTop: 5 },
  log: { ...type.footnote, color: colors.labelTertiary, marginTop: 3, fontFamily: 'Menlo' },
  cwd: { ...type.caption2, color: colors.labelTertiary, marginTop: 3 },
});

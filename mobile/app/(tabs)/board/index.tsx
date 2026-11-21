// Board tab — the orchestra view from GET /api/state: projects, tasks
// (kanban-ish list with status + progress), and connected workers.
import { SymbolView } from 'expo-symbols';
import { ScrollView, StyleSheet, Text, View } from 'react-native';
import { Background } from '../../../components/Background';
import { Glass } from '../../../components/Glass';
import {
  colors,
  projectStatusColors,
  taskStatusColors,
  type,
} from '../../../lib/theme';
import { useServerConfig } from '../../../lib/server';
import type { Task, Worker } from '../../../lib/types';
import { useOnlyVibes } from '../../../lib/useOnlyVibes';

const TASK_ORDER = ['in_progress', 'claimed', 'review', 'todo', 'backlog', 'done', 'failed', 'cancelled'];

function StatusPill({ label, color }: { label: string; color: string }) {
  return (
    <View style={[styles.pill, { backgroundColor: `${color}26` }]}>
      <Text style={[styles.pillText, { color }]}>{label}</Text>
    </View>
  );
}

function TaskRow({ task }: { task: Task }) {
  const c = taskStatusColors[task.status] ?? colors.gray;
  const pct = Math.min(100, Math.max(0, task.progress || 0));
  return (
    <View style={styles.taskRow}>
      <View style={styles.taskHead}>
        <Text style={[styles.taskKey, { color: c }]}>{task.key}</Text>
        <Text style={styles.taskTitle} numberOfLines={1}>{task.title}</Text>
        <StatusPill label={String(task.status).replace('_', ' ')} color={c} />
      </View>
      <View style={styles.cpuTrack}>
        <View style={[styles.cpuFill, { width: `${pct}%`, backgroundColor: c }]} />
      </View>
      {!!task.assignee && (
        <Text style={styles.meta}>
          {task.assignee.agent}@{task.assignee.laptop} · {task.assignee.owner}
        </Text>
      )}
      {!!task.progress_note && <Text style={styles.meta}>{task.progress_note}</Text>}
      {!!task.last_event && <Text style={styles.meta} numberOfLines={1}>{task.last_event}</Text>}
      {!!task.error && <Text style={[styles.meta, { color: colors.red }]} numberOfLines={1}>{task.error}</Text>}
    </View>
  );
}

function WorkerRow({ w }: { w: Worker }) {
  const c = w.status === 'busy' ? colors.orange : w.status === 'offline' ? colors.red : colors.green;
  return (
    <View style={styles.workerRow}>
      <View style={[styles.iconTile, { backgroundColor: `${c}22` }]}>
        <SymbolView name="laptopcomputer" size={15} tintColor={c} />
      </View>
      <View style={{ flex: 1 }}>
        <Text style={styles.workerName} numberOfLines={1}>{w.laptop} · {w.owner}</Text>
        <Text style={styles.meta} numberOfLines={1}>
          {w.running.length}/{w.max_concurrent} running · {w.agents.join(', ')}
        </Text>
      </View>
      <StatusPill label={w.status} color={c} />
    </View>
  );
}

export default function BoardScreen() {
  const cfg = useServerConfig();
  const { snapshot } = useOnlyVibes(cfg.url, cfg.demo, cfg.nonce);

  const projects = snapshot?.projects ?? [];
  const tasks = [...(snapshot?.tasks ?? [])].sort(
    (a, b) => (TASK_ORDER.indexOf(a.status) + 100) % 100 - (TASK_ORDER.indexOf(b.status) + 100) % 100
      || a.key.localeCompare(b.key, undefined, { numeric: true }),
  );
  const workers = [...(snapshot?.workers ?? [])].sort((a, b) => a.laptop.localeCompare(b.laptop));
  const hasBoard = !!(snapshot?.projects || snapshot?.tasks || snapshot?.workers);

  return (
    <View style={styles.root}>
      <Background />
      <ScrollView
        style={styles.scroll}
        contentInsetAdjustmentBehavior="automatic"
        contentContainerStyle={styles.content}
        showsVerticalScrollIndicator={false}
      >
        {!hasBoard && (
          <Glass style={styles.card}>
            <Text style={styles.empty}>
              No board data. This hub only exposes the laptop fleet (/view) — or enable Demo mode in Settings.
            </Text>
          </Glass>
        )}

        {projects.length > 0 && (
          <>
            <Text style={styles.sectionTitle}>Projects ({projects.length})</Text>
            {projects.map((p) => {
              const c = projectStatusColors[p.status] ?? colors.gray;
              return (
                <Glass key={p.id} style={styles.card}>
                  <View style={styles.row}>
                    <Text style={styles.cardTitle} numberOfLines={1}>{p.title}</Text>
                    <StatusPill label={p.status} color={c} />
                  </View>
                  {!!p.repo && <Text style={styles.meta}>repo {p.repo} · {p.base_branch}</Text>}
                  {!!p.summary && <Text style={styles.meta} numberOfLines={2}>{p.summary}</Text>}
                </Glass>
              );
            })}
          </>
        )}

        {tasks.length > 0 && (
          <>
            <Text style={styles.sectionTitle}>Tasks ({tasks.length})</Text>
            <Glass style={styles.card}>
              {tasks.map((t, i) => (
                <View key={t.id} style={i > 0 && styles.divider}>
                  <TaskRow task={t} />
                </View>
              ))}
            </Glass>
          </>
        )}

        {workers.length > 0 && (
          <>
            <Text style={styles.sectionTitle}>Workers ({workers.length})</Text>
            <Glass style={styles.card}>
              {workers.map((w, i) => (
                <View key={w.id} style={i > 0 && styles.divider}>
                  <WorkerRow w={w} />
                </View>
              ))}
            </Glass>
          </>
        )}
      </ScrollView>
    </View>
  );
}

const styles = StyleSheet.create({
  root: { flex: 1, backgroundColor: colors.bg },
  scroll: { flex: 1 },
  content: { paddingHorizontal: 16, paddingBottom: 120, gap: 12 },
  sectionTitle: { ...type.title2, fontWeight: '700', color: colors.label, marginTop: 10, marginBottom: -2, paddingHorizontal: 4 },
  card: { padding: 16, gap: 10 },
  row: { flexDirection: 'row', alignItems: 'center', gap: 10 },
  cardTitle: { ...type.headline, color: colors.label, flex: 1 },
  pill: { paddingHorizontal: 9, paddingVertical: 3, borderRadius: 999 },
  pillText: { ...type.caption2, fontWeight: '600', textTransform: 'capitalize' },
  taskRow: { paddingVertical: 8, gap: 4 },
  taskHead: { flexDirection: 'row', alignItems: 'center', gap: 8 },
  taskKey: { ...type.caption2, fontWeight: '700' },
  taskTitle: { ...type.subhead, fontWeight: '600', color: colors.label, flex: 1 },
  cpuTrack: { height: 5, borderRadius: 999, backgroundColor: 'rgba(255,255,255,0.08)', overflow: 'hidden' },
  cpuFill: { height: '100%', borderRadius: 999 },
  meta: { ...type.caption1, color: colors.labelSecondary },
  workerRow: { flexDirection: 'row', alignItems: 'center', gap: 10, paddingVertical: 6 },
  workerName: { ...type.subhead, fontWeight: '600', color: colors.label },
  iconTile: { width: 30, height: 30, borderRadius: 9, alignItems: 'center', justifyContent: 'center' },
  divider: { borderTopWidth: StyleSheet.hairlineWidth, borderTopColor: colors.separator },
  empty: { ...type.subhead, color: colors.labelSecondary, lineHeight: 21 },
});

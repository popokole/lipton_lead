'use client';

import { Shell } from '@/components/shell';
import { Badge, BarChart, Card, Empty, ErrorText, PageHeader, Stat, Table } from '@/components/ui';
import { useApi, useRealtime } from '@/lib/hooks';
import type { DashboardCounters, DashboardSeries, RuleStat } from '@/lib/types';

export default function DashboardPage() {
  const counters = useApi<DashboardCounters>('/analytics/dashboard', 10_000);
  const series = useApi<DashboardSeries>('/analytics/series?days=14', 60_000);
  const ruleStats = useApi<RuleStat[]>('/analytics/rules', 30_000);
  const realtime = useRealtime(25);

  return (
    <Shell>
      <PageHeader
        title="Дашборд"
        subtitle="Сводка за последние сутки"
        actions={
          <span className={`text-xs ${realtime.connected ? 'text-emerald-400' : 'text-slate-500'}`}>
            {realtime.connected ? '● realtime подключён' : '○ realtime не подключён'}
          </span>
        }
      />

      <ErrorText>{counters.error}</ErrorText>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Аккаунты онлайн" value={`${counters.data?.accounts_online ?? 0} / ${counters.data?.accounts_total ?? 0}`} />
        <Stat label="Чатов под наблюдением" value={counters.data?.chats_monitored ?? 0} />
        <Stat label="Сообщений за сутки" value={counters.data?.messages_today ?? 0} />
        <Stat label="Обращений к AI" value={counters.data?.ai_analyzed_today ?? 0} />
        <Stat label="Отправлено ответов" value={counters.data?.replies_today ?? 0} />
        <Stat label="Лидов всего" value={counters.data?.leads_total ?? 0} />
        <Stat label="Предупреждений" value={counters.data?.errors_today ?? 0} />
        <Stat label="Живых воркеров" value={counters.data?.workers_healthy ?? 0} />
      </div>

      <div className="mt-6 grid gap-4 lg:grid-cols-2">
        <Card title="Сообщения по дням">
          <BarChart points={series.data?.messages ?? []} />
        </Card>
        <Card title="Совпадения правил по дням">
          <BarChart points={series.data?.matches ?? []} color="#38bdf8" />
        </Card>
        <Card title="Ответы по дням">
          <BarChart points={series.data?.replies ?? []} color="#34d399" />
        </Card>
        <Card title="Лиды по дням">
          <BarChart points={series.data?.leads ?? []} color="#fbbf24" />
        </Card>
      </div>

      <Card title="Ответы по правилам" className="mt-6">
        <ErrorText>{ruleStats.error}</ErrorText>
        {(ruleStats.data?.length ?? 0) === 0 ? (
          <Empty>Правил пока нет</Empty>
        ) : (
          <Table head={['Правило', 'Статус', 'Совпадений', 'Ответов']}>
            {ruleStats.data?.map((rule) => (
              <tr key={rule.rule_id} className="border-b border-ink-800/70 last:border-0">
                <td className="py-2 pr-4 text-slate-200">{rule.rule_name}</td>
                <td className="py-2 pr-4">
                  <Badge tone={rule.enabled ? 'ok' : 'mute'}>
                    {rule.enabled ? 'включено' : 'выключено'}
                  </Badge>
                </td>
                <td className="py-2 pr-4 text-slate-400">{rule.matches}</td>
                <td className="py-2 text-slate-300">{rule.replies}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <Card title="Живая лента" className="mt-6">
        {realtime.events.length === 0 ? (
          <Empty>
            Событий пока не было. Они появятся, как только аккаунт начнёт получать сообщения.
          </Empty>
        ) : (
          <ul className="space-y-1 font-mono text-xs text-slate-400">
            {realtime.events.map((event, index) => (
              <li key={`${event.ts}-${index}`} className="truncate">
                <span className="text-slate-600">{event.ts.slice(11, 19)}</span>{' '}
                <span className="text-accent">{event.type}</span>{' '}
                {JSON.stringify(event.payload).slice(0, 160)}
              </li>
            ))}
          </ul>
        )}
      </Card>
    </Shell>
  );
}

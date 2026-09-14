'use client';

import { Shell } from '@/components/shell';
import {
  BarChart,
  Card,
  Empty,
  ErrorText,
  PageHeader,
  Stat,
  StatusBadge,
} from '@/components/ui';
import { useApi } from '@/lib/hooks';
import type { DashboardCounters, ScenarioLeadStat } from '@/lib/types';

function weekSum(series: { value: number }[]): number {
  return series.reduce((acc, point) => acc + point.value, 0);
}

export default function OverviewPage() {
  const counters = useApi<DashboardCounters>('/analytics/dashboard', 10_000);
  const leadsByScenario = useApi<ScenarioLeadStat[]>('/analytics/leads/by-scenario?days=7', 30_000);

  const c = counters.data;
  const online = c?.accounts_online ?? 0;
  const total = c?.accounts_total ?? 0;
  const directions = leadsByScenario.data ?? [];

  return (
    <Shell>
      <PageHeader
        title="Обзор"
        subtitle="Что происходит прямо сейчас"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={online > 0 ? 'ok' : 'bad'} pulse={online > 0}>
              AI {online > 0 ? 'активен' : 'не в сети'}
            </StatusBadge>
            <StatusBadge tone={online > 0 ? 'ok' : 'bad'}>
              Telegram {online}/{total}
            </StatusBadge>
            <StatusBadge tone={(c?.workers_healthy ?? 0) > 0 ? 'ok' : 'bad'}>
              Воркеры: {c?.workers_healthy ?? 0}
            </StatusBadge>
          </div>
        }
      />

      <ErrorText>{counters.error}</ErrorText>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label="Сообщений за сутки" value={c?.messages_today ?? 0} />
        <Stat label="Отправлено ответов" value={c?.replies_today ?? 0} />
        <Stat label="Чатов под наблюдением" value={c?.chats_monitored ?? 0} />
        <Stat label="Групп под наблюдением" value={c?.groups_monitored ?? 0} />
        <Stat label="Личных переписок под наблюдением" value={c?.private_monitored ?? 0} />
        <Stat label="Лидов всего" value={c?.leads_total ?? 0} />
        <Stat label="Аккаунты онлайн" value={`${online} / ${total}`} />
      </div>

      <div className="mt-8">
        <h2 className="mb-4 text-lg font-semibold text-slate-100">Лиды по направлениям</h2>
        <ErrorText>{leadsByScenario.error}</ErrorText>
        {directions.length === 0 ? (
          <Card>
            <Empty>Лидов по направлениям пока нет</Empty>
          </Card>
        ) : (
          <div className="grid gap-4 lg:grid-cols-2">
            {directions.map((direction) => (
              <Card key={direction.scenario_id ?? direction.name} title={direction.name}>
                <div className="mb-4 flex items-end gap-6">
                  <div>
                    <div className="text-3xl font-semibold text-slate-100">{direction.total}</div>
                    <div className="text-xs uppercase tracking-wide text-slate-500">
                      лидов за всё время
                    </div>
                  </div>
                  <div>
                    <div className="text-2xl font-medium text-amber-400">
                      +{weekSum(direction.series)}
                    </div>
                    <div className="text-xs uppercase tracking-wide text-slate-500">за неделю</div>
                  </div>
                </div>
                <BarChart points={direction.series} color="#eab308" />
              </Card>
            ))}
          </div>
        )}
      </div>
    </Shell>
  );
}

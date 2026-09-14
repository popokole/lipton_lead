'use client';

import { useId } from 'react';

import { Shell } from '@/components/shell';
import { Card, Empty, ErrorText, PageHeader, Stat, StatusBadge } from '@/components/ui';
import { useApi } from '@/lib/hooks';
import type { DailyPoint, DashboardCounters, ScenarioLeadStat } from '@/lib/types';

const WEEKDAYS = ['вс', 'пн', 'вт', 'ср', 'чт', 'пт', 'сб'];

// Палитра направлений: каждому свой цвет, чтобы графики различались.
const PALETTE = ['#f59e0b', '#38bdf8', '#34d399', '#a78bfa', '#f472b6', '#fb7185'];

interface Bar {
  key: string;
  label: string;
  value: number;
}

/** Разворачивает разрежённый ряд в полные 7 дней (сегодня — крайний справа). */
function lastSevenDays(series: DailyPoint[]): Bar[] {
  const byDay = new Map(series.map((point) => [point.day, point.value]));
  const bars: Bar[] = [];
  const today = new Date();
  for (let offset = 6; offset >= 0; offset -= 1) {
    const date = new Date(today);
    date.setDate(today.getDate() - offset);
    const key = date.toISOString().slice(0, 10);
    bars.push({ key, label: WEEKDAYS[date.getDay()], value: byDay.get(key) ?? 0 });
  }
  return bars;
}

/** Адаптивный area-график на inline-SVG (gradient fill + линия + точки).
 *  SVG вместо CSS-высот: проценты внутри flex не резолвятся, а SVG надёжен. */
function WeekAreaChart({ series, color }: { series: DailyPoint[]; color: string }) {
  const gradientId = useId();
  const bars = lastSevenDays(series);
  const n = bars.length;
  const W = 320;
  const H = 110;
  const padTop = 14;
  const padBottom = 6;
  const max = Math.max(...bars.map((bar) => bar.value), 1);

  const cx = (i: number) => ((i + 0.5) / n) * W;
  const cy = (value: number) => H - padBottom - (value / max) * (H - padTop - padBottom);

  const linePoints = bars.map((bar, i) => `${cx(i)},${cy(bar.value)}`);
  const areaPath =
    `M ${cx(0)},${H - padBottom} ` +
    bars.map((bar, i) => `L ${cx(i)},${cy(bar.value)}`).join(' ') +
    ` L ${cx(n - 1)},${H - padBottom} Z`;
  const linePath = `M ${linePoints.join(' L ')}`;

  return (
    <div>
      {/* значения над точками */}
      <div className="flex">
        {bars.map((bar) => (
          <div key={bar.key} className="flex-1 text-center text-[11px] font-medium text-slate-300">
            {bar.value || ''}
          </div>
        ))}
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 110 }} role="img">
        <defs>
          <linearGradient id={gradientId} x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={color} stopOpacity="0.45" />
            <stop offset="100%" stopColor={color} stopOpacity="0.02" />
          </linearGradient>
        </defs>
        <path d={areaPath} fill={`url(#${gradientId})`} />
        <path d={linePath} fill="none" stroke={color} strokeWidth="2.5" strokeLinejoin="round" strokeLinecap="round" />
        {bars.map((bar, i) => (
          <circle
            key={bar.key}
            cx={cx(i)}
            cy={cy(bar.value)}
            r={bar.value > 0 ? 3 : 2}
            fill={bar.value > 0 ? color : '#475569'}
          />
        ))}
      </svg>
      {/* подписи дней */}
      <div className="flex">
        {bars.map((bar) => (
          <div key={bar.key} className="flex-1 text-center text-[11px] text-slate-600">
            {bar.label}
          </div>
        ))}
      </div>
    </div>
  );
}

function Metric({ value, label, tone }: { value: number; label: string; tone?: string }) {
  return (
    <div>
      <div className={`text-xl font-semibold ${tone ?? 'text-slate-100'}`}>{value}</div>
      <div className="text-[11px] uppercase tracking-wide text-slate-500">{label}</div>
    </div>
  );
}

function DirectionCard({ stat, color }: { stat: ScenarioLeadStat; color: string }) {
  const conversion = stat.total > 0 ? Math.round((stat.converted / stat.total) * 100) : 0;
  return (
    <Card
      title={stat.name}
      actions={stat.hot > 0 ? <StatusBadge tone="bad">🔥 {stat.hot}</StatusBadge> : null}
    >
      <div className="mb-3 grid grid-cols-4 gap-3">
        <Metric value={stat.total} label="всего" />
        <Metric value={stat.week} label="за 7 дней" tone="text-amber-400" />
        <Metric value={stat.today} label="сегодня" tone="text-sky-400" />
        <Metric value={stat.converted} label={`продаж · ${conversion}%`} tone="text-emerald-400" />
      </div>
      <WeekAreaChart series={stat.series} color={color} />
    </Card>
  );
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
            {directions.map((direction, index) => (
              <DirectionCard
                key={direction.scenario_id ?? direction.name}
                stat={direction}
                color={PALETTE[index % PALETTE.length]}
              />
            ))}
          </div>
        )}
      </div>
    </Shell>
  );
}

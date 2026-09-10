'use client';

import {
  AlertTriangle,
  Bot,
  Brain,
  MessageSquare,
  PartyPopper,
  Send,
  UserCircle,
  Users,
  Zap,
} from 'lucide-react';
import type { ComponentType } from 'react';

import { Shell } from '@/components/shell';
import {
  Badge,
  BarChart,
  Card,
  Empty,
  EmptyState,
  ErrorText,
  PageHeader,
  Stat,
  StatusBadge,
  Table,
  Trend,
} from '@/components/ui';
import { useApi, useRealtime } from '@/lib/hooks';
import type { DashboardCounters, DashboardSeries, Escalation, Lead, RealtimeEvent, RuleStat } from '@/lib/types';

type PendingReview = { id: string };

interface StoryStat {
  viewed_today: number;
  liked_today: number;
  viewed_total: number;
  liked_total: number;
  reacted_today: number;
  reacted_total: number;
  recent: { name: string; username: string | null; liked: boolean; at: string }[];
}

const EVENT_META: Record<string, { icon: ComponentType<{ size?: number }>; label: string }> = {
  'account.status': { icon: UserCircle, label: 'Статус аккаунта изменился' },
  'worker.status': { icon: Bot, label: 'Статус воркера изменился' },
  'message.new': { icon: MessageSquare, label: 'Новое сообщение' },
  'ai.analysis': { icon: Brain, label: 'ИИ проанализировал сообщение' },
  'action.created': { icon: Zap, label: 'Создано действие' },
  'action.sent': { icon: Send, label: 'Ответ отправлен' },
  'lead.updated': { icon: Users, label: 'Лид обновлён' },
  'human.handoff': { icon: AlertTriangle, label: 'Передано оператору' },
  error: { icon: AlertTriangle, label: 'Ошибка' },
};

function timeAgo(iso: string): string {
  const diffMs = Date.now() - new Date(iso).getTime();
  const sec = Math.max(0, Math.round(diffMs / 1000));
  if (sec < 60) return 'только что';
  const min = Math.round(sec / 60);
  if (min < 60) return `${min} мин назад`;
  const hours = Math.round(min / 60);
  if (hours < 24) return `${hours} ч назад`;
  return `${Math.round(hours / 24)} дн назад`;
}

function eventDetail(event: RealtimeEvent): string | null {
  const p = event.payload;
  if (typeof p.status === 'string' && typeof p.score === 'number') return `${p.status} (${p.score})`;
  if (typeof p.text === 'string') return p.text.slice(0, 80);
  if (typeof p.reason === 'string') return p.reason.slice(0, 80);
  return null;
}

function lastAndPrev(series: { day: string; value: number }[]): [number, number] {
  if (series.length < 2) return [series.at(-1)?.value ?? 0, 0];
  return [series.at(-1)!.value, series.at(-2)!.value];
}

export default function OverviewPage() {
  const counters = useApi<DashboardCounters>('/analytics/dashboard', 10_000);
  const series = useApi<DashboardSeries>('/analytics/series?days=14', 60_000);
  const ruleStats = useApi<RuleStat[]>('/analytics/rules', 30_000);
  const handoff = useApi<Escalation[]>('/handoff', 20_000);
  const reviews = useApi<PendingReview[]>('/reviews', 20_000);
  const leads = useApi<Lead[]>('/leads?limit=200', 30_000);
  const stories = useApi<StoryStat>('/analytics/stories', 30_000);
  const realtime = useRealtime(25);

  const hotLeads = leads.data?.filter((l) => l.status === 'HOT').length ?? 0;
  const handoffCount = handoff.data?.length ?? 0;
  const reviewCount = reviews.data?.length ?? 0;
  const errorsToday = counters.data?.errors_today ?? 0;
  const attentionTotal = handoffCount + reviewCount + hotLeads + errorsToday;

  const [msgLast, msgPrev] = lastAndPrev(series.data?.messages ?? []);
  const [replyLast, replyPrev] = lastAndPrev(series.data?.replies ?? []);
  const [leadLast, leadPrev] = lastAndPrev(series.data?.leads ?? []);

  const aiDown = (counters.data?.accounts_online ?? 0) === 0;
  const aiTone = aiDown ? 'bad' : errorsToday > 0 ? 'warn' : 'ok';

  return (
    <Shell>
      <PageHeader
        title="Обзор"
        subtitle="Что происходит прямо сейчас"
        actions={
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge tone={aiTone} pulse={!aiDown}>
              AI {aiDown ? 'не в сети' : 'активен'}
            </StatusBadge>
            <StatusBadge tone={(counters.data?.accounts_online ?? 0) > 0 ? 'ok' : 'bad'}>
              Telegram {counters.data?.accounts_online ?? 0}/{counters.data?.accounts_total ?? 0}
            </StatusBadge>
            <StatusBadge tone={(counters.data?.workers_healthy ?? 0) > 0 ? 'ok' : 'bad'}>
              Воркеры: {counters.data?.workers_healthy ?? 0}
            </StatusBadge>
            <StatusBadge tone={realtime.connected ? 'ok' : 'mute'} pulse={realtime.connected}>
              {realtime.connected ? 'live' : 'нет связи'}
            </StatusBadge>
          </div>
        }
      />

      <ErrorText>{counters.error}</ErrorText>

      {attentionTotal === 0 ? (
        <Card tone="info" className="mb-6">
          <EmptyState
            icon={<PartyPopper size={28} />}
            title="Всё под контролем"
            description="Очередей нет, ИИ обработал всё сам."
          />
        </Card>
      ) : (
        <Card tone="primary" className="mb-6">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <div className="flex items-center gap-2 text-lg font-semibold text-slate-100">
                <AlertTriangle size={20} className="text-warning" />
                {attentionTotal} {attentionTotal === 1 ? 'событие требует' : 'событий требуют'} внимания
              </div>
              <ul className="mt-3 space-y-1.5 text-sm text-slate-400">
                {hotLeads > 0 && <li>🔥 {hotLeads} горячих лидов</li>}
                {handoffCount > 0 && <li>👤 {handoffCount} передано оператору</li>}
                {reviewCount > 0 && <li>✅ {reviewCount} ответов ИИ ждут подтверждения</li>}
                {errorsToday > 0 && <li>⚠️ {errorsToday} ошибок ИИ за сутки</li>}
              </ul>
            </div>
            <div className="flex flex-wrap gap-2">
              {handoffCount > 0 && (
                <a href="/handoff">
                  <Badge tone="warn">Открыть «Требует внимания»</Badge>
                </a>
              )}
              {reviewCount > 0 && (
                <a href="/reviews">
                  <Badge tone="info">Открыть «На подтверждение»</Badge>
                </a>
              )}
              {hotLeads > 0 && (
                <a href="/leads">
                  <Badge tone="bad">Открыть «Лиды»</Badge>
                </a>
              )}
            </div>
          </div>
        </Card>
      )}

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat
          label="Сообщений за сутки"
          value={counters.data?.messages_today ?? 0}
          hint={<Trend current={msgLast} previous={msgPrev} />}
        />
        <Stat
          label="Отправлено ответов"
          value={counters.data?.replies_today ?? 0}
          hint={<Trend current={replyLast} previous={replyPrev} />}
        />
        <Stat
          label="Лидов всего"
          value={counters.data?.leads_total ?? 0}
          hint={<Trend current={leadLast} previous={leadPrev} />}
        />
        <Stat label="Обращений к AI" value={counters.data?.ai_analyzed_today ?? 0} />
        <Stat label="Аккаунты онлайн" value={`${counters.data?.accounts_online ?? 0} / ${counters.data?.accounts_total ?? 0}`} />
        <Stat label="Чатов под наблюдением" value={counters.data?.chats_monitored ?? 0} />
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
          <BarChart points={series.data?.replies ?? []} color="#22c55e" />
        </Card>
        <Card title="Лиды по дням">
          <BarChart points={series.data?.leads ?? []} color="#eab308" />
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

      <Card title="Прогрев — истории и реакции" className="mt-6">
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Stat label="Просмотрено сегодня" value={stories.data?.viewed_today ?? 0} />
          <Stat label="Лайков сегодня" value={stories.data?.liked_today ?? 0} />
          <Stat label="Реакций на посты сегодня" value={stories.data?.reacted_today ?? 0} />
          <Stat label="Просмотрено всего" value={stories.data?.viewed_total ?? 0} />
          <Stat label="Лайков всего" value={stories.data?.liked_total ?? 0} />
          <Stat label="Реакций всего" value={stories.data?.reacted_total ?? 0} />
        </div>
        {(stories.data?.recent?.length ?? 0) > 0 && (
          <ul className="mt-4 flex flex-wrap gap-2">
            {stories.data?.recent.slice(0, 12).map((item, index) => (
              <li
                key={`${item.at}-${index}`}
                className="inline-flex items-center gap-1.5 rounded-lg border border-ink-700 bg-ink-900 px-2.5 py-1 text-xs text-slate-300"
              >
                <span>{item.liked ? '❤️' : '👁'}</span>
                <span className="max-w-[140px] truncate">{item.name || '?'}</span>
                {item.username && <span className="text-slate-500">@{item.username}</span>}
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card title="Живая лента" className="mt-6">
        {realtime.events.length === 0 ? (
          <Empty>
            Событий пока не было. Они появятся, как только аккаунт начнёт получать сообщения.
          </Empty>
        ) : (
          <ul className="space-y-2.5">
            {realtime.events.map((event, index) => {
              const meta = EVENT_META[event.type] ?? { icon: Zap, label: event.type };
              const Icon = meta.icon;
              const detail = eventDetail(event);
              return (
                <li key={`${event.ts}-${index}`} className="flex items-start gap-2.5 text-sm">
                  <Icon size={15} />
                  <span className="min-w-0 flex-1">
                    <span className="text-slate-300">{meta.label}</span>
                    {detail && <span className="text-slate-500"> — {detail}</span>}
                  </span>
                  <span className="shrink-0 text-xs text-slate-600">{timeAgo(event.ts)}</span>
                </li>
              );
            })}
          </ul>
        )}
      </Card>
    </Shell>
  );
}

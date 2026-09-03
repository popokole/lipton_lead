'use client';

import {
  Bot,
  MessageSquare,
  Server,
  ShieldAlert,
  Sparkles,
  Target,
} from 'lucide-react';
import Link from 'next/link';
import type { ComponentType } from 'react';

import { Shell } from '@/components/shell';
import { BarChart, Card, PageHeader } from '@/components/ui';
import { useApi } from '@/lib/hooks';
import type { DashboardCounters, DashboardSeries } from '@/lib/types';

interface TileDef {
  href: string;
  label: string;
  description: string;
  icon: ComponentType<{ size?: number; className?: string }>;
  stat: (c: DashboardCounters | null) => string;
  points: (s: DashboardSeries | null) => { day: string; value: number }[];
  color: string;
}

const TILES: TileDef[] = [
  {
    href: '/overview',
    label: 'Обзор',
    description: 'Сводка, графики, живая лента',
    icon: Server,
    stat: (c) => `${c?.messages_today ?? 0} сообщ. за сутки`,
    points: (s) => s?.messages ?? [],
    color: '#3b82f6',
  },
  {
    href: '/inbox',
    label: 'Общение',
    description: 'Личка и группы, ручные ответы',
    icon: MessageSquare,
    stat: (c) => `${c?.replies_today ?? 0} ответов за сутки`,
    points: (s) => s?.replies ?? [],
    color: '#22c55e',
  },
  {
    href: '/handoff',
    label: 'Внимания требует',
    description: 'Эскалации и подтверждения',
    icon: ShieldAlert,
    stat: (c) => `${c?.errors_today ?? 0} ошибок за сутки`,
    points: (s) => s?.errors ?? [],
    color: '#ef4444',
  },
  {
    href: '/scenarios',
    label: 'Настройка ИИ',
    description: 'Сценарии, правила, база знаний',
    icon: Sparkles,
    stat: (c) => `${c?.ai_analyzed_today ?? 0} обращений к AI`,
    points: (s) => s?.matches ?? [],
    color: '#eab308',
  },
  {
    href: '/leads',
    label: 'Лиды',
    description: 'Собранные контакты и статусы',
    icon: Target,
    stat: (c) => `${c?.leads_total ?? 0} всего`,
    points: (s) => s?.leads ?? [],
    color: '#3b82f6',
  },
  {
    href: '/accounts',
    label: 'Система',
    description: 'Аккаунты, воркеры, журнал',
    icon: Bot,
    stat: (c) => `${c?.workers_healthy ?? 0} живых воркеров`,
    points: (s) => s?.messages ?? [],
    color: '#94a3b8',
  },
];

export default function HomePage() {
  const counters = useApi<DashboardCounters>('/analytics/dashboard', 15_000);
  const series = useApi<DashboardSeries>('/analytics/series?days=14', 60_000);

  return (
    <Shell>
      <PageHeader title="Главная" subtitle="Выберите раздел" />
      <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
        {TILES.map((tile) => {
          const Icon = tile.icon;
          return (
            <Link key={tile.href} href={tile.href} className="block">
              <Card className="h-full transition hover:border-accent/50">
                <div className="flex items-center gap-3">
                  <div className="rounded-lg bg-ink-800 p-2 text-accent">
                    <Icon size={20} />
                  </div>
                  <div>
                    <div className="text-sm font-medium text-slate-100">{tile.label}</div>
                    <div className="text-xs text-slate-500">{tile.description}</div>
                  </div>
                </div>
                <div className="mt-4 text-lg font-semibold text-slate-200">
                  {tile.stat(counters.data)}
                </div>
                <div className="mt-2">
                  <BarChart points={tile.points(series.data)} color={tile.color} />
                </div>
              </Card>
            </Link>
          );
        })}
      </div>
    </Shell>
  );
}

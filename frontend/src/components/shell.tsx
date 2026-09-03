'use client';

import {
  BookOpen,
  Bot,
  ChevronDown,
  FlaskConical,
  Home,
  LayoutGrid,
  ListChecks,
  MessageSquare,
  Server,
  Settings,
  ShieldAlert,
  Sparkles,
  Target,
  Users,
} from 'lucide-react';
import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import type { ComponentType } from 'react';

import { api, logout, tokens } from '@/lib/api';
import type { CurrentUser } from '@/lib/types';

interface NavItem {
  href: string;
  label: string;
  icon: ComponentType<{ size?: number; className?: string }>;
}

interface NavGroup {
  section: string;
  icon: ComponentType<{ size?: number; className?: string }>;
  items: NavItem[];
}

const NAV: NavGroup[] = [
  { section: 'Обзор', icon: LayoutGrid, items: [{ href: '/overview', label: 'Обзор', icon: LayoutGrid }] },
  {
    section: 'Общение',
    icon: MessageSquare,
    items: [
      { href: '/inbox', label: 'Общение', icon: MessageSquare },
      { href: '/chats', label: 'Чаты', icon: MessageSquare },
      { href: '/conversations', label: 'Диалоги', icon: MessageSquare },
      { href: '/tree', label: 'Дерево чатов', icon: MessageSquare },
      { href: '/messages', label: 'Сообщения', icon: MessageSquare },
    ],
  },
  {
    section: 'Внимания требует',
    icon: ShieldAlert,
    items: [
      { href: '/handoff', label: 'Требует внимания', icon: ShieldAlert },
      { href: '/reviews', label: 'На подтверждение', icon: ListChecks },
    ],
  },
  {
    section: 'Настройка ИИ',
    icon: Sparkles,
    items: [
      { href: '/scenarios', label: 'Сценарии', icon: Sparkles },
      { href: '/rules', label: 'Правила', icon: Bot },
      { href: '/knowledge', label: 'База знаний', icon: BookOpen },
      { href: '/abtest', label: 'A/B заходов', icon: FlaskConical },
      { href: '/stoplist', label: 'Стоп-лист', icon: ShieldAlert },
    ],
  },
  { section: 'Лиды', icon: Target, items: [{ href: '/leads', label: 'Лиды', icon: Target }] },
  {
    section: 'Система',
    icon: Server,
    items: [
      { href: '/accounts', label: 'Аккаунты', icon: Users },
      { href: '/workers', label: 'Воркеры', icon: Server },
      { href: '/logs', label: 'Журнал', icon: ListChecks },
      { href: '/settings', label: 'Настройки', icon: Settings },
    ],
  },
];

function activeGroupSection(pathname: string): string | null {
  for (const group of NAV) {
    if (group.items.some((item) => (item.href === '/' ? pathname === '/' : pathname.startsWith(item.href)))) {
      return group.section;
    }
  }
  return null;
}

/**
 * Каркас панели с проверкой входа.
 *
 * Токена нет — уходим на страницу входа до того, как страница успеет
 * запросить данные и получить 401.
 */
export function Shell({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const [user, setUser] = useState<CurrentUser | null>(null);
  const [checked, setChecked] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [openSection, setOpenSection] = useState<string | null>(() => activeGroupSection(pathname));

  useEffect(() => {
    if (!tokens.access()) {
      router.replace('/login');
      return;
    }
    api
      .get<CurrentUser>('/auth/me')
      .then(setUser)
      .catch(() => undefined)
      .finally(() => setChecked(true));
  }, [router]);

  // Навигация закрывает мобильное меню и разворачивает группу текущего раздела.
  useEffect(() => {
    setMenuOpen(false);
    const current = activeGroupSection(pathname);
    if (current) setOpenSection(current);
  }, [pathname]);

  if (!checked) {
    return <div className="p-8 text-sm text-slate-500">Загрузка…</div>;
  }

  const sidebar = (
    <>
      <div className="px-2 pb-5">
        <div className="text-sm font-semibold text-slate-100">Telegram AI</div>
        <div className="text-xs text-slate-500">панель управления</div>
      </div>
      <nav className="space-y-0.5">
        <Link
          href="/"
          className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition ${
            pathname === '/' ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:bg-ink-800'
          }`}
        >
          <Home size={16} />
          Главная
        </Link>
        <div className="my-2 border-t border-ink-800" />
        {NAV.map((group) => {
          if (group.items.length === 1) {
            const item = group.items[0];
            const active = pathname.startsWith(item.href);
            const Icon = item.icon;
            return (
              <Link
                key={group.section}
                href={item.href}
                className={`flex items-center gap-2.5 rounded-lg px-3 py-2 text-sm transition ${
                  active ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:bg-ink-800'
                }`}
              >
                <Icon size={16} />
                {item.label}
              </Link>
            );
          }

          const GroupIcon = group.icon;
          const expanded = openSection === group.section;
          return (
            <div key={group.section}>
              <button
                onClick={() => setOpenSection(expanded ? null : group.section)}
                className="flex w-full items-center justify-between gap-2.5 rounded-lg px-3 py-2 text-sm text-slate-400 transition hover:bg-ink-800"
              >
                <span className="flex items-center gap-2.5">
                  <GroupIcon size={16} />
                  {group.section}
                </span>
                <ChevronDown
                  size={14}
                  className={`transition-transform ${expanded ? 'rotate-180' : ''}`}
                />
              </button>
              {expanded && (
                <div className="ml-3 space-y-0.5 border-l border-ink-800 pl-3">
                  {group.items.map((item) => {
                    const active = pathname.startsWith(item.href);
                    const Icon = item.icon;
                    return (
                      <Link
                        key={item.href}
                        href={item.href}
                        className={`flex items-center gap-2.5 rounded-lg px-3 py-1.5 text-sm transition ${
                          active ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:bg-ink-800'
                        }`}
                      >
                        <Icon size={15} />
                        {item.label}
                      </Link>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </nav>
      <div className="mt-6 border-t border-ink-800 px-3 pt-4">
        <div className="truncate text-xs text-slate-400">{user?.email ?? '—'}</div>
        <div className="text-xs text-slate-600">{user?.role ?? ''}</div>
        <button
          onClick={logout}
          className="mt-3 text-xs text-slate-500 underline-offset-2 hover:text-slate-300 hover:underline"
        >
          Выйти
        </button>
      </div>
    </>
  );

  return (
    <div className="flex min-h-screen flex-col lg:flex-row">
      {/* Верхняя панель с бургером — только на мобиле */}
      <header className="sticky top-0 z-30 flex items-center gap-3 border-b border-ink-800 bg-ink-900/80 px-4 py-3 backdrop-blur lg:hidden">
        <button
          onClick={() => setMenuOpen(true)}
          aria-label="Меню"
          className="rounded-lg border border-ink-700 px-2.5 py-1.5 text-slate-300"
        >
          ☰
        </button>
        <span className="text-sm font-semibold text-slate-100">Telegram AI</span>
      </header>

      {/* Статичный сайдбар — на десктопе */}
      <aside className="hidden w-56 shrink-0 border-r border-ink-800 bg-ink-900/60 px-3 py-5 lg:block">
        {sidebar}
      </aside>

      {/* Выезжающее меню — на мобиле */}
      {menuOpen && (
        <div className="fixed inset-0 z-40 lg:hidden">
          <div
            className="absolute inset-0 bg-black/60"
            onClick={() => setMenuOpen(false)}
            aria-hidden
          />
          <aside className="absolute left-0 top-0 h-full w-64 overflow-y-auto border-r border-ink-800 bg-ink-900 px-3 py-5">
            {sidebar}
          </aside>
        </div>
      )}

      <main className="min-w-0 flex-1 overflow-x-hidden px-4 py-5 lg:px-8 lg:py-7">{children}</main>
    </div>
  );
}

'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { api, logout, tokens } from '@/lib/api';
import type { CurrentUser } from '@/lib/types';

const NAV = [
  { href: '/', label: 'Дашборд' },
  { href: '/handoff', label: 'Требует внимания' },
  { href: '/reviews', label: 'На подтверждение' },
  { href: '/accounts', label: 'Аккаунты' },
  { href: '/chats', label: 'Чаты' },
  { href: '/tree', label: 'Дерево чатов' },
  { href: '/scenarios', label: 'Сценарии' },
  { href: '/abtest', label: 'A/B заходов' },
  { href: '/knowledge', label: 'База знаний' },
  { href: '/rules', label: 'Правила' },
  { href: '/stoplist', label: 'Стоп-лист' },
  { href: '/messages', label: 'Сообщения' },
  { href: '/conversations', label: 'Диалоги' },
  { href: '/inbox', label: 'Общение' },
  { href: '/leads', label: 'Лиды' },
  { href: '/logs', label: 'Журнал' },
  { href: '/workers', label: 'Воркеры' },
  { href: '/settings', label: 'Настройки' },
];

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

  // Навигация закрывает мобильное меню.
  useEffect(() => {
    setMenuOpen(false);
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
        {NAV.map((item) => {
          const active = item.href === '/' ? pathname === '/' : pathname.startsWith(item.href);
          return (
            <Link
              key={item.href}
              href={item.href}
              className={`block rounded-lg px-3 py-2 text-sm transition ${
                active ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:bg-ink-800'
              }`}
            >
              {item.label}
            </Link>
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

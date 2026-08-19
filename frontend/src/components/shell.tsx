'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';

import { api, logout, tokens } from '@/lib/api';
import type { CurrentUser } from '@/lib/types';

const NAV = [
  { href: '/', label: 'Дашборд' },
  { href: '/handoff', label: 'Требует внимания' },
  { href: '/accounts', label: 'Аккаунты' },
  { href: '/chats', label: 'Чаты' },
  { href: '/tree', label: 'Дерево чатов' },
  { href: '/scenarios', label: 'Сценарии' },
  { href: '/rules', label: 'Правила' },
  { href: '/messages', label: 'Сообщения' },
  { href: '/conversations', label: 'Диалоги' },
  { href: '/leads', label: 'Лиды' },
  { href: '/logs', label: 'Журнал' },
  { href: '/workers', label: 'Воркеры' },
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

  if (!checked) {
    return <div className="p-8 text-sm text-slate-500">Загрузка…</div>;
  }

  return (
    <div className="flex min-h-screen">
      <aside className="w-56 shrink-0 border-r border-ink-800 bg-ink-900/60 px-3 py-5">
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
      </aside>
      <main className="flex-1 overflow-x-hidden px-8 py-7">{children}</main>
    </div>
  );
}

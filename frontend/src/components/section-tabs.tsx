'use client';

import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';

export interface TabDef {
  key: string;
  label: string;
  badge?: number;
  element: ReactNode;
}

/** Горизонтальные вкладки внутри одного раздела: монтируется только активная
 *  (каждый view сам грузит данные, когда открыт). Последняя вкладка запоминается
 *  в localStorage по ключу раздела. */
export function SectionTabs({ id, tabs }: { id: string; tabs: TabDef[] }) {
  const first = tabs[0]?.key ?? '';
  const [active, setActive] = useState(first);

  useEffect(() => {
    try {
      const saved = localStorage.getItem(`tgai.tab.${id}`);
      if (saved && tabs.some((tab) => tab.key === saved)) setActive(saved);
    } catch {
      /* приватный режим — просто первая вкладка */
    }
  }, [id, tabs]);

  function select(key: string) {
    setActive(key);
    try {
      localStorage.setItem(`tgai.tab.${id}`, key);
    } catch {
      /* не критично */
    }
  }

  const current = tabs.find((tab) => tab.key === active) ?? tabs[0];

  return (
    <div>
      <div className="mb-6 flex flex-wrap gap-1 border-b border-ink-700">
        {tabs.map((tab) => {
          const isActive = tab.key === current?.key;
          return (
            <button
              key={tab.key}
              type="button"
              onClick={() => select(tab.key)}
              className={`-mb-px flex items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition-colors ${
                isActive
                  ? 'border-accent text-slate-100'
                  : 'border-transparent text-slate-500 hover:text-slate-300'
              }`}
            >
              {tab.label}
              {tab.badge ? (
                <span className="rounded-full bg-rose-500/20 px-1.5 text-xs font-semibold text-rose-300">
                  {tab.badge}
                </span>
              ) : null}
            </button>
          );
        })}
      </div>
      {current?.element}
    </div>
  );
}

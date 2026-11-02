'use client';

import { useEffect, useState } from 'react';
import type { ReactNode } from 'react';

import { TabBar } from '@/components/ui';

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
      <TabBar
        tabs={tabs.map(({ key, label, badge }) => ({ key, label, badge }))}
        value={current?.key ?? ''}
        onChange={select}
      />
      {current?.element}
    </div>
  );
}

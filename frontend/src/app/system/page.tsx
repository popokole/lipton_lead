'use client';

import { Shell } from '@/components/shell';
import { SectionTabs } from '@/components/section-tabs';

import { AccountsView } from '../accounts/page';
import { LogsView } from '../logs/page';
import { SettingsView } from '../settings/page';
import { WorkersView } from '../workers/page';

export default function SystemPage() {
  return (
    <Shell>
      <SectionTabs
        id="system"
        tabs={[
          { key: 'accounts', label: 'Аккаунты', element: <AccountsView /> },
          { key: 'workers', label: 'Воркеры', element: <WorkersView /> },
          { key: 'logs', label: 'Журнал', element: <LogsView /> },
          { key: 'settings', label: 'Настройки', element: <SettingsView /> },
        ]}
      />
    </Shell>
  );
}

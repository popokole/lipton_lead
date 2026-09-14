'use client';

import { Shell } from '@/components/shell';
import { SectionTabs } from '@/components/section-tabs';

import { AccountsView } from '../accounts/view';
import { LogsView } from '../logs/view';
import { SettingsView } from '../settings/view';
import { WorkersView } from '../workers/view';

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

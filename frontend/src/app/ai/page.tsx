'use client';

import { Shell } from '@/components/shell';
import { SectionTabs } from '@/components/section-tabs';

import { AbTestView } from '../abtest/page';
import { KnowledgeView } from '../knowledge/page';
import { RulesView } from '../rules/page';
import { ScenariosView } from '../scenarios/page';
import { StoplistView } from '../stoplist/page';

export default function AiSettingsPage() {
  return (
    <Shell>
      <SectionTabs
        id="ai"
        tabs={[
          { key: 'scenarios', label: 'Сценарии', element: <ScenariosView /> },
          { key: 'rules', label: 'Правила', element: <RulesView /> },
          { key: 'knowledge', label: 'База знаний', element: <KnowledgeView /> },
          { key: 'abtest', label: 'A/B заходов', element: <AbTestView /> },
          { key: 'stoplist', label: 'Стоп-лист', element: <StoplistView /> },
        ]}
      />
    </Shell>
  );
}

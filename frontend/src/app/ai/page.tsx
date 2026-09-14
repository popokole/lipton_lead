'use client';

import { Shell } from '@/components/shell';
import { SectionTabs } from '@/components/section-tabs';

import { AbTestView } from '../abtest/view';
import { KnowledgeView } from '../knowledge/view';
import { RulesView } from '../rules/view';
import { ScenariosView } from '../scenarios/view';
import { StoplistView } from '../stoplist/view';

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

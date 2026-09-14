'use client';

import { Shell } from '@/components/shell';
import { SectionTabs } from '@/components/section-tabs';

import { HandoffView } from '../handoff/view';
import { ReviewsView } from '../reviews/view';

export default function AttentionPage() {
  return (
    <Shell>
      <SectionTabs
        id="attention"
        tabs={[
          { key: 'handoff', label: 'Требует внимания', element: <HandoffView /> },
          { key: 'reviews', label: 'На подтверждение', element: <ReviewsView /> },
        ]}
      />
    </Shell>
  );
}

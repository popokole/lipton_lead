'use client';

import { Shell } from '@/components/shell';
import { SectionTabs } from '@/components/section-tabs';

import { ChatsView } from '../chats/page';
import { ConversationsView } from '../conversations/page';
import { InboxView } from '../inbox/page';
import { MessagesView } from '../messages/page';
import { TreeView } from '../tree/page';

export default function CommunicationPage() {
  return (
    <Shell>
      <SectionTabs
        id="communication"
        tabs={[
          { key: 'inbox', label: 'Общение', element: <InboxView /> },
          { key: 'chats', label: 'Чаты', element: <ChatsView /> },
          { key: 'dialogs', label: 'Диалоги', element: <ConversationsView /> },
          { key: 'activity', label: 'Активность', element: <TreeView /> },
          { key: 'messages', label: 'Сообщения', element: <MessagesView /> },
        ]}
      />
    </Shell>
  );
}

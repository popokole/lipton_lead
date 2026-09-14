'use client';

import { Shell } from '@/components/shell';
import { SectionTabs } from '@/components/section-tabs';

import { ChatsView } from '../chats/view';
import { ConversationsView } from '../conversations/view';
import { InboxView } from '../inbox/view';
import { MessagesView } from '../messages/view';
import { TreeView } from '../tree/view';

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

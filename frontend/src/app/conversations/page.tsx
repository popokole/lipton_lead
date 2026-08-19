'use client';

import { Shell } from '@/components/shell';
import { Badge, Card, Empty, ErrorText, PageHeader, Table, statusTone } from '@/components/ui';
import { useApi } from '@/lib/hooks';
import type { Conversation } from '@/lib/types';

export default function ConversationsPage() {
  const conversations = useApi<Conversation[]>('/conversations?limit=100', 10_000);

  return (
    <Shell>
      <PageHeader
        title="Диалоги"
        subtitle="Статус HUMAN_REQUIRED означает, что диалог ждёт оператора"
      />
      <ErrorText>{conversations.error}</ErrorText>

      <Card>
        {(conversations.data?.length ?? 0) === 0 ? (
          <Empty>Диалогов пока нет</Empty>
        ) : (
          <Table head={['Собеседник', 'Статус', 'Сообщений', 'Ответов подряд', 'Пересказ', 'Активность']}>
            {conversations.data?.map((conversation) => (
              <tr key={conversation.id} className="border-b border-ink-800/70 last:border-0">
                <td className="py-3 pr-4 text-slate-200">{conversation.peer_tg_id}</td>
                <td className="py-3 pr-4">
                  <Badge tone={statusTone(conversation.status)}>{conversation.status}</Badge>
                </td>
                <td className="py-3 pr-4 text-slate-400">{conversation.message_count}</td>
                <td className="py-3 pr-4 text-slate-400">{conversation.ai_replies_in_row}</td>
                <td className="max-w-md py-3 pr-4 text-xs text-slate-500">
                  {conversation.summary ?? '—'}
                </td>
                <td className="py-3 text-xs text-slate-500">
                  {conversation.last_message_at
                    ? new Date(conversation.last_message_at).toLocaleString('ru')
                    : '—'}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </Shell>
  );
}

'use client';

import { useState } from 'react';

import { Shell } from '@/components/shell';
import { Badge, Button, Card, Empty, ErrorText, PageHeader, Table, inputClass , ChatAvatar} from '@/components/ui';
import { useApi } from '@/lib/hooks';
import type { LogRow, Page } from '@/lib/types';

const TYPES = [
  '',
  'MESSAGE_RECEIVED',
  'MESSAGE_SKIPPED',
  'RULE_MATCH',
  'AI_ANALYSIS',
  'AI_GENERATION',
  'ACTION_SENT',
  'ACTION_FAILED',
  'HUMAN_HANDOFF',
  'ACCOUNT_CONNECTED',
  'ACCOUNT_DISCONNECTED',
  'ERROR',
];

export default function LogsPage() {
  const [eventType, setEventType] = useState('');
  const logs = useApi<Page<LogRow>>(
    `/logs?limit=150${eventType ? `&event_type=${eventType}` : ''}`,
    5000,
  );

  return (
    <Shell>
      <PageHeader title="Журнал" subtitle="Путь каждого сообщения через конвейер" />
      <ErrorText>{logs.error}</ErrorText>

      <Card className="mb-6">
        <div className="flex flex-wrap items-center gap-3">
          <select
            className={`${inputClass} max-w-xs`}
            value={eventType}
            onChange={(event) => setEventType(event.target.value)}
          >
            {TYPES.map((type) => (
              <option key={type} value={type}>
                {type || 'все события'}
              </option>
            ))}
          </select>
          <Button variant="ghost" onClick={() => void logs.reload()}>
            Обновить
          </Button>
          <span className="text-xs text-slate-600">записей: {logs.data?.total ?? 0}</span>
        </div>
      </Card>

      <Card>
        {(logs.data?.items.length ?? 0) === 0 ? (
          <Empty>Записей нет</Empty>
        ) : (
          <Table head={['Время', 'Событие', 'Канал', 'Статус', 'Подробности']}>
            {logs.data?.items.map((row) => (
              <tr key={row.id} className="border-b border-ink-800/70 last:border-0">
                <td className="whitespace-nowrap py-2 pr-4 text-xs text-slate-500">
                  {new Date(row.ts).toLocaleString('ru')}
                </td>
                <td className="py-2 pr-4">
                  <Badge tone={row.level === 'WARNING' ? 'warn' : 'mute'}>{row.event_type}</Badge>
                  </td>
                <td className="py-2 pr-4">
                  {row.chat_id ? (
                    <div className="flex items-center gap-2">
                      <ChatAvatar chatId={row.chat_id} title={row.chat_title} hasAvatar={row.chat_has_avatar} size={22} />
                      <span className="max-w-[130px] truncate text-xs text-slate-400">{row.chat_title ?? (row.chat_username ? `@${row.chat_username}` : '—')}</span>
                    </div>
                  ) : (
                    <span className="text-xs text-slate-600">—</span>
                  )}
                </td>
                <td className="py-2 pr-4 text-xs text-slate-400">{row.status ?? '—'}</td>
                <td className="max-w-xl py-2 font-mono text-xs text-slate-500">
                  {row.error ? (
                    <span className="text-rose-300">{row.error}</span>
                  ) : (
                    JSON.stringify(row.extra ?? {})
                  )}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </Shell>
  );
}

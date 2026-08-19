'use client';

import { useState } from 'react';

import { Shell } from '@/components/shell';
import {
  Badge,
  Button,
  Card,
  ChatAvatar,
  Empty,
  ErrorText,
  PageHeader,
  Table,
  inputClass,
  statusTone,
} from '@/components/ui';
import { useApi, useRealtime } from '@/lib/hooks';
import type { ActionRow, Message, Page } from '@/lib/types';

export default function MessagesPage() {
  const [search, setSearch] = useState('');
  const [matchedOnly, setMatchedOnly] = useState(false);
  const query = `/messages?limit=50&matched_only=${matchedOnly}${
    search ? `&search=${encodeURIComponent(search)}` : ''
  }`;

  const messages = useApi<Page<Message>>(query, 8000);
  const actions = useApi<Page<ActionRow>>('/actions?limit=25', 8000);
  const realtime = useRealtime(40);

  return (
    <Shell>
      <PageHeader
        title="Сообщения"
        subtitle="Сохраняются сообщения отслеживаемых чатов и личных диалогов"
        actions={
          <span className={`text-xs ${realtime.connected ? 'text-emerald-400' : 'text-slate-500'}`}>
            {realtime.connected ? '● живая лента' : '○ лента отключена'}
          </span>
        }
      />
      <ErrorText>{messages.error}</ErrorText>

      <Card className="mb-6">
        <div className="flex flex-wrap items-center gap-3">
          <input
            className={`${inputClass} max-w-xs`}
            placeholder="Поиск по тексту"
            value={search}
            onChange={(event) => setSearch(event.target.value)}
          />
          <label className="flex items-center gap-2 text-sm text-slate-400">
            <input
              type="checkbox"
              checked={matchedOnly}
              onChange={(event) => setMatchedOnly(event.target.checked)}
              className="h-4 w-4 rounded border-ink-600 bg-ink-950"
            />
            только совпавшие с правилом
          </label>
          <Button variant="ghost" onClick={() => void messages.reload()}>
            Обновить
          </Button>
          <span className="text-xs text-slate-600">всего: {messages.data?.total ?? 0}</span>
        </div>
      </Card>

      <Card title="Последние сообщения" className="mb-6">
        {(messages.data?.items.length ?? 0) === 0 ? (
          <Empty>Сообщений пока нет</Empty>
        ) : (
          <Table head={['Время', 'Канал', 'Отправитель', 'Текст', 'Статус']}>
            {messages.data?.items.map((message) => (
              <tr key={message.id} className="border-b border-ink-800/70 last:border-0">
                <td className="whitespace-nowrap py-2 pr-4 text-xs text-slate-500">
                  {new Date(message.date).toLocaleString('ru')}
                </td>
                <td className="py-2 pr-4">
                  <div className="flex items-center gap-2">
                    <ChatAvatar
                      chatId={message.chat_id}
                      title={message.chat_title ?? String(message.tg_chat_id)}
                      hasAvatar={message.chat_has_avatar}
                    />
                    <span className="max-w-[140px] truncate text-xs text-slate-400">
                      {message.chat_title ??
                        (message.chat_username ? `@${message.chat_username}` : message.tg_chat_id)}
                    </span>
                  </div>
                </td>
                <td className="py-2 pr-4 text-xs">
                  {message.is_bot_reply ? (
                    <span className="text-emerald-300">наш ответ</span>
                  ) : (
                    <span className="text-slate-300">
                      {message.sender_display_name ??
                        (message.sender_username ? `@${message.sender_username}` : message.sender_tg_id)}
                    </span>
                  )}
                </td>
                <td className="max-w-xl py-2 pr-4 text-slate-300">{message.text ?? '—'}</td>
                <td className="py-2">
                  <Badge tone={statusTone(message.processed_status)}>{message.processed_status}</Badge>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      <div className="grid gap-4 lg:grid-cols-2">
        <Card title="Действия">
          {(actions.data?.items.length ?? 0) === 0 ? (
            <Empty>Действий пока не было</Empty>
          ) : (
            <ul className="space-y-2 text-sm">
              {actions.data?.items.map((action) => (
                <li key={action.id} className="rounded-lg border border-ink-800 px-3 py-2">
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-slate-300">{action.type}</span>
                    <Badge tone={statusTone(action.status)}>{action.status}</Badge>
                  </div>
                  {action.reply_text && (
                    <p className="mt-1 text-xs text-slate-400">{action.reply_text}</p>
                  )}
                  {action.error && <p className="mt-1 text-xs text-rose-300">{action.error}</p>}
                  {action.validation && !action.validation.passed && (
                    <p className="mt-1 text-xs text-amber-300">
                      не прошло проверку:{' '}
                      {action.validation.checks
                        .filter((check) => !check.passed)
                        .map((check) => check.reason)
                        .join('; ')}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </Card>

        <Card title="Живая лента событий">
          {realtime.events.length === 0 ? (
            <Empty>Ждём событий от воркера</Empty>
          ) : (
            <ul className="space-y-1 font-mono text-xs text-slate-400">
              {realtime.events.map((event, index) => (
                <li key={`${event.ts}-${index}`} className="truncate">
                  <span className="text-slate-600">{event.ts.slice(11, 19)}</span>{' '}
                  <span className="text-accent">{event.type}</span>{' '}
                  {JSON.stringify(event.payload).slice(0, 120)}
                </li>
              ))}
            </ul>
          )}
        </Card>
      </div>
    </Shell>
  );
}

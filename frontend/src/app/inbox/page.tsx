'use client';

import { useEffect, useRef, useState } from 'react';

import { Shell } from '@/components/shell';
import { Badge, Button, Empty, ErrorText, PageHeader, inputClass, statusTone } from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi } from '@/lib/hooks';
import type { Message, Thread } from '@/lib/types';

function peerName(thread: Thread): string {
  return (
    thread.display_name ||
    (thread.username ? `@${thread.username}` : null) ||
    `id ${thread.peer_tg_id}`
  );
}

/**
 * «Общение» — двухпанельный инбокс.
 *
 * Список слева — только диалоги, у которых есть лид (с кем реально общаемся).
 * Справа — переписка в личке и поле для ручного ответа от имени аккаунта.
 */
export default function InboxPage() {
  const threads = useApi<Thread[]>('/conversations/threads?limit=200', 8_000);
  const [selected, setSelected] = useState<string | null>(null);

  // При первой загрузке открываем самый свежий тред.
  useEffect(() => {
    if (selected === null && threads.data && threads.data.length > 0) {
      setSelected(threads.data[0].conversation_id);
    }
  }, [threads.data, selected]);

  const active = threads.data?.find((thread) => thread.conversation_id === selected) ?? null;

  return (
    <Shell>
      <PageHeader
        title="Общение"
        subtitle="Диалоги с лидами: переписка в личке и ручной ответ от имени аккаунта"
      />
      <ErrorText>{threads.error}</ErrorText>

      <div className="grid gap-4 lg:grid-cols-[320px_1fr]">
        <div className="max-h-[72vh] overflow-y-auto rounded-xl border border-ink-800 bg-ink-900/40">
          {(threads.data?.length ?? 0) === 0 ? (
            <div className="p-4">
              <Empty>Пока не с кем общаться — лиды появятся после первых ответов</Empty>
            </div>
          ) : (
            <ul className="divide-y divide-ink-800/70">
              {threads.data?.map((thread) => {
                const on = thread.conversation_id === selected;
                return (
                  <li key={thread.conversation_id}>
                    <button
                      onClick={() => setSelected(thread.conversation_id)}
                      className={`block w-full px-4 py-3 text-left transition ${
                        on ? 'bg-accent-soft' : 'hover:bg-ink-800/60'
                      }`}
                    >
                      <div className="flex items-center justify-between gap-2">
                        <span className="truncate text-sm font-medium text-slate-100">
                          {peerName(thread)}
                        </span>
                        {thread.awaiting_reply && (
                          <span className="h-2 w-2 shrink-0 rounded-full bg-accent" title="Ждёт ответа" />
                        )}
                      </div>
                      <div className="mt-0.5 truncate text-xs text-slate-500">
                        {thread.last_text ?? '—'}
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {active ? (
          <ThreadView key={active.conversation_id} thread={active} onSent={threads.reload} />
        ) : (
          <div className="flex min-h-[40vh] items-center justify-center rounded-xl border border-ink-800 bg-ink-900/40">
            <Empty>Выберите диалог слева</Empty>
          </div>
        )}
      </div>
    </Shell>
  );
}

function ThreadView({ thread, onSent }: { thread: Thread; onSent: () => void }) {
  const messages = useApi<Message[]>(`/conversations/${thread.conversation_id}/messages`, 5_000);
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  // Прокручиваем к последней реплике при обновлении ленты.
  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [messages.data]);

  const send = async () => {
    const body = text.trim();
    if (!body || sending) return;
    setSending(true);
    setError(null);
    try {
      await api.post<Message>(`/conversations/${thread.conversation_id}/reply`, { text: body });
      setText('');
      await messages.reload();
      onSent();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setSending(false);
    }
  };

  return (
    <div className="flex max-h-[72vh] flex-col rounded-xl border border-ink-800 bg-ink-900/40">
      <div className="flex items-center justify-between gap-3 border-b border-ink-800 px-4 py-3">
        <div className="min-w-0">
          <div className="truncate text-sm font-medium text-slate-100">{peerName(thread)}</div>
          <div className="text-xs text-slate-500">
            {thread.username ? `@${thread.username} · ` : ''}
            {thread.message_count} сообщений
          </div>
        </div>
        <Badge tone={statusTone(thread.status)}>{thread.status}</Badge>
      </div>

      <div className="flex-1 space-y-2 overflow-y-auto px-4 py-4">
        <ErrorText>{messages.error}</ErrorText>
        {(messages.data?.length ?? 0) === 0 ? (
          <Empty>В этом диалоге пока нет сохранённых сообщений</Empty>
        ) : (
          messages.data?.map((message) => {
            const mine = !message.is_incoming;
            return (
              <div key={message.id} className={`flex ${mine ? 'justify-end' : 'justify-start'}`}>
                <div
                  className={`max-w-[78%] rounded-2xl px-3 py-2 text-sm ${
                    mine
                      ? 'rounded-br-sm bg-accent text-white'
                      : 'rounded-bl-sm bg-ink-800 text-slate-200'
                  }`}
                >
                  <div className="whitespace-pre-wrap break-words">{message.text ?? '—'}</div>
                  <div className={`mt-1 text-[10px] ${mine ? 'text-white/60' : 'text-slate-500'}`}>
                    {new Date(message.date).toLocaleString('ru')}
                    {mine && message.is_bot_reply ? ' · авто' : ''}
                  </div>
                </div>
              </div>
            );
          })
        )}
        <div ref={bottomRef} />
      </div>

      <div className="border-t border-ink-800 px-4 py-3">
        {error && <div className="mb-2 text-xs text-rose-300">{error}</div>}
        <div className="flex items-end gap-2">
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
                event.preventDefault();
                void send();
              }
            }}
            rows={2}
            placeholder="Написать от имени аккаунта… (Ctrl+Enter — отправить)"
            className={`${inputClass} resize-none`}
          />
          <Button onClick={() => void send()} disabled={sending || text.trim().length === 0}>
            {sending ? '…' : 'Отправить'}
          </Button>
        </div>
      </div>
    </div>
  );
}

'use client';

import { useEffect, useRef, useState } from 'react';

import { Shell } from '@/components/shell';
import {
  Badge,
  Button,
  ChatAvatar,
  Empty,
  ErrorText,
  PageHeader,
  inputClass,
} from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi } from '@/lib/hooks';
import type { Message, Thread } from '@/lib/types';

type Kind = 'dm' | 'group';

function threadName(thread: Thread): string {
  return (
    thread.title ||
    (thread.username ? `@${thread.username}` : null) ||
    (thread.kind === 'dm' ? `id ${thread.tg_chat_id}` : `чат ${thread.tg_chat_id}`)
  );
}

/**
 * «Общение» — инбокс по ЧАТАМ.
 *
 * Личка и группы разделены вкладками и никогда не смешиваются: тред привязан к
 * чату, а не к человеку. Справа — переписка выбранного чата и ручной ответ от
 * имени аккаунта.
 */
export default function InboxPage() {
  const [kind, setKind] = useState<Kind>('dm');
  const threads = useApi<Thread[]>(`/conversations/threads?kind=${kind}&limit=300`, 8_000);
  const [selected, setSelected] = useState<string | null>(null);

  // При смене вкладки сбрасываем выбор — id чата из другой категории не подходит.
  useEffect(() => {
    setSelected(null);
  }, [kind]);

  // Автовыбор всегда сверяется с ТЕКУЩИМ списком: если выбранного чата в нём
  // нет (сменили вкладку, он исчез из выдачи) — открываем самый свежий. Так
  // правая панель не залипает пустой на устаревшем id из другой вкладки.
  useEffect(() => {
    if (!threads.data) return;
    const stillThere = selected !== null && threads.data.some((t) => t.chat_id === selected);
    if (!stillThere) {
      setSelected(threads.data.length > 0 ? threads.data[0].chat_id : null);
    }
  }, [threads.data, selected]);

  const active = threads.data?.find((thread) => thread.chat_id === selected) ?? null;

  return (
    <Shell>
      <PageHeader
        title="Общение"
        subtitle="Личные диалоги и группы — раздельно. Переписка и ручной ответ от имени аккаунта"
      />

      <div className="mb-4 inline-flex rounded-lg border border-ink-800 bg-ink-900/40 p-1 text-sm">
        <button
          onClick={() => setKind('dm')}
          className={`rounded-md px-4 py-1.5 transition ${
            kind === 'dm' ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Личка
        </button>
        <button
          onClick={() => setKind('group')}
          className={`rounded-md px-4 py-1.5 transition ${
            kind === 'group' ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          Группы
        </button>
      </div>

      <ErrorText>{threads.error}</ErrorText>

      <div className="grid gap-4 lg:grid-cols-[340px_1fr]">
        <div className="max-h-[70vh] overflow-y-auto rounded-xl border border-ink-800 bg-ink-900/40">
          {(threads.data?.length ?? 0) === 0 ? (
            <div className="p-4">
              <Empty>
                {kind === 'dm' ? 'Личных диалогов пока нет' : 'Групп с перепиской пока нет'}
              </Empty>
            </div>
          ) : (
            <ul className="divide-y divide-ink-800/70">
              {threads.data?.map((thread) => {
                const on = thread.chat_id === selected;
                return (
                  <li key={thread.chat_id}>
                    <button
                      onClick={() => setSelected(thread.chat_id)}
                      className={`flex w-full items-center gap-3 px-3 py-3 text-left transition ${
                        on ? 'bg-accent-soft' : 'hover:bg-ink-800/60'
                      }`}
                    >
                      <ChatAvatar
                        chatId={thread.chat_id}
                        title={threadName(thread)}
                        hasAvatar={thread.has_avatar}
                        size={36}
                      />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center justify-between gap-2">
                          <span className="truncate text-sm font-medium text-slate-100">
                            {threadName(thread)}
                          </span>
                          {thread.awaiting_reply && (
                            <span
                              className="h-2 w-2 shrink-0 rounded-full bg-accent"
                              title="Ждёт ответа"
                            />
                          )}
                        </div>
                        <div className="mt-0.5 truncate text-xs text-slate-500">
                          {thread.last_text ?? '—'}
                        </div>
                      </div>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>

        {active ? (
          <ThreadView key={active.chat_id} thread={active} onSent={threads.reload} />
        ) : (
          <div className="flex min-h-[40vh] items-center justify-center rounded-xl border border-ink-800 bg-ink-900/40">
            <Empty>Выберите чат слева</Empty>
          </div>
        )}
      </div>
    </Shell>
  );
}

function ThreadView({ thread, onSent }: { thread: Thread; onSent: () => void }) {
  const messages = useApi<Message[]>(`/conversations/${thread.chat_id}/messages`, 5_000);
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [togglingAi, setTogglingAi] = useState(false);
  const bottomRef = useRef<HTMLDivElement | null>(null);

  const toggleAiChat = async () => {
    if (togglingAi) return;
    setTogglingAi(true);
    setError(null);
    try {
      await api.post(`/conversations/${thread.chat_id}/ai-chat`, { enabled: !thread.ai_chat });
      onSent();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setTogglingAi(false);
    }
  };

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ block: 'end' });
  }, [messages.data]);

  const send = async () => {
    const body = text.trim();
    if (!body || sending) return;
    setSending(true);
    setError(null);
    try {
      await api.post<Message>(`/conversations/${thread.chat_id}/reply`, { text: body });
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
    <div className="flex max-h-[70vh] flex-col rounded-xl border border-ink-800 bg-ink-900/40">
      <div className="flex items-center justify-between gap-3 border-b border-ink-800 px-4 py-3">
        <div className="flex min-w-0 items-center gap-3">
          <ChatAvatar
            chatId={thread.chat_id}
            title={threadName(thread)}
            hasAvatar={thread.has_avatar}
            size={36}
          />
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-slate-100">{threadName(thread)}</div>
            <div className="text-xs text-slate-500">
              {thread.kind === 'dm' ? 'личный диалог' : 'группа'} · {thread.message_count} сообщений
            </div>
          </div>
        </div>
        <div className="flex items-center gap-2">
          {thread.kind === 'dm' && thread.lead_score != null && (
            <Badge tone="info">лид {thread.lead_score}</Badge>
          )}
          {thread.kind === 'group' && (
            <button
              onClick={() => void toggleAiChat()}
              disabled={togglingAi}
              title="Бот отвечает в этой группе по решению ИИ (не на всё подряд)"
              className={`rounded-lg px-3 py-1.5 text-xs font-medium transition disabled:opacity-50 ${
                thread.ai_chat
                  ? 'bg-emerald-500/15 text-emerald-300 border border-emerald-500/30'
                  : 'border border-ink-600 text-slate-400 hover:bg-ink-800'
              }`}
            >
              {togglingAi ? '…' : thread.ai_chat ? '● Общение ИИ включено' : '○ Включить общение ИИ'}
            </button>
          )}
        </div>
      </div>

      <div className="flex-1 space-y-2 overflow-y-auto px-4 py-4">
        <ErrorText>{messages.error}</ErrorText>
        {(messages.data?.length ?? 0) === 0 ? (
          <Empty>В этом чате пока нет сохранённых сообщений</Empty>
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
                  {thread.kind === 'group' && message.is_incoming && message.sender_display_name && (
                    <div className="mb-0.5 text-[11px] font-medium text-accent">
                      {message.sender_display_name}
                    </div>
                  )}
                  <div className="whitespace-pre-wrap break-words">{message.text ?? '—'}</div>
                  <div className={`mt-1 text-[10px] ${mine ? 'text-white/60' : 'text-slate-500'}`}>
                    {new Date(message.date).toLocaleString('ru')}
                    {mine && message.is_bot_reply ? ' · ии' : ''}
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

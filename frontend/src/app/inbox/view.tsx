'use client';

import { Bot, FlaskConical, Infinity as InfinityIcon, Repeat, Timer } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';

import { Badge, Button, ChatAvatar, Empty, ErrorText, PageHeader, RowButton, Segmented, Textarea, Tile, ToggleChip } from '@/components/ui';
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
export function InboxView() {
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
    <>
      <PageHeader
        title="Общение"
        subtitle="Личные диалоги и группы — раздельно. Переписка и ручной ответ от имени аккаунта"
      />

      <div className="mb-4">
        <Segmented<Kind>
          value={kind}
          onChange={setKind}
          options={[
            ['dm', 'Личка'],
            ['group', 'Группы'],
          ]}
        />
      </div>

      <ErrorText>{threads.error}</ErrorText>

      <div className="grid gap-4 lg:grid-cols-[340px_1fr]">
        <Tile className="max-h-[70vh] overflow-y-auto">
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
                    <RowButton selected={on} onClick={() => setSelected(thread.chat_id)} className="px-3 py-3">
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
                    </RowButton>
                  </li>
                );
              })}
            </ul>
          )}
        </Tile>

        {active ? (
          <ThreadView key={active.chat_id} thread={active} onSent={threads.reload} />
        ) : (
          <Tile className="flex min-h-[40vh] items-center justify-center">
            <Empty>Выберите чат слева</Empty>
          </Tile>
        )}
      </div>
    </>
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

  const toggleCooldownExempt = async () => {
    try {
      await api.post(`/conversations/${thread.chat_id}/cooldown-exempt`, {
        enabled: !thread.cooldown_exempt,
      });
      onSent();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  };

  const toggleTestMode = async () => {
    try {
      await api.post(`/conversations/${thread.chat_id}/test-mode`, { enabled: !thread.test_mode });
      onSent();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  };

  const avoidRepeat = thread.reply_settings?.avoid_repeat_topics ?? true;
  const toggleAvoidRepeat = async () => {
    try {
      await api.post(`/conversations/${thread.chat_id}/reply-settings`, {
        avoid_repeat_topics: !avoidRepeat,
        repeat_context_depth: thread.reply_settings?.repeat_context_depth ?? 5,
      });
      onSent();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
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
    <Tile className="flex max-h-[70vh] flex-col">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-ink-800 px-4 py-3">
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
        <div className="flex flex-wrap items-center gap-2">
          {thread.kind === 'dm' && thread.lead_score != null && (
            <Badge tone="info">лид {thread.lead_score}</Badge>
          )}
          <ToggleChip
            on={thread.test_mode}
            tone="test"
            onClick={() => void toggleTestMode()}
            icon={<FlaskConical size={14} />}
            title="Тест-чат: отвечаем всегда, без one_shot / лимита / рабочих часов"
          >
            {thread.test_mode ? 'Тест-чат' : 'Тест'}
          </ToggleChip>
          <ToggleChip
            on={avoidRepeat}
            tone="info"
            onClick={() => void toggleAvoidRepeat()}
            icon={<Repeat size={14} />}
            title="Не повторять смысл недавних ответов ИИ в этом чате"
          >
            {avoidRepeat ? 'Не повторяться' : 'Повторы ок'}
          </ToggleChip>
          {thread.kind === 'group' && (
            <ToggleChip
              on={thread.ai_chat}
              tone="ok"
              onClick={() => void toggleAiChat()}
              disabled={togglingAi}
              icon={<Bot size={14} />}
              title="Бот отвечает в этой группе по решению ИИ (не на всё подряд)"
            >
              {togglingAi ? '…' : thread.ai_chat ? 'Общение ИИ включено' : 'Включить общение ИИ'}
            </ToggleChip>
          )}
          {thread.kind === 'group' && (
            <ToggleChip
              on={thread.cooldown_exempt}
              tone="warn"
              onClick={() => void toggleCooldownExempt()}
              icon={thread.cooldown_exempt ? <InfinityIcon size={14} /> : <Timer size={14} />}
              title="Без анти-бан лимита: в этот чат можно отвечать без задержки «раз в N минут»"
            >
              {thread.cooldown_exempt ? 'Без лимита' : 'Лимит вкл'}
            </ToggleChip>
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
                    <div className="mb-0.5 text-xs font-medium text-accent">
                      {message.sender_display_name}
                    </div>
                  )}
                  <div className="whitespace-pre-wrap break-words">{message.text ?? '—'}</div>
                  <div className={`mt-1 text-xs tabular-nums ${mine ? 'text-white/70' : 'text-slate-500'}`}>
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
          <Textarea
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
            className="resize-none"
          />
          <Button onClick={() => void send()} disabled={sending || text.trim().length === 0}>
            {sending ? '…' : 'Отправить'}
          </Button>
        </div>
      </div>
    </Tile>
  );
}

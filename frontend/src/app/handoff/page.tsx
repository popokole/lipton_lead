'use client';

import { useEffect, useState } from 'react';

import { Shell } from '@/components/shell';
import { Button, Card, ChatAvatar, Empty, ErrorText, PageHeader } from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi, useRealtime } from '@/lib/hooks';
import type { Escalation } from '@/lib/types';

export default function HandoffPage() {
  const escalations = useApi<Escalation[]>('/handoff', 20_000);
  const { events } = useRealtime(10);
  const [resolving, setResolving] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Новая эскалация или решение с другого места — переспрашиваем список,
  // а не пытаемся аккуратно смёржить payload события в состояние.
  useEffect(() => {
    if (events.some((event) => event.type === 'HUMAN_HANDOFF')) {
      void escalations.reload();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [events]);

  async function resolve(conversationId: string) {
    setResolving(conversationId);
    setError(null);
    try {
      await api.post(`/handoff/${conversationId}/resolve`);
      await escalations.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setResolving(null);
    }
  }

  const items = escalations.data ?? [];

  return (
    <Shell>
      <PageHeader
        title="Требует внимания"
        subtitle="Диалоги, переданные оператору: ИИ попросил помощи, отказался отвечать или не прошёл проверку"
      />
      <ErrorText>{error ?? escalations.error}</ErrorText>

      {items.length === 0 ? (
        <Card>
          <Empty>
            {escalations.loading ? 'Загрузка…' : 'Эскалаций нет — всё под контролем ИИ'}
          </Empty>
        </Card>
      ) : (
        <div className="space-y-3">
          {items.map((item) => (
            <Card key={item.conversation_id}>
              <div className="flex items-start justify-between gap-4">
                <div className="flex min-w-0 items-start gap-3">
                  <ChatAvatar
                    chatId={item.chat_id}
                    title={item.chat_title}
                    hasAvatar={item.chat_has_avatar}
                    size={40}
                  />
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <h3 className="truncate text-sm font-medium text-slate-100">
                        {item.chat_title ??
                          (item.chat_username ? `@${item.chat_username}` : item.peer_tg_id)}
                      </h3>
                      <span className="text-xs text-slate-500">
                        {item.account_label ?? item.account_id}
                      </span>
                    </div>
                    <p className="mt-1 text-sm text-amber-300">{item.reason ?? 'Нужен оператор'}</p>
                    {item.suggested_reply && (
                      <blockquote className="mt-2 max-w-xl rounded-lg border border-ink-700 bg-ink-950 px-3 py-2 text-xs text-slate-400">
                        {item.suggested_reply}
                      </blockquote>
                    )}
                    {item.summary && (
                      <p className="mt-2 max-w-xl text-xs text-slate-500">{item.summary}</p>
                    )}
                  </div>
                </div>

                <div className="flex shrink-0 flex-col items-end gap-2">
                  <span className="text-xs text-slate-500">
                    {new Date(item.created_at).toLocaleString('ru')}
                  </span>
                  <div className="flex gap-2">
                    {item.chat_username && (
                      <a
                        href={`https://t.me/${item.chat_username}`}
                        target="_blank"
                        rel="noreferrer"
                        className="inline-flex min-h-9 items-center rounded-lg border border-ink-600 px-3 py-2 text-sm text-slate-300 hover:bg-ink-800"
                      >
                        Открыть в Telegram
                      </a>
                    )}
                    <Button
                      variant="primary"
                      disabled={resolving === item.conversation_id}
                      onClick={() => resolve(item.conversation_id)}
                    >
                      {resolving === item.conversation_id ? '…' : 'Отметить решённым'}
                    </Button>
                  </div>
                </div>
              </div>
            </Card>
          ))}
        </div>
      )}
    </Shell>
  );
}

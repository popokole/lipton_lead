'use client';

import { useEffect, useState } from 'react';

import { Shell } from '@/components/shell';
import { Badge, Button, Card, Empty, ErrorText, PageHeader, inputClass } from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi } from '@/lib/hooks';

interface Review {
  id: string;
  incoming_text: string | null;
  reply_text: string;
  dm_text: string | null;
  confidence: number | null;
  sender: string | null;
  created_at: string;
}

export default function ReviewsPage() {
  const reviews = useApi<Review[]>('/reviews', 6_000);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Черновики текста для правки: заполняем из dm_text (или reply_text).
  useEffect(() => {
    if (!reviews.data) return;
    setDrafts((prev) => {
      const next = { ...prev };
      for (const r of reviews.data!) {
        if (next[r.id] === undefined) next[r.id] = r.dm_text ?? r.reply_text;
      }
      return next;
    });
  }, [reviews.data]);

  async function approve(r: Review) {
    setBusy(r.id);
    setError(null);
    try {
      const text = (drafts[r.id] ?? '').trim();
      // Правим то поле, что уходит собеседнику: dm_text, если есть, иначе reply_text.
      const body = r.dm_text != null ? { dm_text: text } : { reply_text: text };
      await api.post(`/reviews/${r.id}/approve`, body);
      await reviews.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  async function ignore(r: Review) {
    setBusy(r.id);
    setError(null);
    try {
      await api.post(`/reviews/${r.id}/ignore`);
      await reviews.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Shell>
      <PageHeader
        title="На подтверждение"
        subtitle="Сомнительные ответы ИИ: поправьте текст и отправьте, либо пропустите"
      />
      <ErrorText>{error ?? reviews.error}</ErrorText>

      {(reviews.data?.length ?? 0) === 0 ? (
        <Card>
          <Empty>Нет ответов на подтверждение</Empty>
        </Card>
      ) : (
        <div className="space-y-4">
          {reviews.data?.map((r) => (
            <Card key={r.id}>
              <div className="mb-2 flex items-center justify-between gap-2">
                <span className="text-sm font-medium text-slate-200">{r.sender ?? 'собеседник'}</span>
                {r.confidence != null && (
                  <Badge tone="warn">уверенность {(r.confidence * 100).toFixed(0)}%</Badge>
                )}
              </div>
              {r.incoming_text && (
                <div className="mb-3 rounded-lg border border-ink-800 bg-ink-900/40 px-3 py-2 text-xs text-slate-400">
                  <span className="text-slate-500">сообщение: </span>
                  {r.incoming_text}
                </div>
              )}
              <textarea
                className={`${inputClass} h-24 w-full resize-y`}
                value={drafts[r.id] ?? ''}
                onChange={(e) => setDrafts((p) => ({ ...p, [r.id]: e.target.value }))}
              />
              <div className="mt-3 flex gap-2">
                <Button onClick={() => approve(r)} disabled={busy === r.id || !(drafts[r.id] ?? '').trim()}>
                  {busy === r.id ? '…' : 'Отправить'}
                </Button>
                <Button variant="ghost" onClick={() => ignore(r)} disabled={busy === r.id}>
                  Пропустить
                </Button>
              </div>
            </Card>
          ))}
        </div>
      )}
    </Shell>
  );
}

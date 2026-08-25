'use client';

import { useState } from 'react';

import { Shell } from '@/components/shell';
import { Button, Card, Empty, ErrorText, Field, PageHeader, Table, inputClass } from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi } from '@/lib/hooks';

interface StopEntry {
  id: string;
  tg_user_id: number | null;
  username: string | null;
  note: string | null;
  created_at: string;
}

export default function StoplistPage() {
  const entries = useApi<StopEntry[]>('/stoplist', 15_000);
  const [ident, setIdent] = useState('');
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function add() {
    const value = ident.trim();
    if (!value) return;
    setBusy(true);
    setError(null);
    try {
      // Цифры — это tg-id, иначе @username.
      const isId = /^\d+$/.test(value.replace('-', ''));
      await api.post('/stoplist', {
        tg_user_id: isId ? Number(value) : null,
        username: isId ? null : value.replace(/^@/, ''),
        note: note.trim() || null,
      });
      setIdent('');
      setNote('');
      await entries.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(id: string) {
    try {
      await api.delete(`/stoplist/${id}`);
      await entries.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <Shell>
      <PageHeader
        title="Стоп-лист"
        subtitle="Кому никогда не отвечаем: админы, конкуренты, боты, спамеры. Сообщения сохраняются, но ответы не идут"
      />
      <ErrorText>{error ?? entries.error}</ErrorText>

      <Card title="Добавить" className="mb-6">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="tg-id или @username" hint="Цифры — это id, иначе ник">
            <input
              className={inputClass}
              value={ident}
              onChange={(e) => setIdent(e.target.value)}
              placeholder="@spammer или 123456789"
            />
          </Field>
          <Field label="Заметка" hint="Необязательно">
            <input
              className={inputClass}
              value={note}
              onChange={(e) => setNote(e.target.value)}
              placeholder="конкурент / бот / спам"
            />
          </Field>
          <Button onClick={add} disabled={busy || !ident.trim()}>
            {busy ? 'Добавляем…' : 'В стоп-лист'}
          </Button>
        </div>
        <p className="mt-3 text-xs text-slate-500">
          Изменения применяются автоматически в течение ~минуты (воркер обновляет список в памяти).
        </p>
      </Card>

      <Card title="Список">
        {(entries.data?.length ?? 0) === 0 ? (
          <Empty>Стоп-лист пуст</Empty>
        ) : (
          <Table head={['Кто', 'Заметка', 'Добавлен', '']}>
            {entries.data?.map((entry) => (
              <tr key={entry.id} className="border-b border-ink-800/70 last:border-0">
                <td className="py-3 pr-4 text-slate-200">
                  {entry.username ? `@${entry.username}` : entry.tg_user_id}
                </td>
                <td className="py-3 pr-4 text-sm text-slate-400">{entry.note ?? '—'}</td>
                <td className="py-3 pr-4 text-xs text-slate-500">
                  {new Date(entry.created_at).toLocaleString('ru')}
                </td>
                <td className="py-3">
                  <Button variant="danger" onClick={() => remove(entry.id)}>
                    Убрать
                  </Button>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </Shell>
  );
}

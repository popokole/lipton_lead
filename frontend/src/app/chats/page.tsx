'use client';

import { useState } from 'react';

import { Shell } from '@/components/shell';
import {
  Badge,
  Button,
  Card,
  Empty,
  ErrorText,
  Field,
  PageHeader,
  Table,
  inputClass,
} from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi } from '@/lib/hooks';
import type { Account, Chat } from '@/lib/types';

export default function ChatsPage() {
  const chats = useApi<Chat[]>('/chats', 10_000);
  const accounts = useApi<Account[]>('/accounts');
  const [error, setError] = useState<string | null>(null);
  const [accountId, setAccountId] = useState('');
  const [chatId, setChatId] = useState('');
  const [title, setTitle] = useState('');
  const [adding, setAdding] = useState(false);

  const nameOf = (id: string) =>
    accounts.data?.find((account) => account.id === id)?.label ?? id.slice(0, 8);

  async function toggle(chat: Chat) {
    setError(null);
    try {
      await api.patch(`/chats/${chat.id}`, { monitored: !chat.monitored });
      await chats.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function add() {
    setAdding(true);
    setError(null);
    try {
      await api.post('/chats', {
        account_id: accountId || accounts.data?.[0]?.id,
        tg_chat_id: Number(chatId),
        type: Number(chatId) < 0 ? 'SUPERGROUP' : 'PRIVATE',
        title: title.trim() || null,
        monitored: true,
      });
      setChatId('');
      setTitle('');
      await chats.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setAdding(false);
    }
  }

  async function remove(chat: Chat) {
    setError(null);
    try {
      await api.delete(`/chats/${chat.id}`);
      await chats.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <Shell>
      <PageHeader
        title="Чаты"
        subtitle="Обрабатываются только чаты с включённым наблюдением"
      />
      <ErrorText>{error ?? chats.error}</ErrorText>

      <Card title="Добавить чат вручную" className="mb-6">
        <p className="mb-4 text-sm text-slate-400">
          Нужно, когда список диалогов недоступен. ID чата видно в журнале входящих
          сообщений; у групп он отрицательный, например{' '}
          <span className="font-mono text-slate-300">-1001192249190</span>.
        </p>
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-56">
            <Field label="Аккаунт">
              <select
                className={inputClass}
                value={accountId}
                onChange={(event) => setAccountId(event.target.value)}
              >
                {accounts.data?.map((account) => (
                  <option key={account.id} value={account.id}>
                    {account.label}
                  </option>
                ))}
              </select>
            </Field>
          </div>
          <div className="w-52">
            <Field label="ID чата">
              <input
                className={inputClass}
                value={chatId}
                onChange={(event) => setChatId(event.target.value.replace(/[^0-9-]/g, ''))}
                placeholder="-1001192249190"
              />
            </Field>
          </div>
          <div className="w-56">
            <Field label="Название" hint="Необязательно, для панели">
              <input
                className={inputClass}
                value={title}
                onChange={(event) => setTitle(event.target.value)}
              />
            </Field>
          </div>
          <Button onClick={add} disabled={adding || !chatId}>
            {adding ? 'Добавляем…' : 'Следить'}
          </Button>
        </div>
      </Card>

      <Card>
        {(chats.data?.length ?? 0) === 0 ? (
          <Empty>
            Чатов нет. Добавьте их на странице «Аккаунты» → «Диалоги» после авторизации аккаунта.
          </Empty>
        ) : (
          <Table head={['Чат', 'Аккаунт', 'Тип', 'Наблюдение', 'Последнее сообщение', '']}>
            {chats.data?.map((chat) => (
              <tr key={chat.id} className="border-b border-ink-800/70 last:border-0">
                <td className="py-3 pr-4">
                  <div className="text-slate-200">{chat.title ?? chat.tg_chat_id}</div>
                  <div className="text-xs text-slate-600">
                    {chat.username ? `@${chat.username} · ` : ''}
                    {chat.tg_chat_id}
                  </div>
                </td>
                <td className="py-3 pr-4 text-slate-400">{nameOf(chat.account_id)}</td>
                <td className="py-3 pr-4 text-slate-500">{chat.type}</td>
                <td className="py-3 pr-4">
                  {chat.monitored ? <Badge tone="ok">включено</Badge> : <Badge>выключено</Badge>}
                </td>
                <td className="py-3 pr-4 text-xs text-slate-500">
                  {chat.last_message_at ? new Date(chat.last_message_at).toLocaleString('ru') : '—'}
                </td>
                <td className="py-3">
                  <div className="flex gap-2">
                    <Button variant="ghost" onClick={() => toggle(chat)}>
                      {chat.monitored ? 'Не следить' : 'Следить'}
                    </Button>
                    <Button variant="danger" onClick={() => remove(chat)}>
                      Убрать
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </Shell>
  );
}

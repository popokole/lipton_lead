'use client';

import { Filter, Search, Target } from 'lucide-react';
import { useMemo, useState } from 'react';

import { Shell } from '@/components/shell';
import {
  Badge,
  Button,
  Card,
  ChatAvatar,
  Empty,
  ErrorText,
  PageHeader,
  Stat,
  inputClass,
} from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi } from '@/lib/hooks';
import type { ChatNode, ChatTreeAccount } from '@/lib/types';

type TypeFilter = 'all' | 'dm' | 'group' | 'channel';
type SortKey = 'leads' | 'activity' | 'replies';

function kindOf(type: string): Exclude<TypeFilter, 'all'> {
  if (type === 'PRIVATE') return 'dm';
  if (type === 'CHANNEL') return 'channel';
  return 'group';
}

const KIND_LABEL: Record<Exclude<TypeFilter, 'all'>, string> = {
  dm: 'личка',
  group: 'группа',
  channel: 'канал',
};

function chatName(chat: ChatNode): string {
  return chat.title ?? (chat.username ? `@${chat.username}` : String(chat.tg_chat_id));
}

export default function TreePage() {
  const tree = useApi<ChatTreeAccount[]>('/chats/tree', 12_000);
  const [selected, setSelected] = useState<{ account: string; chat: ChatNode } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Фильтры/сортировка — то, чего не хватало радиальному графу: найти, где лиды.
  const [query, setQuery] = useState('');
  const [type, setType] = useState<TypeFilter>('all');
  const [leadsOnly, setLeadsOnly] = useState(false);
  const [monitoredOnly, setMonitoredOnly] = useState(false);
  const [sort, setSort] = useState<SortKey>('leads');

  const totals = (tree.data ?? []).reduce(
    (acc, a) => ({
      messages: acc.messages + a.messages_total,
      leads: acc.leads + a.leads_count,
      chats: acc.chats + a.chats.length,
    }),
    { messages: 0, leads: 0, chats: 0 },
  );

  async function toggleMonitor(chat: ChatNode) {
    setBusy(true);
    setError(null);
    try {
      await api.patch(`/chats/${chat.id}`, { monitored: !chat.monitored });
      await tree.reload();
      setSelected((s) => (s ? { ...s, chat: { ...s.chat, monitored: !chat.monitored } } : s));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <PageHeader
        title="Активность чатов"
        subtitle="Все чаты аккаунта — по лидам и активности. Ищите, фильтруйте, включайте слежку"
      />
      <ErrorText>{error ?? tree.error}</ErrorText>

      <div className="mb-6 grid grid-cols-3 gap-4">
        <Stat label="Чатов" value={totals.chats} />
        <Stat label="Сообщений" value={totals.messages} />
        <Stat label="Лидов" value={totals.leads} />
      </div>

      <Card className="mb-4">
        <div className="flex flex-wrap items-center gap-3">
          <div className="relative min-w-[200px] flex-1">
            <Search
              size={15}
              className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-500"
            />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Поиск по названию или @нику"
              className={`${inputClass} pl-9`}
            />
          </div>
          <Seg
            value={type}
            onChange={(v) => setType(v as TypeFilter)}
            options={[
              ['all', 'Все'],
              ['dm', 'Личка'],
              ['group', 'Группы'],
              ['channel', 'Каналы'],
            ]}
          />
          <button
            onClick={() => setLeadsOnly((v) => !v)}
            className={toggleClass(leadsOnly, 'ok')}
            title="Показать только чаты, где есть лиды"
          >
            <Target size={13} /> с лидами
          </button>
          <button
            onClick={() => setMonitoredOnly((v) => !v)}
            className={toggleClass(monitoredOnly, 'info')}
            title="Показать только чаты со включённой слежкой"
          >
            <Filter size={13} /> отслеживаемые
          </button>
          <div className="ml-auto flex items-center gap-2 text-xs text-slate-500">
            <span>сортировка</span>
            <Seg
              value={sort}
              onChange={(v) => setSort(v as SortKey)}
              options={[
                ['leads', 'Лиды'],
                ['activity', 'Активность'],
                ['replies', 'Ответы'],
              ]}
            />
          </div>
        </div>
      </Card>

      {(tree.data?.length ?? 0) === 0 ? (
        <Empty>Пока нет данных. Аккаунт наполнит список, как только начнёт читать чаты.</Empty>
      ) : (
        <div className="space-y-6">
          {tree.data?.map((account) => (
            <AccountChats
              key={account.account_id}
              account={account}
              filters={{ query, type, leadsOnly, monitoredOnly, sort }}
              onSelect={(chat) => setSelected({ account: account.label, chat })}
              onToggleMonitor={toggleMonitor}
              busy={busy}
            />
          ))}
        </div>
      )}

      {selected && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
          onClick={() => setSelected(null)}
        >
          <div
            className="w-full max-w-md rounded-2xl border border-ink-700 bg-ink-900 p-5"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="mb-4 flex items-center gap-3">
              <ChatAvatar
                chatId={selected.chat.id}
                title={selected.chat.title}
                hasAvatar={selected.chat.has_avatar}
                size={44}
              />
              <div className="min-w-0">
                <h3 className="truncate text-sm font-medium text-slate-100">
                  {chatName(selected.chat)}
                </h3>
                <p className="text-xs text-slate-500">
                  {KIND_LABEL[kindOf(selected.chat.type)]} · {selected.account}
                </p>
              </div>
            </div>

            <div className="grid grid-cols-2 gap-3">
              <Stat label="Сообщений" value={selected.chat.messages_total} />
              <Stat label="Участников" value={selected.chat.active_users} />
              <Stat label="Наших ответов" value={selected.chat.replies_count} />
              <Stat label="Лидов" value={selected.chat.leads_count} />
            </div>

            <div className="mt-4 flex items-center justify-between rounded-lg border border-ink-700 px-3 py-2">
              <span className="text-sm text-slate-300">
                Слежка {selected.chat.monitored ? 'включена' : 'выключена'}
              </span>
              <Button
                variant={selected.chat.monitored ? 'danger' : 'primary'}
                disabled={busy}
                onClick={() => toggleMonitor(selected.chat)}
              >
                {busy ? '…' : selected.chat.monitored ? 'Выключить' : 'Включить'}
              </Button>
            </div>
            <p className="mt-2 text-xs text-slate-500">
              {selected.chat.monitored
                ? 'Правила работают: аккаунт может отвечать в этом чате.'
                : 'Сообщения читаются, но правила не запускаются — ответов не будет.'}
            </p>
            <div className="mt-4 flex justify-end">
              <a href="/inbox">
                <Button variant="ghost">Открыть в «Общении»</Button>
              </a>
            </div>
          </div>
        </div>
      )}
    </Shell>
  );
}

interface Filters {
  query: string;
  type: TypeFilter;
  leadsOnly: boolean;
  monitoredOnly: boolean;
  sort: SortKey;
}

function AccountChats({
  account,
  filters,
  onSelect,
  onToggleMonitor,
  busy,
}: {
  account: ChatTreeAccount;
  filters: Filters;
  onSelect: (c: ChatNode) => void;
  onToggleMonitor: (c: ChatNode) => void;
  busy: boolean;
}) {
  const rows = useMemo(() => {
    const q = filters.query.trim().toLowerCase();
    let list = account.chats.filter((c) => {
      if (filters.type !== 'all' && kindOf(c.type) !== filters.type) return false;
      if (filters.leadsOnly && c.leads_count === 0) return false;
      if (filters.monitoredOnly && !c.monitored) return false;
      if (q) {
        const hay = `${c.title ?? ''} ${c.username ?? ''} ${c.tg_chat_id}`.toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
    const key =
      filters.sort === 'leads'
        ? (c: ChatNode) => c.leads_count * 1e6 + c.messages_total
        : filters.sort === 'replies'
          ? (c: ChatNode) => c.replies_count
          : (c: ChatNode) => c.messages_total;
    list = [...list].sort((a, b) => key(b) - key(a));
    return list;
  }, [account.chats, filters]);

  return (
    <Card
      title={
        <span className="flex items-center gap-2">
          {account.label}
          <Badge tone="ok">{account.leads_count} лидов</Badge>
          <span className="text-xs font-normal text-slate-500">{rows.length} чатов в списке</span>
        </span>
      }
    >
      {rows.length === 0 ? (
        <Empty>Под фильтр ничего не подошло</Empty>
      ) : (
        <ul className="divide-y divide-ink-800/70">
          {rows.map((chat) => (
            <li key={chat.id}>
              <div
                className={`flex items-center gap-3 py-2.5 ${
                  chat.leads_count > 0 ? 'pl-3 -ml-3 border-l-2 border-emerald-500/60' : ''
                }`}
              >
                <button
                  onClick={() => onSelect(chat)}
                  className="flex min-w-0 flex-1 items-center gap-3 text-left"
                >
                  <ChatAvatar
                    chatId={chat.id}
                    title={chat.title}
                    hasAvatar={chat.has_avatar}
                    size={34}
                  />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-center gap-2">
                      <span
                        className={`truncate text-sm ${chat.monitored ? 'text-slate-100' : 'text-slate-400'}`}
                      >
                        {chatName(chat)}
                      </span>
                      <span className="shrink-0 text-[10px] uppercase tracking-wide text-slate-600">
                        {KIND_LABEL[kindOf(chat.type)]}
                      </span>
                    </div>
                    <div className="mt-0.5 flex flex-wrap gap-x-3 gap-y-0.5 text-xs text-slate-500">
                      <span>{chat.messages_total} сообщ.</span>
                      <span>{chat.replies_count} ответов</span>
                      <span>{chat.active_users} чел.</span>
                      {chat.leads_count > 0 && (
                        <span className="font-medium text-emerald-300">{chat.leads_count} лид</span>
                      )}
                    </div>
                  </div>
                </button>
                <button
                  onClick={() => onToggleMonitor(chat)}
                  disabled={busy}
                  title={
                    chat.monitored
                      ? 'Слежка включена — правила работают. Нажмите, чтобы выключить'
                      : 'Слежка выключена. Нажмите, чтобы включить'
                  }
                  className={`shrink-0 rounded-lg border px-2.5 py-1 text-xs font-medium transition disabled:opacity-40 ${
                    chat.monitored
                      ? 'border-sky-500/30 bg-sky-500/15 text-sky-300'
                      : 'border-ink-600 text-slate-400 hover:bg-ink-800'
                  }`}
                >
                  {chat.monitored ? '● слежка' : '○ слежка'}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

function Seg({
  value,
  onChange,
  options,
}: {
  value: string;
  onChange: (v: string) => void;
  options: [string, string][];
}) {
  return (
    <div className="inline-flex rounded-lg border border-ink-700 bg-ink-950 p-0.5 text-xs">
      {options.map(([v, label]) => (
        <button
          key={v}
          onClick={() => onChange(v)}
          className={`rounded-md px-2.5 py-1 transition ${
            value === v ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:text-slate-200'
          }`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

function toggleClass(active: boolean, tone: 'ok' | 'info'): string {
  const on =
    tone === 'ok'
      ? 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300'
      : 'border-sky-500/30 bg-sky-500/15 text-sky-300';
  return `inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-xs font-medium transition ${
    active ? on : 'border-ink-600 text-slate-400 hover:bg-ink-800'
  }`;
}

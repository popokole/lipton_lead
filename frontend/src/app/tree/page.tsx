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

      {tree.data?.map((account) => (
        <SunburstCard
          key={`sb-${account.account_id}`}
          account={account}
          onSelect={(chat) => setSelected({ account: account.label, chat })}
        />
      ))}

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

const KIND_COLOR: Record<Exclude<TypeFilter, 'all'>, string> = {
  group: '#6366f1',
  dm: '#38bdf8',
  channel: '#64748b',
};
const KIND_TITLE: Record<Exclude<TypeFilter, 'all'>, string> = {
  group: 'Группы',
  dm: 'Личка',
  channel: 'Каналы',
};

function polar(cx: number, cy: number, r: number, a: number): [number, number] {
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}

function annular(
  cx: number,
  cy: number,
  rI: number,
  rO: number,
  a0: number,
  a1: number,
): string {
  const large = a1 - a0 > Math.PI ? 1 : 0;
  const [x0, y0] = polar(cx, cy, rO, a0);
  const [x1, y1] = polar(cx, cy, rO, a1);
  const [x2, y2] = polar(cx, cy, rI, a1);
  const [x3, y3] = polar(cx, cy, rI, a0);
  return `M${x0} ${y0} A${rO} ${rO} 0 ${large} 1 ${x1} ${y1} L${x2} ${y2} A${rI} ${rI} 0 ${large} 0 ${x3} ${y3} Z`;
}

/** Радиальная инфографика «дерево»: центр — аккаунт, кольцо — категории,
 *  внешние дольки — чаты (зелёные = есть лиды, сгруппированы по категории). */
function SunburstCard({
  account,
  onSelect,
}: {
  account: ChatTreeAccount;
  onSelect: (c: ChatNode) => void;
}) {
  const groups = useMemo(() => {
    const by: Record<Exclude<TypeFilter, 'all'>, ChatNode[]> = { group: [], dm: [], channel: [] };
    for (const c of account.chats) by[kindOf(c.type)].push(c);
    const order: Exclude<TypeFilter, 'all'>[] = ['group', 'dm', 'channel'];
    return order
      .map((kind) => ({
        kind,
        chats: [...by[kind]].sort(
          (a, b) => b.leads_count - a.leads_count || b.messages_total - a.messages_total,
        ),
      }))
      .filter((g) => g.chats.length > 0);
  }, [account.chats]);

  const size = 560;
  const cx = size / 2;
  const cy = size / 2;
  const ri0 = 62;
  const ri1 = 132;
  const ro0 = 138;
  const ro1 = 250;
  const gap = 0.02;

  const weight = (c: ChatNode) => Math.max(c.messages_total, 1);
  const grand = groups.reduce((s, g) => s + g.chats.reduce((ss, c) => ss + weight(c), 0), 0) || 1;

  const catSegs: { kind: Exclude<TypeFilter, 'all'>; a0: number; a1: number }[] = [];
  const chatSegs: { chat: ChatNode; kind: Exclude<TypeFilter, 'all'>; a0: number; a1: number }[] =
    [];
  const catLabels: {
    kind: Exclude<TypeFilter, 'all'>;
    mid: number;
    count: number;
    leads: number;
  }[] = [];

  let a = -Math.PI / 2;
  for (const g of groups) {
    const gw = g.chats.reduce((s, c) => s + weight(c), 0);
    const span = (gw / grand) * (Math.PI * 2 - gap * groups.length);
    const a0 = a;
    const a1 = a + span;
    catSegs.push({ kind: g.kind, a0, a1 });
    catLabels.push({
      kind: g.kind,
      mid: (a0 + a1) / 2,
      count: g.chats.length,
      leads: g.chats.reduce((s, c) => s + c.leads_count, 0),
    });
    let ca = a0;
    for (const c of g.chats) {
      const cspan = (weight(c) / gw) * span;
      chatSegs.push({ chat: c, kind: g.kind, a0: ca, a1: ca + cspan });
      ca += cspan;
    }
    a = a1 + gap;
  }

  return (
    <Card title={`${account.label} · инфографика`} className="mb-4">
      <div className="overflow-auto">
        <svg
          viewBox={`0 0 ${size} ${size}`}
          className="mx-auto block h-auto w-full"
          style={{ maxWidth: 520 }}
        >
          {/* внешнее кольцо: чаты */}
          {chatSegs.map(({ chat, kind, a0, a1 }) => {
            const hasLead = chat.leads_count > 0;
            const fill = hasLead ? '#34d399' : KIND_COLOR[kind];
            const op = hasLead ? 0.9 : chat.monitored ? 0.4 : 0.16;
            return (
              <path
                key={chat.id}
                d={annular(cx, cy, ro0, ro1, a0, a1)}
                fill={fill}
                fillOpacity={op}
                stroke="#0b1220"
                strokeWidth={0.5}
                className="cursor-pointer transition-opacity hover:fill-opacity-100"
                onClick={() => onSelect(chat)}
              >
                <title>
                  {chatName(chat)} · {chat.messages_total} сообщ.
                  {hasLead ? ` · ${chat.leads_count} лид` : ''}
                </title>
              </path>
            );
          })}

          {/* внутреннее кольцо: категории */}
          {catSegs.map(({ kind, a0, a1 }) => (
            <path
              key={kind}
              d={annular(cx, cy, ri0, ri1, a0, a1)}
              fill={KIND_COLOR[kind]}
              fillOpacity={0.85}
              stroke="#0b1220"
              strokeWidth={1}
            />
          ))}
          {catLabels.map(({ kind, mid, count, leads }) => {
            const [lx, ly] = polar(cx, cy, (ri0 + ri1) / 2, mid);
            return (
              <g key={`l-${kind}`} pointerEvents="none">
                <text
                  x={lx}
                  y={ly - 4}
                  textAnchor="middle"
                  fontSize="13"
                  fontWeight="600"
                  className="fill-white"
                >
                  {KIND_TITLE[kind]}
                </text>
                <text x={lx} y={ly + 11} textAnchor="middle" fontSize="10" className="fill-white/80">
                  {count} · {leads} лид
                </text>
              </g>
            );
          })}

          {/* центр: аккаунт */}
          <circle cx={cx} cy={cy} r={54} fill="#0b1220" stroke="#4f8cff" strokeWidth="3" />
          <text
            x={cx}
            y={cy - 4}
            textAnchor="middle"
            fontSize="14"
            fontWeight="600"
            className="fill-slate-100"
          >
            {account.label.length > 12 ? account.label.slice(0, 11) + '…' : account.label}
          </text>
          <text x={cx} y={cy + 16} textAnchor="middle" fontSize="13" className="fill-emerald-300">
            {account.leads_count} лидов
          </text>
        </svg>
      </div>
      <div className="mt-3 flex flex-wrap justify-center gap-x-4 gap-y-1 text-xs text-slate-400">
        <LegendDot color="#34d399" label="есть лиды" />
        <LegendDot color={KIND_COLOR.dm} label="личка" />
        <LegendDot color={KIND_COLOR.group} label="группы" />
        <LegendDot color={KIND_COLOR.channel} label="каналы" />
        <span className="text-slate-600">размер дольки — активность · клик → детали</span>
      </div>
    </Card>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span className="inline-flex items-center gap-1.5">
      <span className="h-2.5 w-2.5 rounded-sm" style={{ backgroundColor: color }} />
      {label}
    </span>
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

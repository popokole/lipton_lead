'use client';

import { useMemo, useState } from 'react';

import { Shell } from '@/components/shell';
import { Button, Card, ChatAvatar, Empty, ErrorText, PageHeader, Stat } from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi } from '@/lib/hooks';
import type { ChatNode, ChatTreeAccount } from '@/lib/types';

export default function TreePage() {
  const tree = useApi<ChatTreeAccount[]>('/chats/tree', 12_000);
  const [selected, setSelected] = useState<{ account: string; chat: ChatNode } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

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
        title="Дерево чатов"
        subtitle="Аккаунт в центре, чаты вокруг. Размер — активность, зелёный ободок — лиды, тусклый — слежка выключена"
      />
      <ErrorText>{error ?? tree.error}</ErrorText>

      <div className="mb-6 grid gap-4 sm:grid-cols-3">
        <Stat label="Чатов" value={totals.chats} />
        <Stat label="Сообщений" value={totals.messages} />
        <Stat label="Лидов" value={totals.leads} />
      </div>

      {(tree.data?.length ?? 0) === 0 ? (
        <Empty>Пока нет данных. Аккаунт наполнит дерево, как только начнёт читать чаты.</Empty>
      ) : (
        <div className="space-y-6">
          {tree.data?.map((account) => (
            <Card key={account.account_id}>
              <RadialGraph
                account={account}
                onSelect={(chat) => setSelected({ account: account.label, chat })}
              />
            </Card>
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
                  {selected.chat.title ??
                    (selected.chat.username
                      ? `@${selected.chat.username}`
                      : selected.chat.tg_chat_id)}
                </h3>
                <p className="text-xs text-slate-500">
                  {selected.chat.type.toLowerCase()} · {selected.account}
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
          </div>
        </div>
      )}
    </Shell>
  );
}

function RadialGraph({
  account,
  onSelect,
}: {
  account: ChatTreeAccount;
  onSelect: (c: ChatNode) => void;
}) {
  const chats = useMemo(
    () => [...account.chats].sort((a, b) => b.messages_total - a.messages_total),
    [account.chats],
  );
  const maxMsg = Math.max(...chats.map((c) => c.messages_total), 1);

  // Два кольца при большом числе чатов: активные ближе к центру, остальные
  // дальше — так подписи не наслаиваются и остаётся воздух.
  const twoRings = chats.length > 14;
  const inner = twoRings ? chats.filter((_, i) => i % 2 === 0) : chats;
  const outer = twoRings ? chats.filter((_, i) => i % 2 === 1) : [];

  const size = 900;
  const cx = size / 2;
  const cy = size / 2;
  const rInner = twoRings ? 250 : 300;
  const rOuter = 380;

  const place = (list: ChatNode[], ringRadius: number, offset: number) =>
    list.map((chat, i) => {
      const angle = ((i + offset) / Math.max(list.length, 1)) * Math.PI * 2 - Math.PI / 2;
      return { chat, x: cx + Math.cos(angle) * ringRadius, y: cy + Math.sin(angle) * ringRadius };
    });

  const nodes = [...place(inner, rInner, 0), ...place(outer, rOuter, 0.5)];

  return (
    <div className="overflow-auto">
      <svg
        viewBox={`0 0 ${size} ${size}`}
        className="mx-auto block h-auto w-full"
        style={{ minWidth: 640, maxWidth: 860 }}
      >
        <defs>
          {nodes.map(({ chat }) => {
            const r = 18 + (chat.messages_total / maxMsg) * 20;
            return (
              <clipPath key={`clip-${chat.id}`} id={`clip-${chat.id}`}>
                <circle cx="0" cy="0" r={r} />
              </clipPath>
            );
          })}
        </defs>

        {nodes.map(({ chat, x, y }) => (
          <line
            key={`e-${chat.id}`}
            x1={cx}
            y1={cy}
            x2={x}
            y2={y}
            stroke={chat.leads_count > 0 ? '#34d39955' : '#33415544'}
            strokeWidth={chat.monitored ? 1.5 : 1}
          />
        ))}

        {nodes.map(({ chat, x, y }) => {
          const r = 18 + (chat.messages_total / maxMsg) * 20;
          const label =
            chat.title ?? (chat.username ? `@${chat.username}` : String(chat.tg_chat_id));
          const ring = chat.leads_count > 0 ? '#34d399' : chat.monitored ? '#38bdf8' : '#475569';
          return (
            <g key={chat.id} className="cursor-pointer" onClick={() => onSelect(chat)}>
              <circle cx={x} cy={y} r={r} fill="#0f172a" opacity={chat.monitored ? 1 : 0.45} />
              <g transform={`translate(${x},${y})`} clipPath={`url(#clip-${chat.id})`}>
                {chat.has_avatar && (
                  <image
                    href={`/api/chats/${chat.id}/avatar`}
                    x={-r}
                    y={-r}
                    width={r * 2}
                    height={r * 2}
                    preserveAspectRatio="xMidYMid slice"
                    opacity={chat.monitored ? 1 : 0.4}
                  />
                )}
              </g>
              {!chat.has_avatar && (
                <text
                  x={x}
                  y={y + 4}
                  textAnchor="middle"
                  fontSize={r * 0.7}
                  className="fill-slate-500"
                >
                  {(label[0] || '#').toUpperCase()}
                </text>
              )}
              <circle
                cx={x}
                cy={y}
                r={r}
                fill="none"
                stroke={ring}
                strokeWidth={chat.leads_count > 0 ? 3 : 1.5}
              />
              <circle
                cx={x + r * 0.7}
                cy={y - r * 0.7}
                r={9}
                fill="#0b1220"
                stroke={ring}
                strokeWidth="1"
              />
              <text
                x={x + r * 0.7}
                y={y - r * 0.7 + 3}
                textAnchor="middle"
                fontSize="8"
                className="fill-slate-200"
              >
                {chat.messages_total}
              </text>
              <text x={x} y={y + r + 13} textAnchor="middle" fontSize="10" className="fill-slate-400">
                {label.length > 14 ? label.slice(0, 13) + '…' : label}
              </text>
              {chat.leads_count > 0 && (
                <text
                  x={x}
                  y={y + r + 25}
                  textAnchor="middle"
                  fontSize="9"
                  className="fill-emerald-300"
                >
                  {chat.leads_count} лид
                </text>
              )}
            </g>
          );
        })}

        <circle cx={cx} cy={cy} r={52} fill="#0b1220" stroke="#4f8cff" strokeWidth="3" />
        <text
          x={cx}
          y={cy - 4}
          textAnchor="middle"
          fontSize="15"
          fontWeight="600"
          className="fill-slate-100"
        >
          {account.label.length > 12 ? account.label.slice(0, 11) + '…' : account.label}
        </text>
        <text x={cx} y={cy + 14} textAnchor="middle" fontSize="12" className="fill-emerald-300">
          {account.leads_count} лидов
        </text>
      </svg>
    </div>
  );
}

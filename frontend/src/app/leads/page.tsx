'use client';

import { Shell } from '@/components/shell';
import { Badge, Card, Empty, ErrorText, PageHeader, Table, statusTone } from '@/components/ui';
import { useApi } from '@/lib/hooks';
import type { Lead } from '@/lib/types';

export default function LeadsPage() {
  const leads = useApi<Lead[]>('/leads?limit=200', 15_000);

  return (
    <Shell>
      <PageHeader title="Лиды" subtitle="Собеседники, проявившие интерес" />
      <ErrorText>{leads.error}</ErrorText>

      <Card>
        {(leads.data?.length ?? 0) === 0 ? (
          <Empty>Лидов пока нет</Empty>
        ) : (
          <Table head={['Человек', 'Интерес', 'Балл', 'Статус', 'Первый раз', 'Последний раз']}>
            {leads.data?.map((lead) => (
              <tr key={lead.id} className="border-b border-ink-800/70 last:border-0">
                <td className="py-3 pr-4">
                  <div className="text-slate-200">{lead.display_name ?? lead.tg_user_id}</div>
                  {lead.username && <div className="text-xs text-slate-600">@{lead.username}</div>}
                </td>
                <td className="py-3 pr-4 text-xs text-slate-400">{lead.intent ?? '—'}</td>
                <td className="py-3 pr-4">
                  <div className="flex items-center gap-2">
                    <div className="h-1.5 w-20 rounded bg-ink-700">
                      <div
                        className="h-1.5 rounded bg-accent"
                        style={{ width: `${lead.score}%` }}
                      />
                    </div>
                    <span className="text-xs text-slate-400">{lead.score}</span>
                  </div>
                </td>
                <td className="py-3 pr-4">
                  <Badge tone={statusTone(lead.status)}>{lead.status}</Badge>
                </td>
                <td className="py-3 pr-4 text-xs text-slate-500">
                  {new Date(lead.first_seen_at).toLocaleDateString('ru')}
                </td>
                <td className="py-3 text-xs text-slate-500">
                  {lead.last_seen_at ? new Date(lead.last_seen_at).toLocaleString('ru') : '—'}
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </Shell>
  );
}

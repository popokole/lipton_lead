'use client';

import { Shell } from '@/components/shell';
import { Badge, Card, Empty, ErrorText, PageHeader, Table, statusTone } from '@/components/ui';
import { useApi } from '@/lib/hooks';
import type { Worker } from '@/lib/types';

export default function WorkersPage() {
  const workers = useApi<Worker[]>('/workers', 5000);

  return (
    <Shell>
      <PageHeader
        title="Воркеры"
        subtitle="Список берётся из пульса в Redis: пропавший воркер исчезает сам"
      />
      <ErrorText>{workers.error}</ErrorText>

      <Card>
        {(workers.data?.length ?? 0) === 0 ? (
          <Empty>Ни один воркер не отчитывается — обработка сообщений сейчас не идёт</Empty>
        ) : (
          <Table head={['Имя', 'Статус', 'Хост', 'Аккаунты', 'Пульс', 'Ошибка']}>
            {workers.data?.map((worker) => (
              <tr key={worker.id} className="border-b border-ink-800/70 last:border-0">
                <td className="py-3 pr-4">
                  <div className="text-slate-200">{worker.name}</div>
                  <div className="text-xs text-slate-600">
                    v{worker.version} · pid {worker.pid}
                  </div>
                </td>
                <td className="py-3 pr-4">
                  <Badge tone={statusTone(worker.status)}>{worker.status}</Badge>
                </td>
                <td className="py-3 pr-4 text-xs text-slate-500">{worker.hostname}</td>
                <td className="py-3 pr-4 text-slate-300">{worker.accounts_count}</td>
                <td className="py-3 pr-4 text-xs text-slate-500">
                  {worker.updated_at ? new Date(worker.updated_at).toLocaleTimeString('ru') : '—'}
                </td>
                <td className="py-3 text-xs text-rose-300/80">{worker.last_error ?? ''}</td>
              </tr>
            ))}
          </Table>
        )}
      </Card>
    </Shell>
  );
}

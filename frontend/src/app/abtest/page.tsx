'use client';

import { useEffect, useState } from 'react';

import { Shell } from '@/components/shell';
import { Badge, Button, Card, Empty, ErrorText, Field, PageHeader, Table, inputClass } from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi } from '@/lib/hooks';
import type { Scenario } from '@/lib/types';

interface AbVariant {
  id: string;
  scenario_id: string;
  text: string;
  enabled: boolean;
  sent_count: number;
  reply_count: number;
  reply_rate: number;
  created_at: string;
}

export default function AbTestPage() {
  const scenarios = useApi<Scenario[]>('/scenarios', 30_000);
  const [scenarioId, setScenarioId] = useState<string>('');
  const [variants, setVariants] = useState<AbVariant[]>([]);
  const [text, setText] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Первый сценарий выбираем сами.
  useEffect(() => {
    if (!scenarioId && scenarios.data && scenarios.data.length > 0) {
      setScenarioId(scenarios.data[0].id);
    }
  }, [scenarios.data, scenarioId]);

  async function loadVariants(id: string) {
    if (!id) return;
    try {
      setVariants(await api.get<AbVariant[]>(`/ab/${id}`));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  useEffect(() => {
    void loadVariants(scenarioId);
  }, [scenarioId]);

  async function add() {
    if (!text.trim() || !scenarioId) return;
    setBusy(true);
    setError(null);
    try {
      await api.post(`/ab/${scenarioId}`, { text: text.trim() });
      setText('');
      await loadVariants(scenarioId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function toggle(v: AbVariant) {
    try {
      await api.patch(`/ab/variant/${v.id}`, { enabled: !v.enabled });
      await loadVariants(scenarioId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function remove(v: AbVariant) {
    try {
      await api.delete(`/ab/variant/${v.id}`);
      await loadVariants(scenarioId);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  const best = variants
    .filter((v) => v.sent_count >= 5)
    .sort((a, b) => b.reply_rate - a.reply_rate)[0];

  return (
    <Shell>
      <PageHeader
        title="A/B заходов"
        subtitle="Варианты первого сообщения новому лиду. Платформа шлёт их по очереди и считает, какой чаще получает ответ"
      />
      <ErrorText>{error ?? scenarios.error}</ErrorText>

      <Card className="mb-6">
        <Field label="Сценарий">
          <select
            className={`${inputClass} max-w-md`}
            value={scenarioId}
            onChange={(e) => setScenarioId(e.target.value)}
          >
            {scenarios.data?.map((s) => (
              <option key={s.id} value={s.id}>
                {s.name}
              </option>
            ))}
          </select>
        </Field>
        <p className="mt-3 text-xs text-slate-500">
          Пока есть включённые варианты — первый ответ новому собеседнику берётся из них (по очереди),
          а не из ИИ. Дальше диалог ведёт ИИ. Ответ засчитывается, если собеседник написал в ответ.
        </p>
      </Card>

      <Card title="Добавить заход" className="mb-6">
        <div className="flex items-end gap-3">
          <Field label="Текст первого сообщения">
            <textarea
              className={`${inputClass} h-20 w-full resize-y`}
              value={text}
              onChange={(e) => setText(e.target.value)}
              placeholder="привет) сама недавно искала, могу поделиться контактом…"
            />
          </Field>
          <Button onClick={add} disabled={busy || !text.trim()}>
            {busy ? '…' : 'Добавить'}
          </Button>
        </div>
      </Card>

      <Card title="Варианты и конверсия">
        {variants.length === 0 ? (
          <Empty>Заходов пока нет — добавьте 2–3, чтобы сравнить</Empty>
        ) : (
          <Table head={['Текст', 'Отправлено', 'Ответили', 'Конверсия', 'Статус', '']}>
            {variants.map((v) => (
              <tr key={v.id} className="border-b border-ink-800/70 last:border-0">
                <td className="max-w-md py-3 pr-4 text-sm text-slate-200">
                  {v.text.slice(0, 160)}
                  {v.text.length > 160 ? '…' : ''}
                </td>
                <td className="py-3 pr-4 text-slate-400">{v.sent_count}</td>
                <td className="py-3 pr-4 text-slate-400">{v.reply_count}</td>
                <td className="py-3 pr-4">
                  <span className={best && best.id === v.id ? 'font-semibold text-emerald-300' : 'text-slate-200'}>
                    {(v.reply_rate * 100).toFixed(0)}%
                  </span>
                  {best && best.id === v.id && <span className="ml-1 text-xs text-emerald-400">лучший</span>}
                </td>
                <td className="py-3 pr-4">
                  <button onClick={() => toggle(v)}>
                    {v.enabled ? <Badge tone="ok">вкл</Badge> : <Badge>выкл</Badge>}
                  </button>
                </td>
                <td className="py-3">
                  <Button variant="danger" onClick={() => remove(v)}>
                    Удалить
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

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
import type { Scenario } from '@/lib/types';

const SAMPLE_PROMPT =
  'Ты менеджер студии дизайна. Отвечай коротко, по делу и дружелюбно. ' +
  'Уточни задачу и предложи созвон. Не называй цены и сроки, которых нет в контексте.';

export default function ScenariosPage() {
  const scenarios = useApi<Scenario[]>('/scenarios', 15_000);
  const [name, setName] = useState('');
  const [prompt, setPrompt] = useState(SAMPLE_PROMPT);
  const [maxLength, setMaxLength] = useState('500');
  const [fallback, setFallback] = useState('');
  const [grounding, setGrounding] = useState(false);
  const [handoff, setHandoff] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      await api.post('/scenarios', {
        name: name.trim(),
        system_prompt: prompt.trim(),
        max_reply_length: maxLength ? Number(maxLength) : null,
        fallback_text: fallback.trim() || null,
        require_knowledge_grounding: grounding,
        human_handoff_enabled: handoff,
        context_messages: 15,
      });
      setName('');
      await scenarios.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(scenario: Scenario) {
    try {
      await api.delete(`/scenarios/${scenario.id}`);
      await scenarios.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <Shell>
      <PageHeader title="Сценарии" subtitle="Как именно AI формулирует ответ" />
      <ErrorText>{error ?? scenarios.error}</ErrorText>

      <Card title="Новый сценарий" className="mb-6">
        <div className="grid gap-4 lg:grid-cols-2">
          <Field label="Название">
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Максимальная длина ответа" hint="Символов; пусто — без ограничения">
            <input
              className={inputClass}
              value={maxLength}
              onChange={(e) => setMaxLength(e.target.value.replace(/\D/g, ''))}
            />
          </Field>
          <div className="lg:col-span-2">
            <Field label="Системный промпт">
              <textarea
                className={`${inputClass} h-28 resize-y`}
                value={prompt}
                onChange={(e) => setPrompt(e.target.value)}
              />
            </Field>
          </div>
          <Field
            label="Запасной текст"
            hint="Отправится, если модель откажется отвечать. Пусто — диалог уйдёт человеку"
          >
            <input
              className={inputClass}
              value={fallback}
              onChange={(e) => setFallback(e.target.value)}
            />
          </Field>
          <label className="flex items-end gap-2 pb-2 text-sm text-slate-400">
            <input
              type="checkbox"
              checked={grounding}
              onChange={(e) => setGrounding(e.target.checked)}
              className="h-4 w-4 rounded border-ink-600 bg-ink-950"
            />
            Отвечать только по базе знаний
          </label>
          <label className="flex items-end gap-2 pb-2 text-sm text-slate-400">
            <input
              type="checkbox"
              checked={handoff}
              onChange={(e) => setHandoff(e.target.checked)}
              className="h-4 w-4 rounded border-ink-600 bg-ink-950"
            />
            Передавать оператору на чувствительных темах
          </label>
        </div>
        <div className="mt-4">
          <Button onClick={create} disabled={busy || !name.trim() || !prompt.trim()}>
            {busy ? 'Сохраняем…' : 'Создать сценарий'}
          </Button>
        </div>
      </Card>

      <Card title="Список">
        {(scenarios.data?.length ?? 0) === 0 ? (
          <Empty>Сценариев пока нет</Empty>
        ) : (
          <Table head={['Название', 'Промпт', 'Ответ', 'Состояние', '']}>
            {scenarios.data?.map((scenario) => (
              <tr key={scenario.id} className="border-b border-ink-800/70 last:border-0">
                <td className="py-3 pr-4 text-slate-200">{scenario.name}</td>
                <td className="max-w-md py-3 pr-4 text-xs text-slate-500">
                  {scenario.system_prompt.slice(0, 160)}
                  {scenario.system_prompt.length > 160 ? '…' : ''}
                </td>
                <td className="py-3 pr-4 text-xs text-slate-500">
                  до {scenario.max_reply_length ?? '∞'} симв.
                  {scenario.require_knowledge_grounding && ' · только по базе знаний'}
                </td>
                <td className="py-3 pr-4">
                  {scenario.enabled ? <Badge tone="ok">включён</Badge> : <Badge>выключен</Badge>}
                </td>
                <td className="py-3">
                  <Button variant="danger" onClick={() => remove(scenario)}>
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

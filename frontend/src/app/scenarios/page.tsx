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
  const [replyInDm, setReplyInDm] = useState(false);
  const [groupAck, setGroupAck] = useState('Отправлю в лс 🙂');
  const [leadCriteria, setLeadCriteria] = useState('');
  const [reviewUncertain, setReviewUncertain] = useState(false);
  const [reviewMin, setReviewMin] = useState('0.4');
  const knowledgeBases = useApi<{ id: string; name: string }[]>('/knowledge', 30_000);
  const [kbId, setKbId] = useState('');
  const [oneShot, setOneShot] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  function resetForm() {
    setEditingId(null);
    setName('');
    setPrompt(SAMPLE_PROMPT);
    setMaxLength('500');
    setFallback('');
    setGrounding(false);
    setHandoff(false);
    setReplyInDm(false);
    setGroupAck('Отправлю в лс 🙂');
    setLeadCriteria('');
    setReviewUncertain(false);
    setReviewMin('0.4');
    setKbId('');
    setOneShot(false);
  }

  function startEdit(s: Scenario) {
    setEditingId(s.id);
    setName(s.name);
    setPrompt(s.system_prompt);
    setMaxLength(s.max_reply_length ? String(s.max_reply_length) : '');
    setFallback(s.fallback_text ?? '');
    setGrounding(s.require_knowledge_grounding);
    setHandoff(s.human_handoff_enabled ?? false);
    setReplyInDm(s.reply_in_dm ?? false);
    setGroupAck(s.group_ack_text ?? '');
    setLeadCriteria(s.lead_criteria ?? '');
    setReviewUncertain(s.review_when_uncertain ?? false);
    setReviewMin(s.review_min_confidence != null ? String(s.review_min_confidence) : '0.4');
    setKbId(s.knowledge_base_id ?? '');
    setOneShot(s.one_shot ?? false);
    if (typeof window !== 'undefined') window.scrollTo({ top: 0, behavior: 'smooth' });
  }

  async function save() {
    setBusy(true);
    setError(null);
    try {
      const payload = {
        name: name.trim(),
        system_prompt: prompt.trim(),
        max_reply_length: maxLength ? Number(maxLength) : null,
        fallback_text: fallback.trim() || null,
        require_knowledge_grounding: grounding,
        human_handoff_enabled: handoff,
        reply_in_dm: replyInDm,
        group_ack_text: groupAck.trim() || null,
        lead_criteria: leadCriteria.trim() || null,
        review_when_uncertain: reviewUncertain,
        review_min_confidence: reviewUncertain && reviewMin ? Number(reviewMin) : null,
        knowledge_base_id: kbId || null,
        one_shot: oneShot,
        context_messages: 15,
      };
      if (editingId) {
        await api.patch(`/scenarios/${editingId}`, payload);
      } else {
        await api.post('/scenarios', payload);
      }
      resetForm();
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

      <Card title={editingId ? 'Изменить сценарий' : 'Новый сценарий'} className="mb-6">
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
          <Field label="База знаний" hint="ИИ будет отвечать по её материалам (цены, FAQ). Пусто — без базы">
            <select className={inputClass} value={kbId} onChange={(e) => setKbId(e.target.value)}>
              <option value="">— не привязывать —</option>
              {knowledgeBases.data?.map((kb) => (
                <option key={kb.id} value={kb.id}>
                  {kb.name}
                </option>
              ))}
            </select>
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
          <label className="flex items-end gap-2 pb-2 text-sm text-slate-400">
            <input
              type="checkbox"
              checked={replyInDm}
              onChange={(e) => setReplyInDm(e.target.checked)}
              className="h-4 w-4 rounded border-ink-600 bg-ink-950"
            />
            В группе — короткий ответ, полный ИИ-ответ в личку
          </label>
          <label className="flex items-end gap-2 pb-2 text-sm text-slate-400">
            <input
              type="checkbox"
              checked={oneShot}
              onChange={(e) => setOneShot(e.target.checked)}
              className="h-4 w-4 rounded border-ink-600 bg-ink-950"
            />
            Один заход: ответить собеседнику один раз и больше не писать
          </label>
          {replyInDm && (
            <Field label="Фраза в группу" hint="Что видят в чате; полный ответ уйдёт в личку">
              <input
                className={inputClass}
                value={groupAck}
                onChange={(e) => setGroupAck(e.target.value)}
              />
            </Field>
          )}
          <div className="lg:col-span-2">
            <Field
              label="Критерий лида (для анализатора)"
              hint="Что считать настоящим запросом, а что нет. Пусто — берётся системный промпт"
            >
              <textarea
                className={`${inputClass} h-20 resize-y`}
                value={leadCriteria}
                onChange={(e) => setLeadCriteria(e.target.value)}
                placeholder="Настоящий лид: человек сам ищет услугу для себя. НЕ лид: шутки, за другого, уже есть…"
              />
            </Field>
          </div>
          <label className="flex items-end gap-2 pb-2 text-sm text-slate-400">
            <input
              type="checkbox"
              checked={reviewUncertain}
              onChange={(e) => setReviewUncertain(e.target.checked)}
              className="h-4 w-4 rounded border-ink-600 bg-ink-950"
            />
            Сомнительные — на подтверждение (кнопки в лог-чате)
          </label>
          {reviewUncertain && (
            <Field
              label="Порог сомнения"
              hint="Уверенность от этого значения до порога правила уходит на подтверждение"
            >
              <input
                className={inputClass}
                value={reviewMin}
                onChange={(e) => setReviewMin(e.target.value.replace(/[^0-9.]/g, ''))}
                placeholder="0.4"
              />
            </Field>
          )}
        </div>
        <div className="mt-4 flex gap-2">
          <Button onClick={save} disabled={busy || !name.trim() || !prompt.trim()}>
            {busy ? 'Сохраняем…' : editingId ? 'Сохранить' : 'Создать сценарий'}
          </Button>
          {editingId && (
            <Button variant="ghost" onClick={resetForm} disabled={busy}>
              Отмена
            </Button>
          )}
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
                  <div className="flex gap-2">
                    <Button variant="ghost" onClick={() => startEdit(scenario)}>
                      Изменить
                    </Button>
                    <Button variant="danger" onClick={() => remove(scenario)}>
                      Удалить
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

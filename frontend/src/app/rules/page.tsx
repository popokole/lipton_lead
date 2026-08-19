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
import type { Account, ActionType, Rule, Scenario } from '@/lib/types';

const ACTIONS: ActionType[] = [
  'REPLY',
  'NOTIFY_ADMIN',
  'SAVE_LEAD',
  'TAG_USER',
  'ESCALATE_TO_HUMAN',
  'IGNORE',
];

export default function RulesPage() {
  const rules = useApi<Rule[]>('/rules', 15_000);
  const scenarios = useApi<Scenario[]>('/scenarios');
  const accounts = useApi<Account[]>('/accounts');

  const [name, setName] = useState('');
  const [terms, setTerms] = useState('нужен дизайнер, ищу дизайнера');
  const [exclude, setExclude] = useState('');
  const [scenarioId, setScenarioId] = useState('');
  const [scope, setScope] = useState<'CHAT_MONITOR' | 'DIALOG' | 'ALL'>('ALL');
  const [accountIds, setAccountIds] = useState<string[]>([]);
  const [action, setAction] = useState<ActionType>('REPLY');
  const [aiEnabled, setAiEnabled] = useState(true);
  const [threshold, setThreshold] = useState('0.8');
  const [cooldown, setCooldown] = useState('600');
  const [priority, setPriority] = useState('100');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Люди разделяют слова по-разному: запятой, с новой строки, звёздочками.
  // Принимаем всё перечисленное — иначе список молча превращается в одну
  // строку, которая никогда ни с чем не совпадёт.
  const split = (value: string) =>
    value
      .split(/[,;*\r\n]+/)
      .map((item) => item.trim())
      .filter(Boolean);

  async function create() {
    setBusy(true);
    setError(null);
    try {
      await api.post('/rules', {
        name: name.trim(),
        priority: Number(priority) || 100,
        scope,
        scenario_id: scenarioId || null,
        keywords: { terms: split(terms), exclude: split(exclude), mode: 'substring' },
        filters: { incoming_only: true, text_only: true },
        ai_enabled: aiEnabled,
        ai_threshold: aiEnabled ? Number(threshold) : null,
        cooldown: { user: Number(cooldown) || 0 },
        action,
        account_ids: accountIds,
      });
      setName('');
      await rules.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function toggle(rule: Rule) {
    try {
      await api.patch(`/rules/${rule.id}`, { enabled: !rule.enabled });
      await rules.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function remove(rule: Rule) {
    try {
      await api.delete(`/rules/${rule.id}`);
      await rules.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <Shell>
      <PageHeader
        title="Правила"
        subtitle="Проверяются по убыванию приоритета, первое сработавшее останавливает подбор"
      />
      <ErrorText>{error ?? rules.error}</ErrorText>

      <Card title="Новое правило" className="mb-6">
        <div className="grid gap-4 lg:grid-cols-3">
          <Field label="Название">
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label="Ключевые слова" hint="Через запятую, с новой строки или через *">
            <input className={inputClass} value={terms} onChange={(e) => setTerms(e.target.value)} />
          </Field>
          <Field label="Исключения" hint="Сообщение с этими словами пропускается">
            <input
              className={inputClass}
              value={exclude}
              onChange={(e) => setExclude(e.target.value)}
            />
          </Field>
          <Field label="Где применять" hint="Личные сообщения и чаты обрабатываются разными конвейерами">
            <select
              className={inputClass}
              value={scope}
              onChange={(e) => setScope(e.target.value as 'CHAT_MONITOR' | 'DIALOG' | 'ALL')}
            >
              <option value="ALL">везде (личка + чаты)</option>
              <option value="CHAT_MONITOR">только в отслеживаемых чатах</option>
              <option value="DIALOG">только в личных сообщениях</option>
            </select>
          </Field>
          <Field
            label="Аккаунты"
            hint="Пусто — правило работает на всех. Иначе только на выбранных"
          >
            <div className="space-y-1 rounded-lg border border-ink-600 bg-ink-950 p-2">
              {(accounts.data?.length ?? 0) === 0 && (
                <span className="text-xs text-slate-600">Нет аккаунтов</span>
              )}
              {accounts.data?.map((account) => (
                <label key={account.id} className="flex items-center gap-2 text-sm text-slate-300">
                  <input
                    type="checkbox"
                    className="h-4 w-4 rounded border-ink-600 bg-ink-950"
                    checked={accountIds.includes(account.id)}
                    onChange={(e) =>
                      setAccountIds((prev) =>
                        e.target.checked
                          ? [...prev, account.id]
                          : prev.filter((id) => id !== account.id),
                      )
                    }
                  />
                  {account.label}
                  {account.username && (
                    <span className="text-xs text-slate-500">@{account.username}</span>
                  )}
                </label>
              ))}
            </div>
          </Field>
          <Field label="Сценарий">
            <select
              className={inputClass}
              value={scenarioId}
              onChange={(e) => setScenarioId(e.target.value)}
            >
              <option value="">— без сценария —</option>
              {scenarios.data?.map((scenario) => (
                <option key={scenario.id} value={scenario.id}>
                  {scenario.name}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Действие">
            <select
              className={inputClass}
              value={action}
              onChange={(e) => setAction(e.target.value as ActionType)}
            >
              {ACTIONS.map((item) => (
                <option key={item} value={item}>
                  {item}
                </option>
              ))}
            </select>
          </Field>
          <Field label="Приоритет" hint="Больше — раньше проверяется">
            <input
              className={inputClass}
              value={priority}
              onChange={(e) => setPriority(e.target.value.replace(/\D/g, ''))}
            />
          </Field>
          <Field label="Пауза на пользователя" hint="Секунд между ответами одному человеку">
            <input
              className={inputClass}
              value={cooldown}
              onChange={(e) => setCooldown(e.target.value.replace(/\D/g, ''))}
            />
          </Field>
          <Field label="Порог уверенности AI" hint="0…1, применяется при включённой проверке">
            <input
              className={inputClass}
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
              disabled={!aiEnabled}
            />
          </Field>
          <label className="flex items-end gap-2 pb-2 text-sm text-slate-400">
            <input
              type="checkbox"
              checked={aiEnabled}
              onChange={(e) => setAiEnabled(e.target.checked)}
              className="h-4 w-4 rounded border-ink-600 bg-ink-950"
            />
            Проверять сообщение через AI
          </label>
        </div>
        <div className="mt-4">
          <Button onClick={create} disabled={busy || !name.trim()}>
            {busy ? 'Сохраняем…' : 'Создать правило'}
          </Button>
        </div>
      </Card>

      <Card title="Список">
        {(rules.data?.length ?? 0) === 0 ? (
          <Empty>Правил пока нет</Empty>
        ) : (
          <Table head={['Правило', 'Где', 'Слова', 'AI', 'Действие', 'Пауза', 'Состояние', '']}>
            {rules.data?.map((rule) => (
              <tr key={rule.id} className="border-b border-ink-800/70 last:border-0">
                <td className="py-3 pr-4">
                  <div className="text-slate-200">{rule.name}</div>
                  <div className="text-xs text-slate-600">приоритет {rule.priority}</div>
                </td>
                <td className="py-3 pr-4 text-xs text-slate-400">
                  {rule.scope === 'ALL' ? 'везде' : rule.scope === 'DIALOG' ? 'личка' : 'чаты'}
                </td>
                <td className="max-w-xs py-3 pr-4 text-xs text-slate-500">
                  {(rule.keywords.terms ?? []).join(', ') || '—'}
                  {(rule.keywords.exclude ?? []).length > 0 && (
                    <div className="text-rose-300/70">
                      кроме: {(rule.keywords.exclude ?? []).join(', ')}
                    </div>
                  )}
                </td>
                <td className="py-3 pr-4 text-xs">
                  {rule.ai_enabled ? (
                    <Badge tone="info">порог {rule.ai_threshold}</Badge>
                  ) : (
                    <span className="text-slate-600">нет</span>
                  )}
                </td>
                <td className="py-3 pr-4 text-xs text-slate-400">{rule.action}</td>
                <td className="py-3 pr-4 text-xs text-slate-500">
                  {rule.cooldown?.user ? `${rule.cooldown.user} с` : '—'}
                </td>
                <td className="py-3 pr-4">
                  {rule.enabled ? <Badge tone="ok">включено</Badge> : <Badge>выключено</Badge>}
                </td>
                <td className="py-3">
                  <div className="flex gap-2">
                    <Button variant="ghost" onClick={() => toggle(rule)}>
                      {rule.enabled ? 'Выключить' : 'Включить'}
                    </Button>
                    <Button variant="danger" onClick={() => remove(rule)}>
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

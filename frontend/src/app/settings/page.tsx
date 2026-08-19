'use client';

import { useEffect, useState } from 'react';

import { Shell } from '@/components/shell';
import { Button, Card, ErrorText, Field, PageHeader, inputClass } from '@/components/ui';
import { ApiError, api } from '@/lib/api';

interface NotifyStatus {
  enabled: boolean;
  configured: boolean;
  group_id: number | null;
  bot_username: string | null;
  last_error: string | null;
}

export default function SettingsPage() {
  const [status, setStatus] = useState<NotifyStatus | null>(null);
  const [token, setToken] = useState('');
  const [groupId, setGroupId] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function load() {
    try {
      const s = await api.get<NotifyStatus>('/notify');
      setStatus(s);
      if (s.group_id) setGroupId(String(s.group_id));
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  useEffect(() => {
    void load();
  }, []);

  async function save(patch: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      const s = await api.put<NotifyStatus>('/notify', patch);
      setStatus(s);
      setToken('');
      setMsg('Сохранено');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function prepareOrSync(path: '/notify/prepare' | '/notify/sync') {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      const r = await api.post<{ created: number; existing: number; renamed: number; scenarios: number }>(path);
      setMsg(
        `Готово: сценариев ${r.scenarios}, создано топиков ${r.created}` +
          (r.renamed ? `, переименовано ${r.renamed}` : '') +
          (r.existing ? `, уже было ${r.existing}` : ''),
      );
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function test() {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      const r = await api.post<{ bot_username: string }>('/notify/test');
      setMsg(`Бот на связи: @${r.bot_username}`);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Shell>
      <PageHeader title="Настройки" subtitle="Бот-уведомления: отчёты о лидах в форум-группу по топикам" />
      <ErrorText>{error}</ErrorText>
      {msg && (
        <p className="mb-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">
          {msg}
        </p>
      )}

      <Card title="Бот-уведомления">
        <p className="mb-4 text-sm text-slate-400">
          Отдельный бот (@BotFather) шлёт карточку каждого лида в форум-группу: свой топик на каждый
          сценарий. Бот должен быть <b>админом</b> группы с правом управлять топиками, а у группы —
          включены темы (Topics).
        </p>

        <div className="grid gap-4 lg:grid-cols-2">
          <Field label="Токен бота" hint={status?.configured ? 'Токен сохранён. Введите новый, чтобы заменить' : 'Из @BotFather'}>
            <input
              className={inputClass}
              type="password"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder={status?.configured ? '•••••••• (сохранён)' : '123456:ABC-...'}
            />
          </Field>
          <Field label="ID форум-группы" hint="Отрицательный, напр. -1001234567890">
            <input
              className={inputClass}
              value={groupId}
              onChange={(e) => setGroupId(e.target.value.replace(/[^0-9-]/g, ''))}
              placeholder="-1001234567890"
            />
          </Field>
        </div>

        <div className="mt-4 flex flex-wrap items-center gap-3">
          <Button
            disabled={busy || (!token && !groupId)}
            onClick={() =>
              save({
                ...(token ? { bot_token: token } : {}),
                ...(groupId ? { group_id: Number(groupId) } : {}),
              })
            }
          >
            {busy ? 'Сохраняем…' : 'Сохранить'}
          </Button>
          <Button variant="ghost" disabled={busy || !status?.configured} onClick={test}>
            Проверить связь
          </Button>
          <Button
            variant="ghost"
            disabled={busy || !status?.configured || !status?.group_id}
            onClick={() => prepareOrSync('/notify/prepare')}
          >
            Подготовить группу
          </Button>
          <Button
            variant="ghost"
            disabled={busy || !status?.configured || !status?.group_id}
            onClick={() => prepareOrSync('/notify/sync')}
          >
            Синхронизировать
          </Button>
          <label className="flex items-center gap-2 text-sm text-slate-300">
            <input
              type="checkbox"
              checked={status?.enabled ?? false}
              disabled={busy || !status?.configured || !status?.group_id}
              onChange={(e) => save({ enabled: e.target.checked })}
              className="h-4 w-4 rounded border-ink-600 bg-ink-950"
            />
            Включить уведомления
          </label>
        </div>

        {status && (
          <div className="mt-4 text-xs text-slate-500">
            {status.configured ? (
              <>
                Бот {status.bot_username ? `@${status.bot_username}` : ''} · группа{' '}
                {status.group_id ?? 'не задана'} · {status.enabled ? 'включён' : 'выключен'}
              </>
            ) : (
              'Бот ещё не настроен'
            )}
            {status.last_error && <p className="mt-1 text-rose-300">Последняя ошибка: {status.last_error}</p>}
          </div>
        )}
      </Card>
    </Shell>
  );
}

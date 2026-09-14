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

      <PersonaCard />
    </Shell>
  );
}

interface PersonaState {
  enabled: boolean;
  character: string | null;
  examples: string | null;
  base_rules: string | null;
  max_reply_length: number | null;
}

/** Глобальная «личность» ИИ: характер и примеры переписки. */
function PersonaCard() {
  const [enabled, setEnabled] = useState(false);
  const [character, setCharacter] = useState('');
  const [examples, setExamples] = useState('');
  const [baseRules, setBaseRules] = useState('');
  const [maxLen, setMaxLen] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void (async () => {
      try {
        const p = await api.get<PersonaState>('/persona');
        setEnabled(p.enabled);
        setCharacter(p.character ?? '');
        setExamples(p.examples ?? '');
        setBaseRules(p.base_rules ?? '');
        setMaxLen(p.max_reply_length != null ? String(p.max_reply_length) : '');
      } catch (err) {
        setError(err instanceof ApiError ? err.message : String(err));
      }
    })();
  }, []);

  async function save(patch: Record<string, unknown>) {
    setBusy(true);
    setError(null);
    setMsg(null);
    try {
      const p = await api.put<PersonaState>('/persona', patch);
      setEnabled(p.enabled);
      setMsg('Сохранено');
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card title="Личность собеседника">
      <p className="mb-4 text-sm text-slate-400">
        Единая «личность» для всех ответов ИИ: кто он и как пишет. Подмешивается{' '}
        <b>поверх</b> промпта сценария — сценарий решает, что и кому отвечать, а личность задаёт
        характер и тон. Примеры переписки работают как образец стиля, дословно не копируются.
      </p>
      {error && <ErrorText>{error}</ErrorText>}
      {msg && (
        <p className="mb-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-300">
          {msg}
        </p>
      )}

      <div className="grid gap-4">
        <Field label="Характер / кто это" hint="Имя, возраст, привычки речи, что можно и нельзя">
          <textarea
            className={`${inputClass} min-h-32 resize-y`}
            value={character}
            onChange={(e) => setCharacter(e.target.value)}
            placeholder={'например: Настя, 26 лет, живая и дружелюбная, пишет с маленькой буквы, без официоза, короткими фразами…'}
          />
        </Field>
        <Field label="Примеры переписки (история)" hint="Реальные фразы этого человека — по одной на строку. Задают тон">
          <textarea
            className={`${inputClass} min-h-40 resize-y`}
            value={examples}
            onChange={(e) => setExamples(e.target.value)}
            placeholder={'привет) да я как раз недавно так же искала\nне, это вообще не сложно, сейчас скину\nой, у меня то же самое было, помогло вот что…'}
          />
        </Field>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <Button disabled={busy} onClick={() => save({ character, examples })}>
          {busy ? 'Сохраняем…' : 'Сохранить'}
        </Button>
        <label className="flex items-center gap-2 text-sm text-slate-300">
          <input
            type="checkbox"
            checked={enabled}
            disabled={busy}
            onChange={(e) => save({ enabled: e.target.checked })}
            className="h-4 w-4 rounded border-ink-600 bg-ink-950"
          />
          Включить личность
        </label>
        <span className="text-xs text-slate-500">
          {enabled ? 'личность применяется ко всем ответам' : 'выключена — отвечаем только по сценарию'}
        </span>
      </div>

      <div className="mt-6 border-t border-ink-800 pt-5">
        <h3 className="mb-1 text-sm font-semibold text-slate-200">Общие правила и длина ответа</h3>
        <p className="mb-4 text-sm text-slate-400">
          Действуют <b>в целом</b>, поверх всех сценариев и <b>всегда</b> — даже если личность
          выключена. Базовый промпт заменяет стандартные правила ответа; длина применяется, если у
          сценария своя не задана (у сценария она приоритетнее).
        </p>
        <div className="grid gap-4">
          <Field
            label="Базовый промпт (стиль и правила для всех ответов)"
            hint="Пусто — используются стандартные зашитые правила"
          >
            <textarea
              className={`${inputClass} min-h-40 resize-y`}
              value={baseRules}
              onChange={(e) => setBaseRules(e.target.value)}
              placeholder={
                'например: пиши как живой человек, коротко, с маленькой буквы, без длинных тире, не раскрывай что ты ии, если чего-то не знаешь — импровизируй, не пиши что нет данных…'
              }
            />
          </Field>
          <Field
            label="Максимальная длина ответа (в целом)"
            hint="Символов; пусто или 0 — без общего ограничения"
          >
            <input
              className={inputClass}
              value={maxLen}
              onChange={(e) => setMaxLen(e.target.value.replace(/\D/g, ''))}
              placeholder="например 300"
            />
          </Field>
        </div>
        <div className="mt-4">
          <Button
            disabled={busy}
            onClick={() =>
              save({ base_rules: baseRules, max_reply_length: maxLen ? Number(maxLen) : 0 })
            }
          >
            {busy ? 'Сохраняем…' : 'Сохранить общие'}
          </Button>
        </div>
      </div>
    </Card>
  );
}

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
  statusTone,
} from '@/components/ui';
import { ApiError, api } from '@/lib/api';
import { useApi } from '@/lib/hooks';
import type { Account, Chat, Dialog, Proxy, TDataAccount } from '@/lib/types';

type AuthStep = 'phone' | 'code' | 'password' | 'done';

interface AuthResult {
  ok: boolean;
  password_required: boolean;
  authorized: boolean;
  detail: string | null;
}

export default function AccountsPage() {
  const accounts = useApi<Account[]>('/accounts', 5000);
  const proxies = useApi<Proxy[]>('/proxies');
  const [label, setLabel] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [authFor, setAuthFor] = useState<Account | null>(null);
  const [importFor, setImportFor] = useState<Account | null>(null);
  const [dialogsFor, setDialogsFor] = useState<Account | null>(null);

  async function createAccount() {
    if (!label.trim()) return;
    setCreating(true);
    setError(null);
    try {
      await api.post('/accounts', { label: label.trim() });
      setLabel('');
      await accounts.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  }

  async function toggle(account: Account) {
    try {
      await api.patch(`/accounts/${account.id}`, { enabled: !account.enabled });
      await accounts.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function setProxy(account: Account, proxyId: string) {
    setError(null);
    try {
      await api.patch(`/accounts/${account.id}`,
        proxyId ? { proxy_id: proxyId } : { detach_proxy: true });
      await accounts.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  async function remove(account: Account) {
    if (!confirm(`Удалить аккаунт «${account.label}» вместе со всей его историей?`)) return;
    try {
      await api.delete(`/accounts/${account.id}`);
      await accounts.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <Shell>
      <PageHeader title="Аккаунты" subtitle="Telegram-аккаунты, которыми управляет система" />
      <ErrorText>{error ?? accounts.error}</ErrorText>

      <Card title="Добавить аккаунт" className="mb-6">
        <div className="flex flex-wrap items-end gap-3">
          <div className="w-72">
            <Field label="Название" hint="Только для панели, в Telegram не видно">
              <input
                className={inputClass}
                value={label}
                onChange={(event) => setLabel(event.target.value)}
                placeholder="Основной аккаунт"
              />
            </Field>
          </div>
          <Button onClick={createAccount} disabled={creating || !label.trim()}>
            {creating ? 'Создаём…' : 'Создать'}
          </Button>
        </div>
      </Card>

      <ProxyCard proxies={proxies} onError={setError} />

      <Card title="Список">
        {(accounts.data?.length ?? 0) === 0 ? (
          <Empty>Аккаунтов пока нет</Empty>
        ) : (
          <Table head={['Название', 'Telegram', 'Статус', 'Сеть', 'Ошибка', 'Действия']}>
            {accounts.data?.map((account) => (
              <tr key={account.id} className="border-b border-ink-800/70 last:border-0">
                <td className="py-3 pr-4">
                  <div className="text-slate-200">{account.label}</div>
                  <div className="text-xs text-slate-600">{account.id.slice(0, 8)}</div>
                </td>
                <td className="py-3 pr-4">
                  {account.tg_user_id ? (
                    <div>
                      <div>{account.display_name ?? '—'}</div>
                      <div className="text-xs text-slate-500">
                        {account.username ? `@${account.username}` : account.tg_user_id}
                      </div>
                    </div>
                  ) : (
                    <span className="text-slate-600">не авторизован</span>
                  )}
                </td>
                <td className="py-3 pr-4">
                  <Badge tone={statusTone(account.status)}>{account.status}</Badge>
                  {!account.enabled && <span className="ml-2 text-xs text-slate-600">выключен</span>}
                </td>
                <td className="py-3 pr-4">
                  <select
                    className="rounded-lg border border-ink-600 bg-ink-950 px-2 py-1 text-xs text-slate-300"
                    value={account.proxy_id ?? ''}
                    onChange={(event) => setProxy(account, event.target.value)}
                  >
                    <option value="">напрямую</option>
                    {proxies.data?.map((proxy) => (
                      <option key={proxy.id} value={proxy.id}>
                        {proxy.name}
                      </option>
                    ))}
                  </select>
                </td>
                <td className="max-w-[220px] truncate py-3 pr-4 text-xs text-rose-300/80">
                  {account.last_error ?? ''}
                </td>
                <td className="py-3">
                  <div className="flex flex-wrap gap-2">
                    <Button variant="ghost" onClick={() => setAuthFor(account)}>
                      {account.tg_user_id ? 'Войти заново' : 'Вход по номеру'}
                    </Button>
                    <Button variant="ghost" onClick={() => setImportFor(account)}>
                      Импорт tdata
                    </Button>
                    <Button variant="ghost" onClick={() => setDialogsFor(account)}>
                      Диалоги
                    </Button>
                    <Button variant="ghost" onClick={() => toggle(account)}>
                      {account.enabled ? 'Выключить' : 'Включить'}
                    </Button>
                    <Button variant="danger" onClick={() => remove(account)}>
                      Удалить
                    </Button>
                  </div>
                </td>
              </tr>
            ))}
          </Table>
        )}
      </Card>

      {authFor && (
        <AuthWizard
          account={authFor}
          onClose={() => {
            setAuthFor(null);
            void accounts.reload();
          }}
        />
      )}

      {importFor && (
        <TDataImport
          account={importFor}
          onClose={() => {
            setImportFor(null);
            void accounts.reload();
          }}
        />
      )}

      {dialogsFor && <DialogPicker account={dialogsFor} onClose={() => setDialogsFor(null)} />}
    </Shell>
  );
}

function Modal({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) {
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="max-h-[85vh] w-full max-w-2xl overflow-y-auto rounded-2xl border border-ink-700 bg-ink-900">
        <header className="flex items-center justify-between border-b border-ink-700 px-5 py-3">
          <h2 className="text-sm font-medium text-slate-200">{title}</h2>
          <button onClick={onClose} className="text-slate-500 hover:text-slate-300">
            ✕
          </button>
        </header>
        <div className="p-5">{children}</div>
      </div>
    </div>
  );
}

function AuthWizard({ account, onClose }: { account: Account; onClose: () => void }) {
  const [step, setStep] = useState<AuthStep>('phone');
  const [phone, setPhone] = useState('');
  const [apiId, setApiId] = useState('');
  const [apiHash, setApiHash] = useState('');
  const [code, setCode] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function run<T>(action: () => Promise<T>, next: (result: T) => void) {
    setBusy(true);
    setError(null);
    try {
      next(await action());
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Авторизация «${account.label}»`} onClose={onClose}>
      <div className="space-y-4">
        {step === 'phone' && (
          <>
            <p className="text-sm text-slate-400">
              Нужны собственные api_id и api_hash с my.telegram.org. Если они уже заданы в
              переменных окружения, поля можно оставить пустыми.
            </p>
            <Field label="Телефон" hint="В международном формате, например +79991234567">
              <input
                className={inputClass}
                value={phone}
                onChange={(event) => setPhone(event.target.value)}
                placeholder="+7…"
              />
            </Field>
            <div className="grid gap-3 sm:grid-cols-2">
              <Field label="api_id (необязательно)">
                <input className={inputClass} value={apiId} onChange={(e) => setApiId(e.target.value)} />
              </Field>
              <Field label="api_hash (необязательно)">
                <input className={inputClass} value={apiHash} onChange={(e) => setApiHash(e.target.value)} />
              </Field>
            </div>
            <ErrorText>{error}</ErrorText>
            <Button
              disabled={busy || phone.trim().length < 5}
              onClick={() =>
                run<AuthResult>(
                  () =>
                    api.post('/accounts/' + account.id + '/send-code', {
                      phone: phone.trim(),
                      api_id: apiId ? Number(apiId) : null,
                      api_hash: apiHash || null,
                    }),
                  (result) => {
                    if (result.ok) setStep('code');
                    else setError(result.detail ?? 'Telegram не принял запрос');
                  },
                )
              }
            >
              {busy ? 'Отправляем…' : 'Получить код'}
            </Button>
          </>
        )}

        {step === 'code' && (
          <>
            <p className="text-sm text-slate-400">Код пришёл в Telegram на этот номер.</p>
            <Field label="Код подтверждения">
              <input className={inputClass} value={code} onChange={(e) => setCode(e.target.value)} />
            </Field>
            <ErrorText>{error}</ErrorText>
            <Button
              disabled={busy || code.trim().length < 3}
              onClick={() =>
                run<AuthResult>(
                  () => api.post(`/accounts/${account.id}/sign-in`, { code: code.trim() }),
                  (result) => {
                    if (result.password_required) setStep('password');
                    else if (result.authorized) setStep('done');
                    else setError(result.detail ?? 'Не удалось войти');
                  },
                )
              }
            >
              {busy ? 'Проверяем…' : 'Войти'}
            </Button>
          </>
        )}

        {step === 'password' && (
          <>
            <p className="text-sm text-slate-400">
              У аккаунта включена двухфакторная защита — нужен облачный пароль.
            </p>
            <Field label="Пароль 2FA">
              <input
                className={inputClass}
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </Field>
            <ErrorText>{error}</ErrorText>
            <Button
              disabled={busy || password.length === 0}
              onClick={() =>
                run<AuthResult>(
                  () => api.post(`/accounts/${account.id}/sign-in-password`, { password }),
                  (result) => {
                    if (result.authorized) setStep('done');
                    else setError(result.detail ?? 'Пароль не подошёл');
                  },
                )
              }
            >
              {busy ? 'Проверяем…' : 'Подтвердить'}
            </Button>
          </>
        )}

        {step === 'done' && (
          <>
            <p className="text-sm text-emerald-300">
              Аккаунт авторизован. Воркер подключит его в течение нескольких секунд.
            </p>
            <Button onClick={onClose}>Готово</Button>
          </>
        )}
      </div>
    </Modal>
  );
}

function DialogPicker({ account, onClose }: { account: Account; onClose: () => void }) {
  const dialogs = useApi<Dialog[]>(`/accounts/${account.id}/dialogs?limit=200`);
  const chats = useApi<Chat[]>(`/chats?account_id=${account.id}`);
  const [busy, setBusy] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const monitored = new Set((chats.data ?? []).filter((chat) => chat.monitored).map((chat) => chat.tg_chat_id));

  async function add(dialog: Dialog) {
    setBusy(dialog.tg_chat_id);
    setError(null);
    try {
      await api.post('/chats', {
        account_id: account.id,
        tg_chat_id: dialog.tg_chat_id,
        type: dialog.is_user ? 'PRIVATE' : dialog.is_channel ? 'CHANNEL' : 'SUPERGROUP',
        title: dialog.title,
        username: dialog.username,
        monitored: true,
      });
      await chats.reload();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(null);
    }
  }

  return (
    <Modal title={`Диалоги «${account.label}»`} onClose={onClose}>
      <ErrorText>{error ?? dialogs.error}</ErrorText>
      {dialogs.loading ? (
        <Empty>Запрашиваем список у Telegram…</Empty>
      ) : (dialogs.data?.length ?? 0) === 0 ? (
        <Empty>Диалогов нет или аккаунт не подключён к воркеру</Empty>
      ) : (
        <ul className="space-y-1">
          {dialogs.data?.map((dialog) => (
            <li
              key={dialog.tg_chat_id}
              className="flex items-center justify-between gap-3 rounded-lg px-3 py-2 hover:bg-ink-800"
            >
              <div className="min-w-0">
                <div className="truncate text-sm text-slate-200">{dialog.title ?? dialog.tg_chat_id}</div>
                <div className="text-xs text-slate-600">
                  {dialog.is_user ? 'личный' : dialog.is_channel ? 'канал' : 'группа'} ·{' '}
                  {dialog.tg_chat_id}
                </div>
              </div>
              {monitored.has(dialog.tg_chat_id) ? (
                <Badge tone="ok">под наблюдением</Badge>
              ) : (
                <Button
                  variant="ghost"
                  disabled={busy === dialog.tg_chat_id}
                  onClick={() => add(dialog)}
                >
                  Следить
                </Button>
              )}
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}

function TDataImport({ account, onClose }: { account: Account; onClose: () => void }) {
  const [files, setFiles] = useState<FileList | null>(null);
  const [found, setFound] = useState<TDataAccount[] | null>(null);
  const [chosen, setChosen] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  function formData(): FormData {
    const form = new FormData();
    for (const file of Array.from(files ?? [])) {
      // Для выбора папки браузер отдаёт относительный путь — он важен,
      // tdata это набор файлов с фиксированной структурой.
      const relative = (file as File & { webkitRelativePath?: string }).webkitRelativePath;
      form.append('files', file, relative || file.name);
    }
    return form;
  }

  async function inspect() {
    setBusy(true);
    setError(null);
    try {
      const accounts = await api.upload<TDataAccount[]>('/imports/tdata/inspect', formData());
      setFound(accounts);
      setChosen(accounts[0]?.index ?? null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function run() {
    setBusy(true);
    setError(null);
    try {
      const form = formData();
      if (chosen !== null) form.append('account_index', String(chosen));
      await api.upload<Account>(`/imports/tdata/${account.id}`, form);
      setDone(true);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Modal title={`Импорт tdata в «${account.label}»`} onClose={onClose}>
      <div className="space-y-4">
        <p className="text-sm text-slate-400">
          Выберите папку <span className="font-mono text-slate-300">tdata</span> из Telegram
          Desktop или ZIP-архив с ней. Импортируйте только свои аккаунты: это перенос доступа,
          который у вас уже есть. Вход в самом Telegram Desktop при этом не слетает.
        </p>

        <Field label="Папка tdata или архив">
          <input
            type="file"
            multiple
            // @ts-expect-error нестандартные атрибуты выбора папки
            webkitdirectory=""
            directory=""
            onChange={(event) => {
              setFiles(event.target.files);
              setFound(null);
              setDone(false);
            }}
            className="block w-full text-sm text-slate-400 file:mr-3 file:rounded-lg file:border-0 file:bg-ink-700 file:px-3 file:py-1.5 file:text-sm file:text-slate-200"
          />
        </Field>

        <p className="text-xs text-slate-600">
          Если браузер не даёт выбрать папку — соберите её в ZIP и выберите архив как файл.
        </p>

        <ErrorText>{error}</ErrorText>

        {found && found.length > 0 && !done && (
          <Field label="Аккаунт внутри tdata">
            <select
              className={inputClass}
              value={chosen ?? ''}
              onChange={(event) => setChosen(Number(event.target.value))}
            >
              {found.map((item) => (
                <option key={item.index} value={item.index}>
                  #{item.index} · Telegram ID {item.tg_user_id} · DC {item.dc_id}
                </option>
              ))}
            </select>
          </Field>
        )}

        {done ? (
          <>
            <p className="text-sm text-emerald-300">
              Сессия импортирована. Воркер подключит аккаунт в течение нескольких секунд.
            </p>
            <Button onClick={onClose}>Готово</Button>
          </>
        ) : (
          <div className="flex gap-2">
            <Button variant="ghost" disabled={busy || !files?.length} onClick={inspect}>
              {busy ? 'Читаем…' : 'Показать аккаунты'}
            </Button>
            <Button disabled={busy || !files?.length} onClick={run}>
              {busy ? 'Импортируем…' : 'Импортировать'}
            </Button>
          </div>
        )}
      </div>
    </Modal>
  );
}

function ProxyCard({
  proxies,
  onError,
}: {
  proxies: ReturnType<typeof useApi<Proxy[]>>;
  onError: (message: string) => void;
}) {
  const [name, setName] = useState('Локальный');
  const [host, setHost] = useState('host.docker.internal');
  const [port, setPort] = useState('10808');
  const [scheme, setScheme] = useState('socks5');
  const [busy, setBusy] = useState(false);

  async function create() {
    setBusy(true);
    try {
      await api.post('/proxies', {
        name: name.trim(),
        scheme,
        host: host.trim(),
        port: Number(port),
      });
      await proxies.reload();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  }

  async function remove(proxy: Proxy) {
    try {
      await api.delete(`/proxies/${proxy.id}`);
      await proxies.reload();
    } catch (err) {
      onError(err instanceof ApiError ? err.message : String(err));
    }
  }

  return (
    <Card title="Прокси" className="mb-6">
      <p className="mb-4 text-sm text-slate-400">
        Нужен, если Telegram не открывается напрямую. Локальный клиент (v2rayN, Nekoray, Clash)
        слушает на вашей машине, а система работает в контейнере — поэтому адрес
        <span className="mx-1 font-mono text-slate-300">host.docker.internal</span>, а не
        <span className="mx-1 font-mono text-slate-300">127.0.0.1</span>.
      </p>

      <div className="flex flex-wrap items-end gap-3">
        <div className="w-40">
          <Field label="Название">
            <input className={inputClass} value={name} onChange={(e) => setName(e.target.value)} />
          </Field>
        </div>
        <div className="w-28">
          <Field label="Тип">
            <select className={inputClass} value={scheme} onChange={(e) => setScheme(e.target.value)}>
              <option value="socks5">socks5</option>
              <option value="socks4">socks4</option>
              <option value="http">http</option>
            </select>
          </Field>
        </div>
        <div className="w-56">
          <Field label="Хост">
            <input className={inputClass} value={host} onChange={(e) => setHost(e.target.value)} />
          </Field>
        </div>
        <div className="w-24">
          <Field label="Порт">
            <input
              className={inputClass}
              value={port}
              onChange={(e) => setPort(e.target.value.replace(/\D/g, ''))}
            />
          </Field>
        </div>
        <Button onClick={create} disabled={busy || !name.trim() || !host.trim() || !port}>
          {busy ? 'Сохраняем…' : 'Добавить'}
        </Button>
      </div>

      {(proxies.data?.length ?? 0) > 0 && (
        <ul className="mt-4 space-y-1">
          {proxies.data?.map((proxy) => (
            <li
              key={proxy.id}
              className="flex items-center justify-between rounded-lg border border-ink-800 px-3 py-2 text-sm"
            >
              <span className="text-slate-300">
                {proxy.name}{' '}
                <span className="font-mono text-xs text-slate-500">
                  {proxy.scheme}://{proxy.host}:{proxy.port}
                </span>
              </span>
              <Button variant="danger" onClick={() => remove(proxy)}>
                Удалить
              </Button>
            </li>
          ))}
        </ul>
      )}
    </Card>
  );
}

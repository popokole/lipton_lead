'use client';

import { useRouter } from 'next/navigation';
import { useState } from 'react';

import { Button, ErrorText, Field, inputClass } from '@/components/ui';
import { login } from '@/lib/api';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await login(email, password);
      router.replace('/');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Не удалось войти');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center px-4">
      <form
        onSubmit={submit}
        className="w-full max-w-sm space-y-4 rounded-2xl border border-ink-700 bg-ink-900 p-7"
      >
        <div>
          <h1 className="text-lg font-semibold text-slate-100">Вход в панель</h1>
          <p className="mt-1 text-sm text-slate-500">
            Учётные данные из ADMIN_EMAIL и ADMIN_PASSWORD в .env
          </p>
        </div>

        <Field label="Email">
          <input
            className={inputClass}
            type="email"
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            required
          />
        </Field>

        <Field label="Пароль">
          <input
            className={inputClass}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            required
          />
        </Field>

        <ErrorText>{error}</ErrorText>

        <Button type="submit" disabled={busy} className="w-full">
          {busy ? 'Проверяем…' : 'Войти'}
        </Button>
      </form>
    </div>
  );
}

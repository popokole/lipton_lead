'use client';

/**
 * Клиент REST API.
 *
 * Запросы идут по относительным путям: браузер обращается к nginx, а тот
 * проксирует `/api` в бэкенд. Так нет ни CORS, ни зашитого в сборку адреса
 * сервера — один и тот же образ работает и локально, и на VPS.
 */

const ACCESS_KEY = 'tgai.access';
const REFRESH_KEY = 'tgai.refresh';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
    readonly code?: string,
  ) {
    super(message);
  }
}

export const tokens = {
  access: () => (typeof window === 'undefined' ? null : localStorage.getItem(ACCESS_KEY)),
  refresh: () => (typeof window === 'undefined' ? null : localStorage.getItem(REFRESH_KEY)),
  save(access: string, refresh: string) {
    localStorage.setItem(ACCESS_KEY, access);
    localStorage.setItem(REFRESH_KEY, refresh);
  },
  clear() {
    localStorage.removeItem(ACCESS_KEY);
    localStorage.removeItem(REFRESH_KEY);
  },
};

async function refreshTokens(): Promise<boolean> {
  const refresh = tokens.refresh();
  if (!refresh) return false;

  const response = await fetch('/api/auth/refresh', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ refresh_token: refresh }),
  });
  if (!response.ok) return false;

  const data = await response.json();
  tokens.save(data.access_token, data.refresh_token);
  return true;
}

async function send<T>(path: string, init: RequestInit, retry = true): Promise<T> {
  const access = tokens.access();
  const response = await fetch(`/api${path}`, {
    ...init,
    headers: {
      'Content-Type': 'application/json',
      ...(access ? { Authorization: `Bearer ${access}` } : {}),
      ...(init.headers ?? {}),
    },
  });

  // Короткий access истекает часто — один раз молча продлеваем сессию,
  // чтобы пользователь не видел случайных разлогинов посреди работы.
  if (response.status === 401 && retry && (await refreshTokens())) {
    return send<T>(path, init, false);
  }

  if (response.status === 401) {
    tokens.clear();
    if (typeof window !== 'undefined' && !window.location.pathname.startsWith('/login')) {
      window.location.href = '/login';
    }
    throw new ApiError('Требуется вход', 401);
  }

  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    let code: string | undefined;
    try {
      const body = await response.json();
      message = body?.error?.message ?? message;
      code = body?.error?.code;
    } catch {
      /* тело может быть пустым — оставляем общий текст */
    }
    throw new ApiError(message, response.status, code);
  }

  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

/** Загрузка файлов: Content-Type проставляет браузер вместе с boundary. */
async function sendForm<T>(path: string, form: FormData, retry = true): Promise<T> {
  const access = tokens.access();
  const response = await fetch(`/api${path}`, {
    method: 'POST',
    body: form,
    headers: access ? { Authorization: `Bearer ${access}` } : {},
  });

  if (response.status === 401 && retry && (await refreshTokens())) {
    return sendForm<T>(path, form, false);
  }

  if (!response.ok) {
    let message = `Ошибка ${response.status}`;
    try {
      const body = await response.json();
      message = body?.error?.message ?? message;
    } catch {
      /* тело может быть пустым */
    }
    throw new ApiError(message, response.status);
  }

  return (await response.json()) as T;
}

export const api = {
  get: <T,>(path: string) => send<T>(path, { method: 'GET' }),
  post: <T,>(path: string, body?: unknown) =>
    send<T>(path, { method: 'POST', body: body === undefined ? undefined : JSON.stringify(body) }),
  patch: <T,>(path: string, body: unknown) =>
    send<T>(path, { method: 'PATCH', body: JSON.stringify(body) }),
  put: <T,>(path: string, body: unknown) =>
    send<T>(path, { method: 'PUT', body: JSON.stringify(body) }),
  delete: <T,>(path: string) => send<T>(path, { method: 'DELETE' }),
  upload: <T,>(path: string, form: FormData) => sendForm<T>(path, form),
};

export async function login(email: string, password: string): Promise<void> {
  const response = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
  });
  if (!response.ok) {
    const body = await response.json().catch(() => null);
    throw new ApiError(body?.error?.message ?? 'Не удалось войти', response.status);
  }
  const data = await response.json();
  tokens.save(data.access_token, data.refresh_token);
}

export function logout(): void {
  tokens.clear();
  window.location.href = '/login';
}

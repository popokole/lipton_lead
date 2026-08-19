'use client';

import { useCallback, useEffect, useRef, useState } from 'react';

import { ApiError, api, tokens } from './api';
import type { RealtimeEvent } from './types';

/** Загрузка данных с ручным обновлением и опциональным опросом. */
export function useApi<T>(path: string | null, pollMs = 0) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(path !== null);

  const reload = useCallback(async () => {
    if (path === null) return;
    try {
      setData(await api.get<T>(path));
      setError(null);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, [path]);

  useEffect(() => {
    void reload();
    if (pollMs <= 0) return;
    const timer = setInterval(() => void reload(), pollMs);
    return () => clearInterval(timer);
  }, [reload, pollMs]);

  return { data, error, loading, reload, setData };
}

/**
 * Живая лента событий.
 *
 * Сокет переподключается сам: панель, открытая на весь день, переживает и
 * перезапуск API, и обрыв сети, не требуя перезагрузки страницы.
 */
export function useRealtime(limit = 100) {
  const [events, setEvents] = useState<RealtimeEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let stopped = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      const token = tokens.access();
      if (!token || stopped) return;

      const scheme = window.location.protocol === 'https:' ? 'wss' : 'ws';
      const socket = new WebSocket(
        `${scheme}://${window.location.host}/ws?token=${encodeURIComponent(token)}`,
      );
      socketRef.current = socket;

      socket.onopen = () => setConnected(true);
      socket.onmessage = (message) => {
        try {
          const event = JSON.parse(message.data) as RealtimeEvent;
          setEvents((previous) => [event, ...previous].slice(0, limit));
        } catch {
          /* мусор в канале не должен ломать ленту */
        }
      };
      socket.onclose = () => {
        setConnected(false);
        if (!stopped) retry = setTimeout(connect, 3000);
      };
      socket.onerror = () => socket.close();
    };

    connect();
    return () => {
      stopped = true;
      if (retry) clearTimeout(retry);
      socketRef.current?.close();
    };
  }, [limit]);

  return { events, connected, clear: () => setEvents([]) };
}

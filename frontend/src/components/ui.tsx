'use client';

import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

const CARD_TONES: Record<string, string> = {
  secondary: 'border-ink-700 bg-ink-900',
  primary: 'border-accent/40 bg-accent-soft',
  info: 'border-sky-500/30 bg-sky-500/[0.06]',
};

export function Card({
  title,
  actions,
  children,
  className = '',
  tone = 'secondary',
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  tone?: 'secondary' | 'primary' | 'info';
}) {
  return (
    <section className={`rounded-xl border ${CARD_TONES[tone] ?? CARD_TONES.secondary} ${className}`}>
      {(title || actions) && (
        <header className="flex items-center justify-between gap-4 border-b border-ink-700 px-5 py-3">
          <h2 className="text-sm font-medium text-slate-200">{title}</h2>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <div className="rounded-xl border border-ink-700 bg-ink-900 px-5 py-4">
      <div className="text-xs uppercase tracking-wide text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-slate-100">{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-500">{hint}</div>}
    </div>
  );
}

const TONES: Record<string, string> = {
  ok: 'bg-emerald-500/15 text-emerald-300 border-emerald-500/30',
  warn: 'bg-amber-500/15 text-amber-300 border-amber-500/30',
  bad: 'bg-rose-500/15 text-rose-300 border-rose-500/30',
  mute: 'bg-slate-500/10 text-slate-400 border-slate-500/25',
  info: 'bg-sky-500/15 text-sky-300 border-sky-500/30',
};

export function Badge({ children, tone = 'mute' }: { children: ReactNode; tone?: keyof typeof TONES }) {
  return (
    <span className={`rounded-md border px-2 py-0.5 text-xs font-medium ${TONES[tone] ?? TONES.mute}`}>
      {children}
    </span>
  );
}

export function Button({
  children,
  onClick,
  type = 'button',
  variant = 'primary',
  disabled,
  className = '',
  title,
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: 'button' | 'submit';
  variant?: 'primary' | 'ghost' | 'danger';
  disabled?: boolean;
  className?: string;
  title?: string;
}) {
  const styles = {
    primary: 'bg-accent text-white hover:bg-accent/85',
    ghost: 'border border-ink-600 text-slate-300 hover:bg-ink-800',
    danger: 'border border-rose-500/40 text-rose-300 hover:bg-rose-500/10',
  }[variant];

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`inline-flex min-h-9 items-center justify-center whitespace-nowrap rounded-lg px-4 py-2 text-sm font-medium transition active:scale-[0.98] disabled:cursor-not-allowed disabled:opacity-40 ${styles} ${className}`}
    >
      {children}
    </button>
  );
}

export function Field({
  label,
  hint,
  children,
}: {
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-xs font-medium text-slate-400">{label}</span>
      {children}
      {hint && <span className="mt-1 block text-xs text-slate-500">{hint}</span>}
    </label>
  );
}

export const inputClass =
  'w-full min-h-9 rounded-lg border border-ink-600 bg-ink-950 px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-accent';

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-left text-sm">
        <thead>
          <tr className="text-xs uppercase tracking-wide text-slate-500">
            {head.map((column) => (
              <th key={column} className="border-b border-ink-700 pb-2 pr-4 font-medium">
                {column}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="text-slate-300">{children}</tbody>
      </table>
    </div>
  );
}

export function ChatAvatar({
  chatId,
  title,
  hasAvatar,
  size = 28,
}: {
  chatId: string | null;
  title: string | null;
  hasAvatar?: boolean;
  size?: number;
}) {
  const letter = (title ?? '?').trim().charAt(0).toUpperCase() || '#';
  const style = { width: size, height: size, minWidth: size };
  // Аватар грузим только когда он есть: иначе каждый чат дёргал бы Telegram.
  if (chatId && hasAvatar) {
    return (
      // eslint-disable-next-line @next/next/no-img-element
      <img
        src={`/api/chats/${chatId}/avatar`}
        alt=""
        style={style}
        className="rounded-full object-cover"
      />
    );
  }
  return (
    <span
      style={style}
      className="flex items-center justify-center rounded-full bg-ink-700 text-xs font-medium text-slate-300"
    >
      {letter}
    </span>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="py-6 text-center text-sm text-slate-500">{children}</p>;
}

export function ErrorText({ children }: { children: ReactNode }) {
  if (!children) return null;
  return (
    <p className="rounded-lg border border-rose-500/30 bg-rose-500/10 px-3 py-2 text-sm text-rose-300">
      {children}
    </p>
  );
}

export function PageHeader({ title, subtitle, actions }: { title: string; subtitle?: string; actions?: ReactNode }) {
  return (
    <div className="mb-6 flex flex-wrap items-end justify-between gap-4">
      <div>
        <h1 className="text-xl font-semibold text-slate-100">{title}</h1>
        {subtitle && <p className="mt-1 text-sm text-slate-500">{subtitle}</p>}
      </div>
      <div className="flex items-center gap-2">{actions}</div>
    </div>
  );
}

/** Простой график по дням: столбики без внешних библиотек. */
export function BarChart({ points, color = '#4f8cff' }: { points: { day: string; value: number }[]; color?: string }) {
  if (points.length === 0) return <Empty>Пока нет данных</Empty>;
  const max = Math.max(...points.map((point) => point.value), 1);

  return (
    <div className="flex h-28 items-end gap-1">
      {points.map((point) => (
        <div key={point.day} className="group relative flex-1" title={`${point.day}: ${point.value}`}>
          <div
            className="w-full rounded-t transition-all"
            style={{ height: `${Math.max((point.value / max) * 100, 2)}%`, backgroundColor: color }}
          />
        </div>
      ))}
    </div>
  );
}

const STATUS_DOT: Record<string, string> = {
  ok: 'bg-success',
  warn: 'bg-warning',
  bad: 'bg-danger',
  info: 'bg-sky-400',
  mute: 'bg-slate-500',
};

export function StatusBadge({
  children,
  tone = 'mute',
  pulse = false,
}: {
  children: ReactNode;
  tone?: keyof typeof TONES;
  pulse?: boolean;
}) {
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-slate-400">
      <span className="relative flex h-2 w-2">
        {pulse && (
          <span
            className={`absolute inline-flex h-full w-full animate-ping rounded-full opacity-60 ${STATUS_DOT[tone] ?? STATUS_DOT.mute}`}
          />
        )}
        <span
          className={`relative inline-flex h-2 w-2 rounded-full ${STATUS_DOT[tone] ?? STATUS_DOT.mute}`}
        />
      </span>
      {children}
    </span>
  );
}

export function Skeleton({ className = '' }: { className?: string }) {
  return <div className={`animate-skeleton-pulse rounded-md bg-ink-700 ${className}`} />;
}

export function EmptyState({
  icon,
  title,
  description,
  action,
}: {
  icon?: ReactNode;
  title: string;
  description?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center gap-2 py-10 text-center">
      {icon && <div className="text-2xl text-slate-600">{icon}</div>}
      <div className="text-sm font-medium text-slate-300">{title}</div>
      {description && <p className="max-w-sm text-xs text-slate-500">{description}</p>}
      {action && <div className="mt-2">{action}</div>}
    </div>
  );
}

export function Trend({ current, previous }: { current: number; previous: number }) {
  if (previous === 0) return null;
  const pct = Math.round(((current - previous) / previous) * 100);
  if (pct === 0) return <span className="text-xs text-slate-500">без изменений</span>;
  const up = pct > 0;
  return (
    <span className={`text-xs font-medium ${up ? 'text-success' : 'text-danger'}`}>
      {up ? '↑' : '↓'} {Math.abs(pct)}% к пред. дню
    </span>
  );
}

interface Toast {
  id: number;
  text: string;
  tone: 'ok' | 'bad' | 'info';
}

const ToastContext = createContext<((text: string, tone?: Toast['tone']) => void) | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([]);

  const push = useCallback((text: string, tone: Toast['tone'] = 'info') => {
    const id = Date.now() + Math.random();
    setToasts((prev) => [...prev, { id, text, tone }]);
    setTimeout(() => setToasts((prev) => prev.filter((t) => t.id !== id)), 4000);
  }, []);

  const toneClass: Record<Toast['tone'], string> = {
    ok: 'border-success/40 bg-success/10 text-success',
    bad: 'border-danger/40 bg-danger/10 text-danger',
    info: 'border-accent/40 bg-accent-soft text-slate-100',
  };

  return (
    <ToastContext.Provider value={push}>
      {children}
      <div className="pointer-events-none fixed bottom-4 right-4 z-50 flex flex-col gap-2">
        {toasts.map((toast) => (
          <div
            key={toast.id}
            className={`pointer-events-auto rounded-lg border px-4 py-2.5 text-sm shadow-lg ${toneClass[toast.tone]}`}
          >
            {toast.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast(): (text: string, tone?: Toast['tone']) => void {
  const ctx = useContext(ToastContext);
  return useMemo(() => ctx ?? (() => undefined), [ctx]);
}

export function statusTone(status: string): keyof typeof TONES {
  if (['ONLINE', 'HEALTHY', 'SENT', 'REPLIED', 'ACTED'].includes(status)) return 'ok';
  if (['ERROR', 'FAILED', 'DISABLED'].includes(status)) return 'bad';
  if (
    ['AUTH_REQUIRED', 'AUTHENTICATING', 'DEGRADED', 'HUMAN_REQUIRED', 'PENDING', 'ESCALATED'].includes(
      status,
    )
  )
    return 'warn';
  if (['MATCHED', 'ACTIVE', 'HOT'].includes(status)) return 'info';
  return 'mute';
}

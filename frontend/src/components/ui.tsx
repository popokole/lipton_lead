'use client';

import type { ReactNode } from 'react';

export function Card({
  title,
  actions,
  children,
  className = '',
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-xl border border-ink-700 bg-ink-900 ${className}`}>
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

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: string }) {
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
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: 'button' | 'submit';
  variant?: 'primary' | 'ghost' | 'danger';
  disabled?: boolean;
  className?: string;
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

export function statusTone(status: string): keyof typeof TONES {
  if (['ONLINE', 'HEALTHY', 'SENT', 'REPLIED'].includes(status)) return 'ok';
  if (['ERROR', 'FAILED', 'DISABLED'].includes(status)) return 'bad';
  if (['AUTH_REQUIRED', 'AUTHENTICATING', 'DEGRADED', 'HUMAN_REQUIRED', 'PENDING'].includes(status))
    return 'warn';
  if (['MATCHED', 'ACTIVE', 'HOT'].includes(status)) return 'info';
  return 'mute';
}

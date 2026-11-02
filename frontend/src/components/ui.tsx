'use client';

import Link from 'next/link';
import { createContext, useCallback, useContext, useMemo, useState } from 'react';
import type {
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react';

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
  interactive = false,
}: {
  title?: ReactNode;
  actions?: ReactNode;
  children: ReactNode;
  className?: string;
  tone?: 'secondary' | 'primary' | 'info';
  /** Карточка целиком — ссылка: подсветка рамки при наведении. */
  interactive?: boolean;
}) {
  return (
    <section
      className={`rounded-xl border ${CARD_TONES[tone] ?? CARD_TONES.secondary} ${
        interactive ? 'transition-colors hover:border-accent/50' : ''
      } ${className}`}
    >
      {(title || actions) && (
        <header className="flex items-center justify-between gap-4 border-b border-ink-700 px-5 py-3">
          <Heading level={2}>{title}</Heading>
          <div className="flex items-center gap-2">{actions}</div>
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

export function Stat({ label, value, hint }: { label: string; value: ReactNode; hint?: ReactNode }) {
  return (
    <Tile className="px-5 py-4">
      <div className="text-xs font-medium text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums text-slate-100">{value}</div>
      {hint && <div className="mt-1 text-xs text-slate-500">{hint}</div>}
    </Tile>
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
  size = 'md',
}: {
  children: ReactNode;
  onClick?: () => void;
  type?: 'button' | 'submit';
  variant?: 'primary' | 'ghost' | 'danger' | 'text';
  disabled?: boolean;
  className?: string;
  title?: string;
  size?: 'md' | 'sm';
}) {
  const styles = {
    primary: 'bg-accent text-white hover:bg-accent/85',
    ghost: 'border border-ink-600 text-slate-300 hover:bg-ink-800',
    danger: 'border border-rose-500/40 text-rose-300 hover:bg-rose-500/10',
    text: 'text-slate-400 underline-offset-2 hover:text-slate-200 hover:underline',
  }[variant];
  const sizes =
    variant === 'text'
      ? 'text-xs'
      : size === 'sm'
        ? 'min-h-8 gap-1.5 rounded-lg px-3 py-1.5 text-xs'
        : 'min-h-9 gap-2 rounded-lg px-4 py-2 text-sm';

  return (
    <button
      type={type}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`inline-flex items-center justify-center whitespace-nowrap font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-40 ${sizes} ${styles} ${className}`}
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
  'w-full min-h-9 rounded-lg border border-ink-600 bg-ink-950 px-3 py-2 text-sm text-slate-100 outline-none placeholder:text-slate-600 focus:border-accent focus-visible:ring-1 focus-visible:ring-accent';

const smallInputClass =
  'min-h-8 rounded-lg border border-ink-600 bg-ink-950 px-2 py-1 text-xs text-slate-300 outline-none focus:border-accent focus-visible:ring-1 focus-visible:ring-accent';

/** Однострочное поле. Внешний вид — только отсюда; className — для ширины и раскладки.
 *  icon — иконка внутри поля слева (поиск). */
export function Input({
  className = '',
  icon,
  ...props
}: InputHTMLAttributes<HTMLInputElement> & { icon?: ReactNode }) {
  if (!icon) return <input className={`${inputClass} ${className}`} {...props} />;
  return (
    <div className={`relative ${className}`}>
      <span className="pointer-events-none absolute left-3 top-1/2 -translate-y-1/2 text-slate-500">{icon}</span>
      <input className={`${inputClass} pl-9`} {...props} />
    </div>
  );
}

/** Выпадающий список. size="sm" — компактный фильтр в строке таблицы. */
export function Select({
  className = '',
  size = 'md',
  ...props
}: Omit<SelectHTMLAttributes<HTMLSelectElement>, 'size'> & { size?: 'md' | 'sm' }) {
  return <select className={`${size === 'sm' ? smallInputClass : inputClass} ${className}`} {...props} />;
}

/** Многострочное поле; высоту задаёт className (h-24, min-h-40…), тянется по вертикали. */
export function Textarea({ className = '', ...props }: TextareaHTMLAttributes<HTMLTextAreaElement>) {
  return <textarea className={`${inputClass} resize-y ${className}`} {...props} />;
}

/** Выбор файлов; directory — выбрать папку целиком (Chrome/Edge/Firefox). */
export function FileInput({
  directory = false,
  ...props
}: Omit<InputHTMLAttributes<HTMLInputElement>, 'type' | 'className'> & { directory?: boolean }) {
  const folder = directory ? { webkitdirectory: '', directory: '' } : {};
  return (
    <input
      type="file"
      {...folder}
      {...props}
      className="block w-full text-sm text-slate-400 file:mr-3 file:min-h-8 file:rounded-lg file:border-0 file:bg-ink-700 file:px-3 file:py-1.5 file:text-sm file:text-slate-200"
    />
  );
}

/** Флажок; с children — вместе с подписью справа. */
export function Checkbox({
  children,
  className = '',
  ...props
}: Omit<InputHTMLAttributes<HTMLInputElement>, 'type'> & { children?: ReactNode }) {
  const box = (
    <input
      type="checkbox"
      className="h-4 w-4 shrink-0 rounded border-ink-600 bg-ink-950 accent-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
      {...props}
    />
  );
  if (!children) return box;
  return (
    <label className={`inline-flex cursor-pointer items-center gap-2 text-sm text-slate-300 ${className}`}>
      {box}
      {children}
    </label>
  );
}

/** Заголовки: 1 — страница, 2 — карточка, 3 — блок внутри карточки. */
export function Heading({
  level = 2,
  children,
  className = '',
}: {
  level?: 1 | 2 | 3;
  children: ReactNode;
  className?: string;
}) {
  if (level === 1) return <h1 className={`text-xl font-semibold text-slate-100 ${className}`}>{children}</h1>;
  if (level === 2) return <h2 className={`text-sm font-medium text-slate-200 ${className}`}>{children}</h2>;
  return <h3 className={`text-sm font-medium text-slate-300 ${className}`}>{children}</h3>;
}

/** Поверхность без шапки: плитка, панель списка, модалка. Отступы задаются снаружи. */
export function Tile({
  children,
  className = '',
  as = 'div',
}: {
  children: ReactNode;
  className?: string;
  as?: 'div' | 'section';
}) {
  const Tag = as;
  return <Tag className={`rounded-xl border border-ink-700 bg-ink-900 ${className}`}>{children}</Tag>;
}

const CHIP_ON: Record<string, string> = {
  accent: 'border-accent/40 bg-accent-soft text-slate-100',
  ok: 'border-emerald-500/30 bg-emerald-500/15 text-emerald-300',
  info: 'border-sky-500/30 bg-sky-500/15 text-sky-300',
  warn: 'border-amber-500/30 bg-amber-500/15 text-amber-300',
  test: 'border-fuchsia-500/30 bg-fuchsia-500/15 text-fuchsia-300',
};

/** Переключатель-чип: вкл/выкл одной настройки или фильтра. */
export function ToggleChip({
  on,
  onClick,
  tone = 'accent',
  icon,
  children,
  title,
  disabled,
}: {
  on: boolean;
  onClick: () => void;
  tone?: keyof typeof CHIP_ON;
  icon?: ReactNode;
  children: ReactNode;
  title?: string;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      aria-pressed={on}
      onClick={onClick}
      disabled={disabled}
      title={title}
      className={`inline-flex min-h-8 items-center gap-1.5 rounded-lg border px-3 py-1.5 text-xs font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent disabled:cursor-not-allowed disabled:opacity-40 ${
        on ? CHIP_ON[tone] ?? CHIP_ON.accent : 'border-ink-600 text-slate-400 hover:bg-ink-800'
      }`}
    >
      {icon}
      {children}
    </button>
  );
}

/** Сегменты: один вариант из нескольких (Личка / Группы, сортировка). */
export function Segmented<T extends string>({
  value,
  onChange,
  options,
  size = 'md',
}: {
  value: T;
  onChange: (value: T) => void;
  options: readonly (readonly [T, ReactNode])[];
  size?: 'md' | 'sm';
}) {
  return (
    <div
      role="radiogroup"
      className={`inline-flex rounded-lg border border-ink-700 bg-ink-950 p-0.5 ${size === 'sm' ? 'text-xs' : 'text-sm'}`}
    >
      {options.map(([key, label]) => (
        <button
          key={key}
          type="button"
          role="radio"
          aria-checked={value === key}
          onClick={() => onChange(key)}
          className={`rounded-md transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent ${
            size === 'sm' ? 'min-h-7 px-2.5 py-1' : 'min-h-8 px-4 py-1.5'
          } ${value === key ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:text-slate-200'}`}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

/** Строка списка, которую выбирают целиком (диалог, база знаний, чат). */
export function RowButton({
  selected = false,
  onClick,
  children,
  className = '',
  title,
}: {
  selected?: boolean;
  onClick: () => void;
  children: ReactNode;
  className?: string;
  title?: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-current={selected || undefined}
      className={`flex w-full min-w-0 items-center gap-3 text-left transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
        selected ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:bg-ink-800'
      } ${className}`}
    >
      {children}
    </button>
  );
}

/** Пункт бокового меню. */
export function NavLink({
  href,
  active,
  icon,
  children,
  size = 'md',
}: {
  href: string;
  active: boolean;
  icon?: ReactNode;
  children: ReactNode;
  size?: 'md' | 'sm';
}) {
  return (
    <Link
      href={href}
      aria-current={active ? 'page' : undefined}
      className={`flex items-center gap-2.5 rounded-lg px-3 text-sm transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent ${
        size === 'sm' ? 'py-1.5' : 'py-2'
      } ${active ? 'bg-accent-soft text-slate-100' : 'text-slate-400 hover:bg-ink-800'}`}
    >
      {icon}
      {children}
    </Link>
  );
}

/** Вкладки раздела: подчёркнутая активная, счётчик — красным. */
export function TabBar({
  tabs,
  value,
  onChange,
}: {
  tabs: { key: string; label: ReactNode; badge?: number }[];
  value: string;
  onChange: (key: string) => void;
}) {
  return (
    <div role="tablist" className="mb-6 flex flex-wrap gap-1 border-b border-ink-700">
      {tabs.map((tab) => {
        const active = tab.key === value;
        return (
          <button
            key={tab.key}
            type="button"
            role="tab"
            aria-selected={active}
            onClick={() => onChange(tab.key)}
            className={`-mb-px flex min-h-10 items-center gap-2 border-b-2 px-4 py-2.5 text-sm font-medium transition-colors focus-visible:outline focus-visible:outline-2 focus-visible:-outline-offset-2 focus-visible:outline-accent ${
              active ? 'border-accent text-slate-100' : 'border-transparent text-slate-500 hover:text-slate-300'
            }`}
          >
            {tab.label}
            {tab.badge ? <Badge tone="bad">{tab.badge}</Badge> : null}
          </button>
        );
      })}
    </div>
  );
}

/** Кнопка только с иконкой: подпись обязательна (скринридер и подсказка). */
export function IconButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className="inline-flex h-9 w-9 shrink-0 items-center justify-center rounded-lg text-slate-400 transition-colors hover:bg-ink-800 hover:text-slate-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent"
    >
      {children}
    </button>
  );
}

export function Table({ head, children }: { head: string[]; children: ReactNode }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[640px] text-left text-sm">
        <thead>
          <tr className="text-xs text-slate-500">
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
  const letter = (title ?? '?').trim().charAt(0).toLocaleUpperCase('ru') || '#';
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

/** Процент из доли 0..1 по правилам русской локали: 0.425 → «43 %». */
export function formatPercent(share: number): string {
  return new Intl.NumberFormat('ru', { style: 'percent', maximumFractionDigits: 0 }).format(share);
}

/** Цитата: исходное сообщение, найденный фрагмент. */
export function Quote({ children, className = '' }: { children: ReactNode; className?: string }) {
  return (
    <div className={`rounded-lg bg-ink-800 px-3 py-2 text-xs text-slate-300 ${className}`}>{children}</div>
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
        <Heading level={1}>{title}</Heading>
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
            className="w-full rounded-t transition-[height]"
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
          // ai-design-check-ignore-next-line pulse-for-live-only -- pulse is only passed for live status (account online, worker alive)
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
            className={`pointer-events-auto rounded-lg border bg-ink-900 px-4 py-2.5 text-sm ${toneClass[toast.tone]}`}
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

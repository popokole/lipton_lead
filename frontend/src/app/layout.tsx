import type { Metadata } from 'next';

import './globals.css';

export const metadata: Metadata = {
  title: 'Telegram AI Platform',
  description: 'Панель мониторинга Telegram-чатов и AI-автоответов',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru">
      <body className="min-h-screen bg-ink-950 font-sans antialiased">{children}</body>
    </html>
  );
}

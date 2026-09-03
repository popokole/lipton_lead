import type { Metadata } from 'next';
import { Inter } from 'next/font/google';

import { ToastProvider } from '@/components/ui';

import './globals.css';

const inter = Inter({ subsets: ['latin', 'cyrillic'], variable: '--font-inter' });

export const metadata: Metadata = {
  title: 'Telegram AI Platform',
  description: 'Панель мониторинга Telegram-чатов и AI-автоответов',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ru" className={inter.variable}>
      <body className="min-h-screen bg-ink-950 font-sans antialiased">
        <ToastProvider>{children}</ToastProvider>
      </body>
    </html>
  );
}

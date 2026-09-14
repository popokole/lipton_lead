/** @type {import('next').NextConfig} */
// Подпуть развёртывания (напр. "/tgaihelper" за общим доменом liptonone.online).
// Пусто → приложение живёт в корне (локальная разработка). Значение читается и
// здесь, и в src/lib/api.ts из одной build-переменной NEXT_PUBLIC_BASE_PATH,
// чтобы ассеты, маршруты и запросы к API были под одним префиксом.
const basePath = process.env.NEXT_PUBLIC_BASE_PATH || '';

const nextConfig = {
  reactStrictMode: true,
  // standalone нужен прод-образу: он копирует только реально используемые
  // зависимости вместо всего node_modules.
  output: 'standalone',
  ...(basePath ? { basePath, assetPrefix: basePath } : {}),
};

export default nextConfig;

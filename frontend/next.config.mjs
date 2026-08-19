/** @type {import('next').NextConfig} */
const nextConfig = {
  reactStrictMode: true,
  // standalone нужен прод-образу: он копирует только реально используемые
  // зависимости вместо всего node_modules.
  output: 'standalone',
};

export default nextConfig;

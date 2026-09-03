import type { Config } from 'tailwindcss';

const config: Config = {
  content: ['./src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: {
          950: '#080b12',
          900: '#111827',
          800: '#1a2332',
          700: '#1f2937',
          600: '#2d3748',
        },
        accent: {
          DEFAULT: '#3b82f6',
          soft: '#152238',
        },
        success: '#22c55e',
        warning: '#eab308',
        danger: '#ef4444',
      },
      fontFamily: {
        sans: ['var(--font-inter)', 'ui-sans-serif', 'system-ui', 'Segoe UI', 'Roboto', 'sans-serif'],
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
    },
  },
  plugins: [],
};

export default config;

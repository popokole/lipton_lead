# syntax=docker/dockerfile:1.7
FROM node:22-alpine AS base
ENV NEXT_TELEMETRY_DISABLED=1
WORKDIR /app


FROM base AS deps
COPY package.json ./
# Без package-lock ставим обычным install; lock появится после первой сборки
# и его стоит закоммитить, чтобы версии перестали плавать.
RUN --mount=type=cache,target=/root/.npm npm install


FROM deps AS dev
COPY . .
EXPOSE 3000
CMD ["npm", "run", "dev"]


FROM deps AS build
COPY . .
RUN npm run build


FROM base AS runtime
ENV NODE_ENV=production
RUN addgroup -g 10001 app && adduser -u 10001 -G app -D app
COPY --from=build --chown=app:app /app/.next/standalone ./
COPY --from=build --chown=app:app /app/.next/static ./.next/static
COPY --from=build --chown=app:app /app/public ./public
USER app
EXPOSE 3000
CMD ["node", "server.js"]

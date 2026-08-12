FROM node:22-alpine AS builder
WORKDIR /app
COPY package.json package-lock.json* ./
RUN npm ci
COPY . .

# `next build` resolves next.config.mjs's rewrites() once and bakes the result into
# .next/routes-manifest.json; `next start` reads that manifest and never calls
# rewrites() again. So CALLHARNESS_API_URL has to be set HERE, at build time —
# setting it only as a runtime container env leaves the baked destination at the
# config's localhost fallback, and every /api/* request from the dashboard dies with
# ECONNREFUSED 127.0.0.1:8010 while the container's own env var looks perfectly correct.
ARG CALLHARNESS_API_URL=http://api:8010
ENV CALLHARNESS_API_URL=$CALLHARNESS_API_URL
RUN npm run build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production
COPY --from=builder /app/.next ./.next
COPY --from=builder /app/node_modules ./node_modules
COPY --from=builder /app/package.json ./
COPY --from=builder /app/next.config.mjs ./

EXPOSE 3010
CMD ["npm", "start"]

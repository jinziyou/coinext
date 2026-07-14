# syntax=docker/dockerfile:1
# ui — React/Vite dashboard → nginx. Host :3000 → container :80 (dev overlay).
# Status: scaffold. Source: operations-interface/services/ui.

FROM node:22-alpine AS builder
WORKDIR /app/ui

COPY operations-interface/services/ui/package.json operations-interface/services/ui/package-lock.json* ./
RUN npm ci || npm install

ARG VITE_API_BASE=/api
ENV VITE_API_BASE=${VITE_API_BASE}

COPY operations-interface/services/ui/ ./
RUN npm run build

FROM nginx:alpine AS runtime
COPY operations-interface/services/ui/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=builder /app/ui/dist /usr/share/nginx/html

EXPOSE 80

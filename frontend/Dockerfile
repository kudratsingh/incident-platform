# The console image: `ghcr.io/<owner>/incident-platform-console`.
#
# Published by .github/workflows/release.yml beside the backend image and on the
# same version, so a release is one pair of digests rather than a backend that
# has moved and a console that has not. The commander's demo compose pins it by
# digest, the same way it pins the platform image.

# ── Stage 1: build ─────────────────────────────────────────────────────────────
FROM node:20-alpine AS builder

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build

# ── Stage 2: serve ─────────────────────────────────────────────────────────────
FROM nginx:1.27-alpine AS runtime

# Remove default nginx config
RUN rm /etc/nginx/conf.d/default.conf

# A TEMPLATE, not a config: the nginx image's entrypoint renders everything in
# /etc/nginx/templates into /etc/nginx/conf.d at container start, substituting
# environment variables. That is what makes the API upstream configurable
# without a rebuild — see frontend/nginx.conf.template for why it has to be.
COPY nginx.conf.template /etc/nginx/templates/app.conf.template

# Where /api/ goes. `api` is the demo stack's backend service name, so the demo
# compose needs no override; the platform's own compose sets http://app:8000.
# In ECS the ALB routes /api/* before nginx sees it, so this value is unused
# there and the deferred DNS resolution keeps it harmless.
ENV API_UPSTREAM=http://api:8000

# Copy built assets from builder
COPY --from=builder /app/dist /usr/share/nginx/html

EXPOSE 80

# Left as CMD, never ENTRYPOINT: the base image's `/docker-entrypoint.sh` is
# what renders the template above, and replacing the entrypoint would silently
# skip it — the container would start, serve the SPA, and 404 every /api/ call.
CMD ["nginx", "-g", "daemon off;"]

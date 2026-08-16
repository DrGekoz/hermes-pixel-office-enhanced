# Hermes Pixel Office — Free Permanent Relay

Cloudflare Workers + Durable Object. This is the everyone-write backbone that
replaces the owner-write-only gist as the multi-player middleman. It:

- stores the leaderboard, chat, profiles and modelboard durably (free forever)
- lets EVERY player write over plain HTTPS -> works under any NAT / CGNAT
- keys the leaderboard by GitHub username -> no more "3 of me" duplicates
- keeps the WebRTC mesh for low-latency realtime when it can connect
- gives the host a single durable gist backup call every 10 min (optional)

## Deploy (once, free)

    cd relay
    npm install
    npx wrangler login          # one-click browser OAuth
    npx wrangler deploy

The printed `https://pixel-office-relay.<subdomain>.workers.dev` URL is the
RELAY URL. Add it to the office config:

    hermes config set plugins.entries.pixel-office.relay_url https://pixel-office-relay.<subdomain>.workers.dev

or set the env var `PIXEL_OFFICE_RELAY` to the same URL.

## Optional: durable 10-min gist backup (host-appointed, one call)

    npx wrangler secret put GITHUB_TOKEN
    npx wrangler secret put GITHUB_GIST_ID

If set, the Durable Object writes the WHOLE merged board to the gist every
10 minutes (one PATCH per interval), giving an offline-readable snapshot.
Without them, the Durable Object storage is still durable on its own.

## Local smoke test

    node relay/test_relay.js

Runs the Durable Object logic against an in-memory storage mock and asserts:
score upsert + per-username dedup + board ranking + chat append.

# Vault website + Ask Vault — deployment copy

Public website plus the Ask Vault server, in curated mode. No AI provider, no API key,
no Vault storage cluster and no third-party packages. Needs Python 3.11+ only.

```
deploy/
├── DEPLOY.md
├── assistant/          server-side only (never served to browsers)
│   ├── __init__.py
│   ├── server.py       static website + POST /api/ask + GET /api/ask/status
│   ├── engine.py       guards, section context, curated answers
│   ├── knowledge.py    the knowledge base answers come from
│   └── provider.py     optional AI provider (unused: curated mode)
└── website/            the only directory served to browsers
    ├── index.html  site.css  site.js  storage-map.js
    └── ask.js  ask.css
```

## Start

From inside `deploy/`:

```bash
ASK_VAULT_PROVIDER=none python3 assistant/server.py --host 0.0.0.0 --port "${PORT:-8095}" --console ''
```

- `ASK_VAULT_PROVIDER=none` forces curated mode, even if the host has AI variables set.
- `--console ''` disables live-data lookups. Live questions get "I can't reach the Live
  Console right now … I won't guess". The website's "Live system" section says the
  Live Console isn't hosted on this site and no live data is available.
- The website never links to, embeds or probes a Live Console: `PUBLIC_CONSOLE_URL` in
  `website/site.js` is empty, and the page says the console is a local demo only. Only set
  it to a real, publicly reachable console, and pass the same URL to `--console`.
- The server speaks plain HTTP. Put it behind a reverse proxy or load balancer that
  terminates HTTPS.
- Check it is up: `GET /api/ask/status` should return `"mode": "curated"`.

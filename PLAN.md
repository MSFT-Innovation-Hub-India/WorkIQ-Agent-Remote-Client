# Plan: WorkIQ Teams Relay Bot (M365 Agents SDK)

## TL;DR

Build a Microsoft 365 Agents SDK bot app that relays messages between Teams users and their personal WorkIQ desktop agents via Azure Managed Redis. The bot receives messages from Teams, pushes them to the user's Redis inbox stream, polls the outbox stream for responses, and delivers them back to Teams via proactive messaging. Deployed as an Azure Container App with managed identity; packaged as a Teams app ZIP uploaded to the CORP tenant.

## Architecture

```
Teams User (CORP Tenant) → Azure Bot Service (FDPO Tenant) → Container App (FDPO)
                                                                    ↓
                                                           Azure Managed Redis (FDPO)
                                                           inbox:{email} / outbox:{email}
                                                                    ↓
                                                           WorkIQ Desktop Agent (Win11)
```

**Two tenants:**
- **FDPO Tenant** — owns Bot Service, App Registration, Container App, Redis, all Azure resources
- **CORP Tenant** — where Teams users live. Bot app ZIP is sideloaded/uploaded to Teams here

## Multi-Tenant Auth Model

- `TENANT_ID` = FDPO tenant (Azure subscription, App Registration)
- `HOST_TENANT_ID` = CORP tenant (Teams users)
- Bot Service App Registration is **multi-tenant** type
- Inbound: JWT tokens from Bot Framework validated by M365 Agents SDK (App ID as audience)
- Outbound: App ID + Client Secret to send proactive replies via Bot Connector
- Redis: Managed identity (`DefaultAzureCredential`) authenticates; user email is only the stream key

## Redis Stream Schema (must match desktop agent exactly)

- **Inbox** `workiq:inbox:{email}`: fields `sender`, `text`, `ts`, `msg_id`
- **Outbox** `workiq:outbox:{email}`: fields `task_id`, `status`, `text`, `ts`, `in_reply_to`
- **Agent presence** `workiq:agents:{email}`: JSON with `{name, email, started_at, version}` + TTL

---

## Implementation Steps

### Phase 1 — Project Scaffolding

**Step 1**: Project structure:
```
workiq-agent-remote-client/
├── app.py                  # Entry point (thin wrapper)
├── agent_sdk.py            # M365 Agents SDK AgentApplication, message handler
├── start_server.py         # aiohttp hosting bootstrap + /health endpoint
├── config.py               # Configuration from env vars
├── redis_relay.py          # Redis connection, inbox push, outbox poller, proactive delivery
├── Dockerfile              # Container image (Python 3.12-slim)
├── requirements.txt
├── .env.example
├── .gitignore
└── appPackage/
    ├── manifest.json       # Teams bot manifest
    ├── color.png
    └── outline.png
```

**Step 2**: `requirements.txt` — M365 Agents SDK packages, `azure-identity`, `redis`, `redis-entraid`, `python-dotenv`, `aiohttp`

**Step 3**: `config.py` — load `TENANT_ID`, `HOST_TENANT_ID`, `CLIENT_ID`, `CLIENT_SECRET`, `AZ_REDIS_CACHE_ENDPOINT`, `PORT`

### Phase 2 — M365 Agents SDK Bot Core

**Step 4**: `start_server.py` — aiohttp app with `jwt_authorization_middleware`, `POST /api/messages`, `GET /health`. Pattern from HUB-TA-Agent-v2.

**Step 5**: `agent_sdk.py`:
- Initialize `AgentApplication[TurnState]` with `MsalConnectionManager`, `CloudAdapter`, `Authorization`, `MemoryStorage`
- Multi-tenant auth: authorize both `TENANT_ID` and `HOST_TENANT_ID`, reject unknown tenants
- Extract user email via Teams member info API
- Store `ConversationReference` on every inbound message for proactive messaging

**Step 6**: Message flow in `on_message()`:
1. Extract user email
2. Check agent online via `GET workiq:agents:{email}`
3. `XADD` to `workiq:inbox:{email}`
4. Reply immediately: "Your request has been submitted..."
5. Outbox poller delivers response async

**Step 7**: `app.py` — thin wrapper calling `agent_sdk.main()`

### Phase 3 — Redis Relay + Proactive Messaging

**Step 8**: `redis_relay.py` — `RedisRelay` class:
- Connection: `redis-entraid` + `DefaultAzureCredential` + `RedisCluster` (SSL)
- `push_to_inbox()`, `is_agent_online()`
- Outbox poller thread: `XREAD` per active user, deliver via `adapter.continue_conversation()`
- Active user tracking with inactivity timeout

**Step 9**: Wire into `agent_sdk.py` startup

**Step 10**: Proactive delivery — `adapter.continue_conversation(ref, callback, app_id)`

### Phase 4 — Containerization & Deployment

**Step 11**: `Dockerfile` — `python:3.12-slim`, non-root user, `EXPOSE 3978`

**Step 12**: `.env.example` with all required vars

**Step 13**: `appPackage/manifest.json` — Teams bot manifest, personal scope

### Phase 5 — Error Handling

**Step 14**: Auto-reconnect on Redis loss, agent offline detection, conversation ref refresh

---

## Key Decisions

| Decision | Rationale |
|----------|-----------|
| Proactive messaging (not sync) | Desktop agent may take minutes; must ack immediately |
| In-memory conversation refs | Single Container App replica; add Redis persistence if scaled |
| `redis-entraid` + `RedisCluster` | Matches desktop agent's connection pattern |
| User email via TeamsInfo | `from_property.id` is opaque in Teams; need actual email for Redis key |
| Single outbox poller thread | Polls all active users; scales better than thread-per-user |
| Personal scope only (1:1 chat) | No group/channel complexity for v1 |

## Verification

1. Local dev: `python app.py` + Bot Framework Emulator → Redis inbox populated
2. Proactive: send message, manually XADD to outbox → bot delivers back
3. End-to-end: Container App → Bot Service → Teams manifest → full round-trip
4. Agent offline: stop desktop agent → "not running" message
5. Multi-message: 2 rapid messages → 2 separate correlated responses

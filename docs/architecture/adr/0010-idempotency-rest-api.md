## ADR-0010: REST API Idempotency via X-Idempotency-Key

**Status:** Accepted

**Date:** 2026-03-13

**Context:**

The `order-service` exposes `POST /api/v1/orders`, a non-idempotent HTTP endpoint.

Clients may retry the request after a timeout without knowing whether the first
attempt was processed.

Without a deduplication mechanism, each retry creates a new
order — leading to duplicate orders for the same intent.

**Decision:**

Accept an `X-Idempotency-Key` header on all non-idempotent REST endpoints. The server
stores a mapping of `(idempotency_key, payload_hash → response)` in PostgreSQL and
returns the cached response on duplicate requests without reprocessing.

The `payload_hash` (SHA-256 of the request body) is stored alongside the key as a
safety guard. If a client reuses the same key with a different payload — indicating a
client bug — the server rejects the request with `422 Unprocessable Entity` rather
than silently returning a mismatched cached response.

```
POST /api/v1/orders
X-Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000

→ first request:         process, store (key + hash → response), return 200
→ retry, same payload:   key + hash match, return cached response — no order created
→ retry, diff payload:   key found but hash mismatch, return 422
```

Schema:

```sql
CREATE TABLE idempotency_keys (
  idempotency_key  VARCHAR PRIMARY KEY,
  payload_hash     VARCHAR NOT NULL,
  response         TEXT NOT NULL,
  created_at       TIMESTAMP NOT NULL
);
```

Note: key expiry and cleanup are deferred to a future phase. Keys persist indefinitely
for now — acceptable given the current request volume.

**Affected services:** `order-service` (`POST /api/v1/orders`)

✅ **Pros:**
- Standard industry pattern (used by Stripe, PayPal, and others)
- Client controls deduplication scope via key choice (UUID per intent)
- Response is deterministic — client receives the exact same payload on retry
- `payload_hash` catches key-reuse bugs early with a clear error instead of silent wrong behavior
- No additional infrastructure — reuses existing PostgreSQL

❌ **Cons:**
- Requires clients to generate and send a key (API contract change)
- Adds a DB lookup on every non-idempotent request
- Storage grows indefinitely until cleanup is implemented (deferred)
- Does not cover Kafka-layer duplicates — this is a complementary layer, not a replacement

**Alternatives Considered:**

- **Redis for key storage:** natural fit with built-in TTL, but introduces a new
  infrastructure dependency. PostgreSQL is sufficient for current request volume.
- **Fingerprint-based deduplication only** (hash of request body as the key): fragile —
  two different clients may legitimately send identical payloads for different intents.
  The hash is stored here as a guard, not as the primary deduplication key.

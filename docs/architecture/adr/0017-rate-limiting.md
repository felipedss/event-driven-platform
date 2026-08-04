## ADR-0017: Rate Limiting Strategy — Token Bucket (In-Memory → Redis)

**Status:** Proposed

## Context

The order service exposes REST endpoints that are publicly reachable. Without a rate limit, a single client can:

- Saturate the service thread pool and starve other clients
- Flood the Kafka topic `order.created` beyond what the saga orchestrator can process
- Trigger unbounded PostgreSQL writes at memory-allocation speed

This risk is highest at `POST /api/v1/orders` — each request creates a DB row, an in-flight Kafka message, and a downstream saga. A burst of 1,000 requests from one IP costs the same compute as 1,000 legitimate orders spread across many clients.

The platform currently runs as a single instance in a local dev/educational environment. Any rate-limiting solution must be simple enough to understand and run without extra infrastructure now, while having a clear upgrade path for the moment multiple instances are deployed behind a load balancer.

---

## Decision

### Phase 1 — Token Bucket, In-Memory (current)

Implement a **token bucket** rate limiter as a Spring `HandlerInterceptor`, backed by a `ConcurrentHashMap<String, TokenBucket>` keyed by client IP.

**Why token bucket over other algorithms:**

- Allows controlled bursts (up to `capacity` tokens) while enforcing a sustained throughput ceiling (`refillRatePerSecond`)
- Refill is lazy — calculated on the next incoming request from elapsed nanoseconds. No background thread, no scheduler, no timer
- The math is a one-liner: `tokens = min(capacity, tokens + elapsed × rate)`

**Configuration (`application.yaml`):**

```yaml
rate-limiter:
  enabled: true
  capacity: 10              # max burst per IP
  refill-rate-per-second: 2.0  # sustained: 2 req/sec per IP
```

**Response on limit exceeded:**

```
HTTP/1.1 429 Too Many Requests
Content-Type: application/json

{"error":"Too Many Requests","message":"Rate limit exceeded. Please slow down."}
```

**IP extraction** checks `X-Forwarded-For` first (proxy-aware), falls back to `remoteAddr`. When `X-Forwarded-For` carries a chain (`"203.0.113.5, 10.0.0.1"`), only the first IP — the original client — is used.

**Registration:** `WebConfig` registers the interceptor on `/api/**` via `WebMvcConfigurer.addInterceptors()`. Setting `enabled: false` in config bypasses all logic with zero overhead.

---

### Phase 2 — Token Bucket, Redis-Backed (planned)

When the service is horizontally scaled, each instance has its own independent `ConcurrentHashMap`. With N instances, a client can burst N × `capacity` requests before seeing a 429 — the limit is not enforced globally.

The fix: move bucket state into **Redis**, shared by all instances.

**Implementation approach — Lua script (atomic):**

Redis executes Lua scripts atomically. The entire token-check-and-consume operation runs as a single Redis command, eliminating the TOCTOU race that would exist with a `GET` → `SET` sequence.

```lua
-- KEYS[1] = "rate:<ip>"
-- ARGV[1] = capacity, ARGV[2] = refillRate, ARGV[3] = now (epoch ms)
local data     = redis.call("HMGET", KEYS[1], "tokens", "last_refill")
local tokens   = tonumber(data[1]) or tonumber(ARGV[1])
local last     = tonumber(data[2]) or tonumber(ARGV[3])
local elapsed  = (tonumber(ARGV[3]) - last) / 1000.0
tokens         = math.min(tonumber(ARGV[1]), tokens + elapsed * tonumber(ARGV[2]))

if tokens >= 1.0 then
  tokens = tokens - 1.0
  redis.call("HMSET",  KEYS[1], "tokens", tokens, "last_refill", ARGV[3])
  redis.call("EXPIRE", KEYS[1], 3600)
  return 1
else
  return 0
end
```

**Java change surface (interceptor only):**

```java
// Before (Phase 1)
private final ConcurrentHashMap<String, TokenBucket> buckets = new ConcurrentHashMap<>();

TokenBucket bucket = buckets.computeIfAbsent(ip,
    k -> new TokenBucket(properties.capacity(), properties.refillRatePerSecond()));
boolean allowed = bucket.tryConsume();

// After (Phase 2)
private final RedisScript<Long> rateLimitScript;
private final RedisTemplate<String, Long> redisTemplate;

Long allowed = redisTemplate.execute(rateLimitScript,
    List.of("rate:" + ip),
    properties.capacity(), properties.refillRatePerSecond(), Instant.now().toEpochMilli());
```

`WebConfig`, `RateLimiterProperties`, and the 429 response path are unchanged.

**New infrastructure dependencies:**

- `spring-boot-starter-data-redis` (brings Lettuce)
- Redis instance — can reuse the existing Docker Compose stack

---

## Known Limitations of Phase 1

| Limitation | Impact | Phase 2 fix |
|---|---|---|
| State is per-instance | N instances → N × capacity effective burst | Redis shared state |
| `ConcurrentHashMap` never evicts | Memory grows with unique IPs seen; leaks under IP spoofing | Redis TTL via `EXPIRE` |
| IP-based identity | Trivially bypassed by rotating IPs | Authenticate first, rate-limit by user ID |
| All endpoints share one limit | `POST /orders` (expensive) and `GET /orders/:id` (cheap) have the same cap | Per-route limits via interceptor path pattern or separate configs |
| No client feedback headers | Clients cannot back off intelligently | Add `X-RateLimit-Remaining`, `X-RateLimit-Limit`, `Retry-After` headers |

---

## Alternatives Considered

### Fixed Window Counter

Count requests per IP in a time window (e.g., 10 req / 1 min). Reset the counter at the window boundary.

**Rejected.**

Suffers from the boundary burst problem: a client sending 10 requests in the last second of window N and 10 in the first second of window N+1 passes 20 requests in a 2-second span — double the intended limit. Token bucket handles this naturally because capacity is the ceiling regardless of when in time the burst occurs.

---

### Sliding Window Log

Store a timestamp for every request and count how many fall within the last N seconds.

**Rejected for now.**

Precise and boundary-safe, but stores one entry per request per IP. Under load, memory cost is O(requests) not O(IPs). Not appropriate for an in-memory implementation. Could be revisited as a Redis sorted set (timestamps as scores), but the token bucket Lua script achieves equivalent precision with O(1) storage per IP.

---

### Leaky Bucket

Requests enter a queue and are processed at a fixed rate. Excess requests are dropped immediately.

**Rejected.**

Leaky bucket enforces a strict, smooth output rate with no burst at all. This is the right model for outbound request shaping (e.g., calling a third-party API at 5 req/sec). For an inbound REST API, the token bucket's controlled burst capacity is a better user experience — a client that has been idle for a few seconds should be able to send multiple requests quickly.

---

### Resilience4j `RateLimiter`

Resilience4j offers a `RateLimiter` decorator that wraps methods with a thread-permit model.

**Deferred.**

Resilience4j's rate limiter counts concurrent threads (permits), not requests-per-unit-time in the token bucket sense. It is better suited to outbound client calls than inbound HTTP protection. Could be combined with the current approach for outbound calls to the inventory service.

---

### Spring Cloud Gateway

Delegate rate limiting to a gateway tier using `spring-cloud-gateway` with its built-in Redis rate limiter.

**Deferred.**

This is the correct long-term architecture for a multi-service platform — a single gateway enforces limits before requests reach any service. However, it requires introducing a new process and changes how all services are reached. Out of scope until the platform graduates from local development.

---

## Future Improvements

Beyond Phase 2, the following are worth considering in order of impact:

1. **Rate-limit by authenticated user, not IP** — once auth is introduced, an authenticated user ID is a more reliable and fair identity than a client IP (which may be shared by NAT, proxies, or VPNs).
2. **Per-endpoint limits** — `POST /orders` triggers a saga; `GET /orders/:id` hits a DB read cache. They warrant different ceilings. Implement as multiple `RateLimiterProperties` profiles keyed by path pattern.
3. **Response headers** — return `X-RateLimit-Limit`, `X-RateLimit-Remaining`, and `Retry-After: <seconds>` on every response so clients can implement cooperative back-off without guessing.
4. **Observability** — emit a `rate_limiter.rejected` metric (Micrometer counter, tagged by endpoint and IP range) so the team can see which clients are hitting limits and tune capacity/rate accordingly.
5. **Allowlist for internal callers** — saga orchestrator callbacks and health checks should bypass rate limiting entirely, checked before the bucket lookup.

---

## Consequences

### Positive

- Protects the service and Kafka topic from accidental or deliberate floods with a single interceptor
- No new infrastructure in Phase 1 — runs entirely in-process
- `enabled: false` provides an instant kill switch without redeployment
- Clean upgrade path: Redis swap changes only the interceptor's bucket lookup, everything else stays the same

### Negative

- In-memory state means the limit is not global across instances (acceptable in Phase 1, fixed in Phase 2)
- `ConcurrentHashMap` grows unbounded — needs Redis TTL or an eviction policy before going to production with real traffic
- IP-based limits are easily bypassed and can unfairly penalize shared NAT addresses

---

## Related ADRs

- ADR-0006 Phased Saga Implementation
- ADR-0010 Idempotency — REST API

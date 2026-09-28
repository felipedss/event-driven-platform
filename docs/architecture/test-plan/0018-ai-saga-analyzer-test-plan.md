# Test Plan: AI Saga Analyzer

**Implements:** [ADR-0018 — AI-Assisted Saga Analysis via Prompt Chaining](../adr/0018-ai-assisted-saga-analysis-prompt-chaining.md)

**Verifies:** [Implementation Plan — AI Saga Analyzer](../implementation-plans/0018-ai-saga-analyzer-implementation-plan.md) (Phase 1 + Phase 2 MVP)

**Target service:** `event-driven-saga-orchestrator`

This is a manual verification plan. The implementation's automated test suite (JUnit/Mockito,
run via `mvn test`) already covers the stage-level logic — the fabrication guard, the
`SagaStatus` → `ExecutionAssessment` mapping table, the `PAYMENT_FAILED`/`COMPENSATING`
regression, chain handoff, structural isolation, and controller status codes — in isolation. This
plan exercises the wired-up service end-to-end instead of retesting logic already covered.

Saga rows are seeded directly in Postgres rather than by running the full 4-service order flow,
so every status in the mapping table can be hit quickly without needing Payment/Inventory
services running.

---

## 0. Prerequisites (once)

```bash
cd ~/Workspace/event-driven-simulator/infrastructure/docker-compose
docker compose up -d          # Kafka + Postgres
export OPENAI_API_KEY=sk-...  # real key, for the happy-path tests

cd ~/Workspace/event-driven-saga-orchestrator
mvn spring-boot:run
```

Confirm the app starts cleanly with no bean-wiring errors — that alone proves
`SagaAnalyzerService`'s `@Qualifier("sagaAnalysisWorkflow")` resolves without ambiguity against
`SagaAnalysisPromptChain`'s `@Component("sagaAnalysisWorkflow")`.

## 1. Seed one saga per status

```bash
psql -h localhost -p 5432 -U postgres -d orchestrator_db <<'SQL'
INSERT INTO order_saga (saga_id, order_id, product_id, quantity, status, cancellation_reason, created_at, updated_at) VALUES
('11111111-1111-1111-1111-111111111111','order-started','prod-A',1,'STARTED',NULL,now(),now()),
('22222222-2222-2222-2222-222222222222','order-payment-pending','prod-A',2,'PAYMENT_PENDING',NULL,now(),now()),
('33333333-3333-3333-3333-333333333333','order-compensating','prod-B',1,'COMPENSATING','Insufficient funds',now(),now()),
('44444444-4444-4444-4444-444444444444','order-completed','prod-A',3,'COMPLETED',NULL,now(),now()),
('55555555-5555-5555-5555-555555555555','order-cancelled','prod-C',1,'CANCELLED','Out of stock',now(),now());
SQL
```

## 2. Call the endpoint for each and check `executionAssessment`

```bash
for id in 11111111-1111-1111-1111-111111111111 \
          22222222-2222-2222-2222-222222222222 \
          33333333-3333-3333-3333-333333333333 \
          44444444-4444-4444-4444-444444444444 \
          55555555-5555-5555-5555-555555555555; do
  echo "== $id"
  curl -s -X POST localhost:8080/ai/sagas/$id/analyze | jq '{complete, currentState: .executionAnalysis.currentState, assessment: .failureClassification.executionAssessment, limitations: .executionAnalysis.dataCompleteness.limitations}'
done
```

Expected: `complete: true` for all five, and:

| saga | expected `executionAssessment` |
|---|---|
| STARTED | `IN_PROGRESS` |
| PAYMENT_PENDING | `IN_PROGRESS` |
| COMPENSATING | `COMPENSATING` (not `TERMINAL_FAILURE` — this is the regression the implementation plan fixed) |
| COMPLETED | `TERMINAL_SUCCESS` |
| CANCELLED | `TERMINAL_FAILURE` |

Every response's `dataCompleteness.limitations` should be non-empty — the fabrication guard.

## 3. Not-found path

```bash
curl -i -X POST localhost:8080/ai/sagas/99999999-9999-9999-9999-999999999999/analyze
```

Expect `404` with `{"message":"Saga not found: ..."}`.

## 4. Degraded path (no crash without a working LLM)

```bash
unset OPENAI_API_KEY
# restart the app, then:
curl -s -X POST localhost:8080/ai/sagas/11111111-1111-1111-1111-111111111111/analyze | jq
```

Expect HTTP `200`, `complete: false`, and a `stageSummaries` entry with `status: "PROVIDER_ERROR"`
— never a 500 or a hang.

## 5. Isolation from real saga processing

While steps 2–4 run, tail the orchestrator's logs (or watch Kafka UI at `localhost:8090`).
Confirm no `SagaService`/`KafkaProducerService` log lines (`Published ...Command`, `Saga
completed`, etc.) are triggered by the `/ai/sagas/.../analyze` calls — only by real Kafka events.
Optionally, run the real order flow concurrently (`order-service` → Kafka → orchestrator) and
confirm it completes normally while the AI endpoint is being called.

## 6. Clean up the seeded rows

```bash
psql -h localhost -p 5432 -U postgres -d orchestrator_db -c \
  "DELETE FROM order_saga WHERE order_id LIKE 'order-%';"
```

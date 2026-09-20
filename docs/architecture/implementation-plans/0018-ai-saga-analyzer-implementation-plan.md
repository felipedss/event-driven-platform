# Implementation Plan: AI Saga Analyzer

**Implements:** [ADR-0018 — AI-Assisted Saga Analysis via Prompt Chaining](../adr/0018-ai-assisted-saga-analysis-prompt-chaining.md)

**Target service:** `event-driven-saga-orchestrator`

**Status:** Not started. This document records the agreed implementation approach; no code has been written yet.

---

## Context

ADR-0018 documents a fully-separate, advisory-only "AI Saga Analyzer" for `event-driven-saga-orchestrator`: a 4-stage prompt chain (Timeline Reconstruction → Failure Classification → Recovery Analysis → Operator Explanation) exposed via `POST /ai/sagas/{sagaId}/analyze`, that helps engineers understand failed/stuck sagas without ever touching saga state.

Two research passes over the codebase confirmed what this has to be built against:

- `OrderSaga` (the saga entity) has no history/audit table — only current status is persisted, and `createdAt`/`updatedAt` are declared but never actually set.
- No REST controller exists yet in this service.
- No DLQ read path exists — DLQ topics exist (production side only, via `DeadLetterPublishingRecoverer`).
- No retry-count is tracked per saga (only a static `kafka.dlq.max-retries: 3` config value).
- No LLM/AI dependency exists anywhere in the repo.
- `RestClient` (order-service's `InventoryClient` pattern) and `@ConfigurationProperties` records are the established conventions to reuse; `spring-boot-starter-web` + `spring-kafka` already provide everything needed — **zero new Maven dependencies**.

**Explicit decision governing this plan:** the first version must **not** introduce a dedicated saga-history table. The analyzer consumes only what's already available today — current persisted `OrderSaga` fields, DLQ topics (already-existing infrastructure, just never read before), and configured retry settings — and when that's insufficient to reconstruct a full step-by-step timeline, it must say so explicitly in its output rather than inventing history.

Outcome of this work: a working, isolated, read-only AI analysis endpoint that degrades gracefully under any LLM/DLQ failure and never risks saga/transaction processing.

LLM provider chosen for the first implementation: **OpenAI API**, called directly over HTTP (no SDK dependency).

---

## Step 0 — Amend ADR-0018

Before implementation begins, add a subsection to ADR-0018's `### Scope of Analysis` section, immediately after the existing two paragraphs:

```markdown
**Data sources for the first version:**

The first implementation must not depend on introducing a dedicated saga history/audit table. Timeline Reconstruction consumes only information already available in the running system: the saga's current persisted state, domain events that are already stored or independently retrievable (e.g., the existing dead-letter-queue topics), failure metadata already captured on the saga record, and retry information where it is actually tracked. Where the available data is insufficient to reconstruct a full step-by-step execution timeline, the analyzer must explicitly state that limitation in its structured output rather than inferring or inventing steps that were never observed.
```

No other section of the ADR changes.

---

## Step 1 — Fix `OrderSaga` timestamps (small, safe prerequisite)

File: `event-driven-saga-orchestrator/src/main/java/com/platform/saga/orchestrator/model/OrderSaga.java`

Add lifecycle callbacks so the already-declared `createdAt`/`updatedAt` fields actually get populated (they exist on the entity today but are never set anywhere):

```java
@PrePersist
protected void onCreate() {
  Instant now = Instant.now();
  createdAt = now;
  updatedAt = now;
}

@PreUpdate
protected void onUpdate() {
  updatedAt = Instant.now();
}
```

Add `import jakarta.persistence.PrePersist;` and `import jakarta.persistence.PreUpdate;`. No changes needed in `SagaService` — Hibernate invokes these automatically on the existing `sagaRepository.save(...)` calls. `SagaServiceTest` mocks the repository so it's unaffected.

## Step 2 — Enable `@ConfigurationProperties` scanning

File: `event-driven-saga-orchestrator/src/main/java/com/platform/saga/orchestrator/SagaOrchestratorApplication.java`

Add `@ConfigurationPropertiesScan` (this annotation is present on `event-driven-order-service`'s main class but missing here — needed for the new `@ConfigurationProperties` records below to be picked up):

```java
@SpringBootApplication
@EnableKafka
@ConfigurationPropertiesScan
public class SagaOrchestratorApplication { ... }
```

## Step 3 — New package skeleton

All new code lives under `com.platform.saga.orchestrator.ai`:

```
ai/
├── controller/         AiSagaAnalyzerController, AiAnalyzerExceptionHandler, dto/ (SagaAnalysisResponse, StageSummary, AiAnalysisErrorResponse)
├── service/             SagaAnalyzerService, PromptChainOrchestrator, ChainResult
├── chain/               PromptChainStage (interface), StageResult, StageName, StageStatus, StageTelemetry
│   ├── timeline/        TimelineReconstructionStage, ...Input, ...Output, DataCompleteness
│   ├── classification/  FailureClassificationStage, ...Input, ...Output, FailureCategory, ExecutionAssessment
│   ├── recovery/        RecoveryAnalysisStage, ...Input, ...Output, RecoveryRecommendationType
│   └── explanation/     OperatorExplanationStage, ...Input, ...Output
├── client/              OpenAiClient, OpenAiClientProperties, OpenAiCallException, dto/ (chat request/response records)
├── dlq/                 DlqLookupService, DlqLookupProperties, DlqLookupResult, DlqMatch
├── model/               SagaSnapshot
├── config/              OpenAiClientConfig
└── exception/           SagaNotFoundException
```

Test mirror under `src/test/java/com/platform/saga/orchestrator/ai/...` (see Step 11).

The existing `core/` package (`Saga`, `SagaContext`, `SagaStep` — unused Phase-2 placeholders) is untouched.

## Step 4 — Shared data model

`ai/model/SagaSnapshot.java` — decouples the chain from the JPA entity (never pass `OrderSaga` directly into a prompt):
```java
public record SagaSnapshot(UUID sagaId, String orderId, String productId, int quantity,
    String status, String cancellationReason, Instant createdAt, Instant updatedAt) {}
```

`ai/dlq/DlqLookupResult.java` / `DlqMatch.java`:
```java
public record DlqLookupResult(boolean lookupAttempted, boolean lookupSucceeded,
    boolean matchFound, List<DlqMatch> matches, String note) {}
public record DlqMatch(String topic, int partition, long offset, Instant timestamp, String rawValuePreview) {}
```

## Step 5 — DLQ best-effort lookup (new capability, read-only)

`ai/dlq/DlqLookupProperties.java`:
```java
@ConfigurationProperties(prefix = "ai.dlq-lookup")
public record DlqLookupProperties(boolean enabled, Duration pollTimeout, List<String> topics) {}
```

`ai/dlq/DlqLookupService.java` — a manually-constructed `KafkaConsumer<String,String>` (NOT the `@KafkaListener` container factory), scoped per-call:
- Fresh `group.id = "ai-saga-analyzer-dlq-lookup-" + UUID.randomUUID()` every call → can never join/rebalance with the real `order-orchestrator` consumer group.
- `enable.auto.commit = false` — pure read, no offset writes.
- `consumer.assign(...)` + `seekToBeginning(...)` over the 7 existing DLQ topics (exact names below), poll in a loop bounded by `poll-timeout` wall-clock deadline, `try-with-resources` for guaranteed `close()`.
- Match records whose value contains the target `orderId`.
- **Any exception (broker down, topic missing, timeout) is caught and turned into `DlqLookupResult(lookupSucceeded=false, ...)`** — never propagates, never blocks the analysis.

Exact DLQ topic names (confirmed from `KafkaTopicConfig.java`): `order.created.DLQ`, `order.payment.processed.DLQ`, `order.payment.failed.DLQ`, `order.inventory.reserved.DLQ`, `order.inventory.failed.DLQ`, `order.inventory.released.DLQ`, `order.inventory.release.failed.DLQ`.

## Step 6 — OpenAI client

`ai/client/OpenAiClientProperties.java`:
```java
@ConfigurationProperties(prefix = "ai.openai")
public record OpenAiClientProperties(String baseUrl, String apiKey, String model,
    Duration connectTimeout, Duration readTimeout, double temperature) {}
```

`ai/config/OpenAiClientConfig.java` — mirrors order-service's `ExternalClientConfig` exactly:
```java
@Configuration
@RequiredArgsConstructor
public class OpenAiClientConfig {
  private final OpenAiClientProperties properties;
  @Bean
  public RestClient openAiRestClient() {
    SimpleClientHttpRequestFactory factory = new SimpleClientHttpRequestFactory();
    factory.setConnectTimeout(properties.connectTimeout());
    factory.setReadTimeout(properties.readTimeout());
    return RestClient.builder().baseUrl(properties.baseUrl())
        .defaultHeader("Authorization", "Bearer " + properties.apiKey())
        .requestFactory(factory).build();
  }
}
```

`ai/client/dto/` — Chat Completions request/response records (`OpenAiChatMessage`, `OpenAiResponseFormat{"type":"json_object"}`, `OpenAiChatCompletionRequest`, `OpenAiChatCompletionResponse` w/ nested `Choice`/`Usage`), `OpenAiCompletionResult(rawContent, usage, latencyMs)`.

`ai/client/OpenAiClient.java` — `POST /chat/completions` via the `RestClient` bean, `response_format: json_object`, each stage's system prompt spells out its target record's exact fields/enum values in plain text. Broad `catch (Exception e)` around the whole call, wrapped as unchecked `OpenAiCallException` — **never propagates raw**. Parsing into each stage's record uses the auto-configured Spring `ObjectMapper` bean.

## Step 7 — The four chain stages

Shared chain types: `ai/chain/PromptChainStage<I,O>` (interface), `StageResult<O>` (success/failure factory methods, carries `StageName`, `StageStatus`, `StageTelemetry`), `StageStatus` enum (`SUCCESS, VALIDATION_FAILED, PROVIDER_ERROR, TIMEOUT, UNEXPECTED_ERROR`).

**Stage 1 — `ai/chain/timeline/`** (`TimelineReconstructionStage`):
- Input: `SagaSnapshot`, `DlqLookupResult`, `configuredMaxRetries`.
- Output: `currentState`, `completedSteps`, `failedStep` (nullable), `compensationStarted`, `narrativeSummary`, and **mandatory `DataCompleteness`**:
  ```java
  public record DataCompleteness(boolean onlyCurrentStatusSnapshotAvailable,
      boolean stepByStepEventHistoryAvailable, boolean dlqChecked, boolean dlqEntryFound,
      boolean retryCountKnown, List<String> limitations) {}
  ```
  This record is the concrete mechanism for the ADR-0018 amendment: `onlyCurrentStatusSnapshotAvailable` is always `true` and `stepByStepEventHistoryAvailable`/`retryCountKnown` are always `false` in v1 — the stage's validator rejects a model response that claims otherwise (fabrication guard).

**Stage 2 — `ai/chain/classification/`** (`FailureClassificationStage`):
- `FailureCategory` enum exactly per ADR-0018: `TRANSIENT_INFRASTRUCTURE_FAILURE, PAYMENT_PROVIDER_FAILURE, INVENTORY_FAILURE, TIMEOUT, INVALID_STATE, COMPENSATION_FAILURE, UNKNOWN`.
- `ExecutionAssessment` enum: `IN_PROGRESS, STUCK, TERMINAL_SUCCESS, TERMINAL_FAILURE` — implements ADR-0018's rule that "stuck" is never inferred from elapsed time alone; the system prompt instructs the model to only choose `STUCK` when `timeline.failedStep != null` or a DLQ entry was found.
- Output adds `confidence` (0.0–1.0) and `reasoning`.

**Stage 3 — `ai/chain/recovery/`** (`RecoveryAnalysisStage`):
- `RecoveryRecommendationType` enum: `RETRY_STEP, MANUAL_COMPENSATION, MONITOR, ESCALATE_TO_PAYMENT_PROVIDER, ESCALATE_TO_INVENTORY_TEAM, NO_ACTION_REQUIRED, INSUFFICIENT_DATA`.
- Output includes `advisoryOnly` (boolean) — the stage's validator force-corrects this to `true` (with a WARN log) if the model ever returns `false`, since nothing in this codebase would ever execute the recommendation regardless.

**Stage 4 — `ai/chain/explanation/`** (`OperatorExplanationStage`):
- Input: snapshot + all three prior outputs.
- Output: `summary`, `detailedExplanation`, `keyFacts`, `suggestedNextStep`, and `limitations` (propagated from Stage 1's `DataCompleteness.limitations()` so caveats survive even if a caller only reads the final explanation).

**Every stage follows the identical error shape**: try/catch around the OpenAI call + JSON parse + manual field validation; every branch returns a `StageResult` (never throws). Malformed JSON → `VALIDATION_FAILED`. Missing required field (e.g. no `dataCompleteness`) → `VALIDATION_FAILED`. `OpenAiCallException` → `PROVIDER_ERROR`/`TIMEOUT`. Any other exception → `UNEXPECTED_ERROR`, logged, never rethrown.

## Step 8 — Orchestration + controller

`ai/service/PromptChainOrchestrator.java` — runs stages 1→4 strictly in order, short-circuits into `ChainResult.partial(...)` on the first stage failure, wraps each stage call in one more `catch (Throwable)` guard, logs per-stage telemetry (`analysisId`, stage, model, provider, latencyMs, tokens, validation result) satisfying ADR-0018's observability list via structured `@Slf4j` logging (no new metrics library).

`ai/service/SagaAnalyzerService.java` — depends **only** on `SagaRepository` (via inherited `findById(UUID)` — `sagaId` is already the `@Id`, no new repository method needed) + the new `DlqLookupService` + `PromptChainOrchestrator`. **Never injects `SagaService` or `KafkaProducerService`** — structurally cannot mutate saga state or publish Kafka messages. Generates `analysisId = UUID.randomUUID()`, builds `SagaSnapshot` from the entity, calls DLQ lookup, runs the chain, returns `SagaAnalysisResponse`. Throws `SagaNotFoundException` (new, in `ai/exception/`) only for an unknown `sagaId`.

`ai/controller/AiSagaAnalyzerController.java`:
```java
@RestController
@RequestMapping("/ai/sagas")
@RequiredArgsConstructor
public class AiSagaAnalyzerController {
  private final SagaAnalyzerService sagaAnalyzerService;
  @PostMapping("/{sagaId}/analyze")
  public ResponseEntity<SagaAnalysisResponse> analyze(@PathVariable UUID sagaId) {
    return ResponseEntity.ok(sagaAnalyzerService.analyze(sagaId));
  }
}
```

`ai/controller/AiAnalyzerExceptionHandler.java` — `@RestControllerAdvice(basePackageClasses = AiSagaAnalyzerController.class)` (scoped only to this feature): `SagaNotFoundException` → 404; any other `Exception` → 502 with a generic `{status, message}` body, logged server-side — **never a raw stack trace to the client**. Note: AI/DLQ failures never reach this handler at all — they're already absorbed into a `ChainResult` and returned as a 200 with `complete=false`; this handler is a last-resort safety net for genuine bugs.

## Step 9 — `application.yaml` additions

New top-level block in `event-driven-saga-orchestrator/src/main/resources/application.yaml`, following the existing `kafka.dlq.*`/`topics.*` convention (top-level sibling to `spring:`, kebab-case, `Duration`-string shorthand):

```yaml
# ---- AI SAGA ANALYZER ----
ai:
  openai:
    base-url: https://api.openai.com/v1
    api-key: ${OPENAI_API_KEY:}
    model: gpt-4o-mini
    connect-timeout: 5s
    read-timeout: 20s
    temperature: 0.2
  dlq-lookup:
    enabled: true
    poll-timeout: 2s
    topics:
      - order.created.DLQ
      - order.payment.processed.DLQ
      - order.payment.failed.DLQ
      - order.inventory.reserved.DLQ
      - order.inventory.failed.DLQ
      - order.inventory.released.DLQ
      - order.inventory.release.failed.DLQ
```

The API key follows the one existing precedent in this workspace for externalizing a third-party key (`price-monitor-service`'s `${FLIGHT_API_KEY:your-api-key-here}`) — never hardcoded. `configuredMaxRetries` reuses the **existing** `kafka.dlq.max-retries` key directly via `@Value`, no duplicate config added.

If `OPENAI_API_KEY` is unset, the first real call fails auth → caught as `OpenAiCallException` → stage 1 returns `PROVIDER_ERROR` → endpoint still returns `200` with `complete=false` and an explanatory stage summary, never a crash.

## Step 10 — Error/timeout handling summary (verifying the ADR's failure-isolation requirement)

| Layer | Guarantee |
|---|---|
| OpenAI HTTP call | Bounded by `connect-timeout`/`read-timeout`; every failure becomes unchecked `OpenAiCallException` |
| Each chain stage | try/catch around call+parse+validate; always returns a `StageResult`, never throws |
| Orchestrator | Extra `catch (Throwable)` guard per stage call; stops chain at first failure, returns partial result |
| DLQ lookup | try/catch + wall-clock deadline; failure degrades to `lookupSucceeded=false`, never blocks |
| Service | Only throws for unknown `sagaId`; everything AI/DLQ-related already absorbed |
| Controller | 200 (complete or degraded), 404 (not found), 502 (last-resort, generic body only) |
| Blast radius | `SagaAnalyzerService`/`PromptChainOrchestrator`/`DlqLookupService` have zero wiring into any `@KafkaListener`, `SagaService`, or `KafkaProducerService` — a failure here structurally cannot reach saga/transaction processing |

## Step 11 — Tests

Mirror `src/test/java/com/platform/saga/orchestrator/ai/...`, same JUnit 5 + Mockito + AssertJ style as `SagaServiceTest` (`@ExtendWith(MockitoExtension.class)`, `@Mock`/`@InjectMocks`, `ArgumentCaptor`, `ReflectionTestUtils`):

1. `OrderSaga` timestamp test — `ReflectionTestUtils.invokeMethod(saga, "onCreate"/"onUpdate")`, assert fields populate.
2. `OpenAiClientTest` — reuse `event-driven-order-service`'s `InventoryClientTest` pattern (`MockRestServiceServer`): happy path + 500/slow-response path, assert `OpenAiCallException` and nothing else leaks.
3. Per-stage tests (`TimelineReconstructionStageTest`, `FailureClassificationStageTest`, `RecoveryAnalysisStageTest`, `OperatorExplanationStageTest`) — `@Mock OpenAiClient` returning canned JSON: well-formed → `SUCCESS`; malformed/missing-field → `VALIDATION_FAILED`; `advisoryOnly:false` → coerced to `true`; client throws → `PROVIDER_ERROR`/`TIMEOUT`, no exception escapes.
4. `DlqLookupServiceTest` — `enabled:false` → immediate no-op result; unreachable broker + short timeout → returns within bound, `lookupSucceeded=false`, no throw. (Full round-trip against a real topic is a stretch goal requiring `spring-kafka-test`, not currently a dependency — explicitly deferred, not silently added.)
5. `PromptChainOrchestratorTest` — all stages mocked; verify short-circuit behavior (a stage-2 failure means stage 3/4 are `verify(..., never())`).
6. `SagaAnalyzerServiceTest` — verify only `SagaRepository.findById` is used; `SagaService`/`KafkaProducerService` are never injected (structural isolation proof).
7. `AiSagaAnalyzerControllerTest` — MockMvc standalone setup (mirroring order-service's `OrderControllerTest`) covering happy/degraded/not-found/unexpected-error paths.

Run `mvn spotless:apply` after each step, `mvn test` to keep the build green incrementally.

## Suggested build order

1. Step 0 (ADR amendment) → 2. Step 1 (timestamp fix) → 3. Step 2 (`@ConfigurationPropertiesScan`) → 4. Step 4 (`SagaSnapshot`, exception) → 5. Step 5 (DLQ lookup, independently testable) → 6. Step 6 (OpenAI client, independently testable) → 7. Step 7 stages one at a time (Timeline first, since 2–4 depend on its shape) → 8. Step 8 (orchestrator, then service, then controller) → 9. Step 9 (config) → 10. Tests alongside each step, not batched at the end.

## Verification

- `mvn spotless:check` and `mvn test` green in `event-driven-saga-orchestrator` after each increment.
- Manual smoke test: `docker compose up -d` (existing Kafka+Postgres stack), start the orchestrator, create a saga via the normal order flow, export a real `OPENAI_API_KEY`, then:
  ```
  curl -X POST localhost:8085/ai/sagas/{sagaId}/analyze
  ```
  Confirm a `200` with populated `dataCompleteness`/`limitations`, and confirm via logs/Kafka UI (`localhost:8090`) that normal saga event processing is completely unaffected while the analysis runs.
- Confirm graceful degradation by unsetting `OPENAI_API_KEY` (or pointing `ai.openai.base-url` at an invalid host) and re-running the curl — expect `200` with `complete=false`, not a 500 or hang.

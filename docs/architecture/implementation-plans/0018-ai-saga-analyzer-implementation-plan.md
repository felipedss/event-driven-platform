# Implementation Plan: AI Saga Analyzer

**Implements:** [ADR-0018 — AI-Assisted Saga Analysis via Prompt Chaining](../adr/0018-ai-assisted-saga-analysis-prompt-chaining.md)

**Target service:** `event-driven-saga-orchestrator`

**Status:** Not started. This document records the agreed implementation approach; no code has been written yet.

**Revision 4** — corrects `ExecutionAssessment` to match the actual `SagaStatus` state machine (adds `COMPENSATING` as its own non-terminal value; `INVENTORY_FAILED`/`PAYMENT_FAILED` no longer both flatten to `TERMINAL_FAILURE`) and makes `SagaAnalyzerService`'s constructor injection explicit instead of relying on Lombok to propagate `@Qualifier`. See "Changes in Revision 4" at the end. (Revision 3 narrowed `ExecutionAssessment` to only values the current system can actually support and removed `configuredRetryLimit` from the first two stages, and added explicit bean identity for the active `AiWorkflow` — see "Changes in Revision 3". Revision 2 narrowed the first milestone to 2 stages, removed DLQ scanning from the MVP, and restructured the package layout so Prompt Chaining is one interchangeable workflow implementation rather than the architecture's central abstraction — see "Changes from Revision 1 → 2".)

---

## Design principles

Two priorities govern every decision below:

1. **Keep the first milestone small and realistic** given what the repository actually has today (no history table, no DLQ read path, no retry tracking, no LLM dependency).
2. **Separate three concerns that must not collapse into one class:**
   ```text
   AI provider  ≠  AI workflow  ≠  Saga use case
   ```
   `SagaAnalyzerService` (the use case) calls an `AiWorkflow` (currently a prompt chain, later possibly routing/parallelization/tool use) which calls an `AiModelClient` (currently OpenAI, later possibly another provider). Each layer is replaceable without touching the others, and the public endpoint (`POST /ai/sagas/{sagaId}/analyze`) never changes shape as the internals evolve.

No speculative abstractions are introduced beyond what the 2-stage MVP already justifies — no routing/parallelization/tool-use packages, no generic agent framework, no schema-generation library.

---

## Context

Two research passes over the codebase confirmed what this has to be built against:

- `OrderSaga` (the saga entity) has no history/audit table — only current status is persisted, and `createdAt`/`updatedAt` are declared but never actually set.
- No REST controller exists yet in this service.
- No DLQ read path exists — DLQ topics exist (production side only, via `DeadLetterPublishingRecoverer`), but nothing reads them back.
- No retry-count is tracked per saga (only a static `kafka.dlq.max-retries: 3` config value, which is a *limit*, not a count of what actually happened).
- No LLM/AI dependency exists anywhere in the repo.
- `RestClient` (order-service's `InventoryClient` pattern) and `@ConfigurationProperties` records are the established conventions to reuse; `spring-boot-starter-web` + `spring-kafka` already provide everything needed — **zero new Maven dependencies**, including in this revision (OpenAI's Structured Outputs mode is just a request-body shape, not an SDK).

**Governing constraint, unchanged from the previous revision:** the analyzer must not depend on a dedicated saga-history table. It consumes only what's already available — current persisted `OrderSaga` fields and configuration — and when that's insufficient to reconstruct a full timeline, it says so explicitly rather than inventing history.

**New in this revision:** the first milestone is deliberately smaller than all four ADR-0018 stages, DLQ access is deferred entirely, and the code is structured so Prompt Chaining is *a* workflow, not *the* architecture.

---

## Package structure

```
ai/
├── provider/
│   ├── AiModelClient.java          interface — provider-neutral contract
│   ├── AiRequest.java              record — systemPrompt, userPrompt, outputJsonSchema, outputType
│   ├── AiModelResponse.java        record<T> — output, rawContent, tokens, latencyMs, model, provider
│   ├── AiModelException.java       unchecked, provider-neutral failure type
│   └── openai/
│       ├── OpenAiModelClient.java  implements AiModelClient
│       ├── OpenAiClientProperties.java
│       ├── config/OpenAiClientConfig.java
│       └── dto/                    OpenAI Chat Completions request/response records (never referenced outside this subpackage)
├── workflow/
│   ├── AiWorkflow.java             interface — generic contract: run(I input, UUID analysisId) -> WorkflowResult<O>
│   ├── WorkflowResult.java         record<O> — analysisId, complete, stageSummaries, output
│   └── promptchain/
│       ├── SagaAnalysisPromptChain.java   implements AiWorkflow<SagaSnapshot, SagaAnalysisResult> — the current (only) workflow
│       ├── SagaAnalysisResult.java        record — aggregates the chain's stage outputs
│       └── stage/
│           ├── PromptChainStage.java      interface — generic single-stage contract
│           ├── StageResult.java, StageName.java, StageStatus.java, StageTelemetry.java
│           ├── execution/
│           │   ├── ExecutionAnalysisStage.java
│           │   ├── ExecutionAnalysisInput.java
│           │   ├── ExecutionAnalysisOutput.java
│           │   └── DataCompleteness.java
│           └── classification/
│               ├── FailureClassificationStage.java
│               ├── FailureClassificationInput.java
│               ├── FailureClassificationOutput.java
│               ├── FailureCategory.java
│               └── ExecutionAssessment.java
├── model/
│   └── SagaSnapshot.java           shared, decouples the whole ai/ package from the JPA entity
├── service/
│   └── SagaAnalyzerService.java    the Saga use case — depends on AiWorkflow<SagaSnapshot, SagaAnalysisResult>, not on promptchain internals
├── exception/
│   └── SagaNotFoundException.java
└── controller/
    ├── AiSagaAnalyzerController.java
    ├── AiAnalyzerExceptionHandler.java
    └── dto/  (SagaAnalysisResponse, StageSummary, AiAnalysisErrorResponse)
```

Test mirror under `src/test/java/com/platform/saga/orchestrator/ai/...`, same package shape.

Deliberately **not** created now: `ai/workflow/routing/`, `ai/workflow/parallel/`, `ai/workflow/tools/`, `ai/dlq/`. These are named in "Future Agentic Evolution" below as where they'd go, but creating empty packages or speculative interfaces for them today would be exactly the over-engineering this revision avoids.

Why `SagaAnalysisPromptChain` and not `PromptChainOrchestrator`: the old name made "prompt chaining" sound like the permanent architecture. The new name says what it is — one `AiWorkflow` implementation, specifically a prompt chain, for the saga-analysis use case. A future routing- or tool-use-based workflow would be a sibling implementation of the same `AiWorkflow` interface, not a replacement of this class.

---

## Phase 1 — Foundation

### 1.1 Fix `OrderSaga` timestamps (independent prerequisite, own commit)

File: `event-driven-saga-orchestrator/src/main/java/com/platform/saga/orchestrator/model/OrderSaga.java`

This is **not conceptually part of the AI architecture** — it's a pre-existing gap (declared fields that are never set) that happens to block `SagaSnapshot` from having real timestamps. Implement and commit it separately from the AI feature:

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

New imports: `jakarta.persistence.PrePersist`, `jakarta.persistence.PreUpdate`. No changes needed in `SagaService` — Hibernate invokes these automatically on the existing `sagaRepository.save(...)` calls. `SagaServiceTest` mocks the repository so it's unaffected.

### 1.2 Enable `@ConfigurationProperties` scanning

File: `event-driven-saga-orchestrator/src/main/java/com/platform/saga/orchestrator/SagaOrchestratorApplication.java`

Add `@ConfigurationPropertiesScan` (present on `event-driven-order-service`'s main class, missing here):

```java
@SpringBootApplication
@EnableKafka
@ConfigurationPropertiesScan
public class SagaOrchestratorApplication { ... }
```

### 1.3 `SagaSnapshot` — shared model, with deterministic phase inference

```java
public record SagaSnapshot(
    UUID sagaId, String orderId, String productId, int quantity,
    String status, String cancellationReason, Instant createdAt, Instant updatedAt,
    List<String> reachedPhases) {}
```

`reachedPhases` is computed **deterministically in Java**, not by the LLM: `SagaStatus` encodes a known, linear happy-path progression plus explicit failure/compensation branches (e.g. reaching `PAYMENT_PENDING` implies inventory was already confirmed). A small static mapping in `SagaAnalyzerService` (or a tiny `SagaProgressionInferrer` helper if that mapping grows unwieldy) converts the current `status` into the list of phases the state machine guarantees were already reached. This is domain logic already encoded in the orchestrator's own transition rules — restating it isn't fabrication.

This directly implements the ADR's data-sources constraint: if `status = PAYMENT_PENDING`, the system may conclude the saga reached the payment phase (deterministically derivable), but the LLM is never asked to guess exact prior events, timestamps, retries, or outcomes it wasn't given.

### 1.4 Provider-neutral AI client abstraction

`ai/provider/AiModelClient.java`:
```java
public interface AiModelClient {
  <T> AiModelResponse<T> generate(AiRequest request, Class<T> outputType);
}
```

`ai/provider/AiRequest.java`:
```java
public record AiRequest(
    String systemPrompt, String userPrompt, Map<String, Object> outputJsonSchema) {}
```

`ai/provider/AiModelResponse.java`:
```java
public record AiModelResponse<T>(
    T output, String rawContent, Integer promptTokens, Integer completionTokens,
    long latencyMs, String model, String provider) {}
```

`ai/provider/AiModelException.java` — unchecked, provider-neutral. `generate(...)` throws this for any HTTP failure, timeout, missing/empty response, or JSON that doesn't deserialize into `outputType`. No OpenAI-specific exception type is ever visible outside `ai/provider/openai/`.

This is the boundary the prompt chain (and any future workflow) is coded against:
```text
Prompt Chain / future Agentic Pattern
          ↓
AiModelClient  (provider-neutral)
          ↓
OpenAiModelClient  (provider-specific)
```
Adding Anthropic, Gemini, or a local model later means one new class implementing `AiModelClient` — zero changes to `ai/workflow/`.

### 1.5 OpenAI implementation, with strict Structured Outputs

`ai/provider/openai/OpenAiClientProperties.java`:
```java
@ConfigurationProperties(prefix = "ai.openai")
public record OpenAiClientProperties(String baseUrl, String apiKey, String model,
    Duration connectTimeout, Duration readTimeout, double temperature) {}
```

`ai/provider/openai/config/OpenAiClientConfig.java` — mirrors order-service's `ExternalClientConfig`:
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

`ai/provider/openai/OpenAiModelClient.java` implements `AiModelClient`:
- Builds an OpenAI Chat Completions request with **`response_format: {"type": "json_schema", "json_schema": {"name": ..., "strict": true, "schema": <request.outputJsonSchema>}}`** — strict JSON Schema, not the looser `json_object` mode from the previous revision. Strict mode enforces required fields, field types, and enum membership at the provider level, so application code only needs to validate domain-level constraints the schema can't express (see 2.2).
- Every stage supplies its own small, hand-written JSON schema (a `Map<String, Object>` constant next to its output record) — no schema-generation library is added; the schemas are small (2 stages, ~6 fields each) and not worth automating yet.
- `POST /chat/completions` via the `RestClient` bean; parses the response content into `outputType` via the auto-configured Spring `ObjectMapper`.
- **Error handling uses `catch (Exception e)`, never `catch (Throwable)`.** Serious JVM errors (`OutOfMemoryError`, `StackOverflowError`) are not caught here and must not be converted into a degraded analysis result — they propagate as real errors, consistent with the principle that only genuinely recoverable failures (HTTP errors, timeouts, malformed model output) degrade gracefully. Every caught `Exception` is wrapped as `AiModelException` — never propagates as, e.g., a raw `RestClientException`.
- `OpenAiClientProperties`/DTOs (`OpenAiChatMessage`, `OpenAiChatCompletionRequest`, `OpenAiResponseFormat`, `OpenAiChatCompletionResponse`) live only in `ai/provider/openai/dto/` and are never imported from `ai/workflow/`.

---

## Phase 2 — Prompt Chaining MVP (first implementation milestone)

Two stages only. Recovery Analysis and Operator Explanation are **not** part of this milestone (Phase 3, below).

```text
SagaSnapshot
    ↓
Execution Analysis
    ↓
structured output
    ↓
Failure Classification
    ↓
structured output
```

This milestone's purpose is to validate LLM integration, structured outputs, prompt chaining, stage-to-stage handoff, error handling, observability, and isolation from transactional saga execution — not to deliver the full ADR-0018 chain yet.

### 2.1 Shared chain types

`ai/workflow/promptchain/stage/PromptChainStage.java` (interface), `StageResult.java` (success/failure factories, carries `StageName`, `StageStatus`, `StageTelemetry`), `StageStatus` enum: `SUCCESS, VALIDATION_FAILED, PROVIDER_ERROR, TIMEOUT, UNEXPECTED_ERROR`.

### 2.2 Stage 1 — Execution Analysis

**Naming:** renamed from "Timeline Reconstruction" (the name used in ADR-0018 and the previous plan revision) to **`ExecutionAnalysisStage`**, because "reconstruction" implies recovering a sequence of past events, which this stage does not do — it interprets the current snapshot plus deterministically-known `reachedPhases`. Recommend aligning ADR-0018's terminology to match (see "Recommended ADR-0018 changes" below); not changed as part of this plan-only revision.

```java
public record ExecutionAnalysisInput(SagaSnapshot saga) {}

public record ExecutionAnalysisOutput(
    String currentState,
    List<String> reachedPhases,       // echoes SagaSnapshot.reachedPhases(), model may add narrative framing only
    String narrativeSummary,          // 1-2 sentence factual recap, no speculation
    DataCompleteness dataCompleteness) {}

public record DataCompleteness(
    boolean onlyCurrentStatusSnapshotAvailable,  // always true in this MVP
    boolean stepByStepEventHistoryAvailable,     // always false in this MVP
    boolean dlqCheckAvailable,                   // always false — DLQ lookup deferred, see Phase 3+
    boolean retryCountKnown,                     // always false — actual per-saga retry attempts are not tracked anywhere in this codebase
    List<String> limitations) {}                 // free text, e.g. "Only current status known; DLQ not checked in this version; actual retry attempts not tracked."
```

`onlyCurrentStatusSnapshotAvailable`/`stepByStepEventHistoryAvailable`/`retryCountKnown` are the fabrication guard: the stage's validator rejects a model response that claims otherwise, since none of that is actually true in this MVP. No retry-limit value is passed into this stage at all — see 2.4.

### 2.3 Stage 2 — Failure Classification

```java
public enum FailureCategory {
  TRANSIENT_INFRASTRUCTURE_FAILURE, PAYMENT_PROVIDER_FAILURE, INVENTORY_FAILURE,
  TIMEOUT, INVALID_STATE, COMPENSATION_FAILURE, UNKNOWN
}

public enum ExecutionAssessment { IN_PROGRESS, COMPENSATING, TERMINAL_SUCCESS, TERMINAL_FAILURE, UNKNOWN }

public record FailureClassificationInput(
    ExecutionAnalysisOutput executionAnalysis, String cancellationReason) {}

public record FailureClassificationOutput(
    FailureCategory category, ExecutionAssessment executionAssessment,
    double confidence, String reasoning) {}
```

**`ExecutionAssessment` only contains values the current system can actually support with evidence it has.** There is no timeout/manual-review signal (ADR-0015 not implemented), no DLQ read path, no per-saga retry count, and no persisted step-by-step history — so the enum is limited to conclusions derivable from the saga's current status and cancellation reason alone. Additional values may be considered later if a concrete evidence source is designed for them (see Phase 3), but no such design exists yet, so none are anticipated here.

**`COMPENSATING` is its own value, not folded into `TERMINAL_FAILURE`.** A saga that is compensating is still executing — inventory release, refunds, or similar undo actions are actively in flight. Calling that "terminal" would say the saga is done when it demonstrably isn't.

#### Mapping `SagaStatus` → `ExecutionAssessment`

This mapping was derived by reading `SagaService.java` directly, not assumed. The orchestrator's state machine has exactly two statuses nothing ever transitions away from (`COMPLETED`, `CANCELLED` — confirmed by grep: each is set in exactly one place in `SagaService`, and no method contains a guard or transition keyed off either of them). Every other status is either a genuine wait point (guarded by an `if (saga.getStatus() != X) return/skip` check, meaning the orchestrator is waiting for an external event to arrive in that state) or a same-call transient marker that `SagaService` sets and then immediately overwrites again, within the same method invocation, before any external event is awaited:

| `SagaStatus` | Set in | Immediately followed by (same call)? | `ExecutionAssessment` | Why |
|---|---|---|---|---|
| `STARTED` | `handleOrderCreated` | Yes → `INVENTORY_PENDING` | `IN_PROGRESS` | Momentary; the saga is beginning execution. |
| `INVENTORY_PENDING` | `handleOrderCreated` | No — genuine wait, guarded in `handleInventoryReserved`/`handleInventoryReservationFailed` | `IN_PROGRESS` | Orchestrator is waiting on the Inventory service's async reply. |
| `INVENTORY_CONFIRMED` | `handleInventoryReserved` | Yes → `PAYMENT_PENDING` | `IN_PROGRESS` | Momentary; execution continues immediately. |
| `INVENTORY_FAILED` | `handleInventoryReservationFailed` | Yes → `CANCELLED` (via `cancelSaga`, **no compensation** — inventory was never reserved, so there's nothing to release) | `TERMINAL_FAILURE` | Unlike `PAYMENT_FAILED`, this path never enters `COMPENSATING`; the very next line in the same method finalizes the saga as cancelled. There is no pending undo action, so treating it as a resolved failure is accurate, not premature. |
| `PAYMENT_PENDING` | `handleInventoryReserved` | No — genuine wait, guarded in `handlePaymentProcessed`/`handlePaymentFailed` | `IN_PROGRESS` | Orchestrator is waiting on the Payment service's async reply. |
| `PAYMENT_CONFIRMED` | `handlePaymentProcessed` | Yes → `COMPLETED` | `IN_PROGRESS` | Momentary; execution continues immediately. |
| `PAYMENT_FAILED` | `handlePaymentFailed` | Yes → `COMPENSATING` (inventory release is triggered) | `COMPENSATING` | Unlike `INVENTORY_FAILED`, this path always enters compensation in the same call. If ever observed, the saga's fate is "compensation has started," not "resolved" — mapping it to `TERMINAL_FAILURE` would be wrong per this codebase's actual behavior. |
| `COMPENSATING` | `handlePaymentFailed` | No — genuine wait, guarded in `handleInventoryReleased`/`handleInventoryReleaseFailed` | `COMPENSATING` | Orchestrator is waiting on the Inventory service's release reply. Explicitly not terminal. |
| `COMPLETED` | `handlePaymentProcessed` | Never — no guard or transition anywhere references `COMPLETED` | `TERMINAL_SUCCESS` | Genuinely final; confirmed no outgoing transition exists. |
| `CANCELLED` | `cancelSaga` (called from 3 places: after `INVENTORY_FAILED`, after successful inventory release, after failed inventory release) | Never — no guard or transition anywhere references `CANCELLED` | `TERMINAL_FAILURE` | Genuinely final; confirmed no outgoing transition exists. The single resting point for every failure/compensation path. |

`UNKNOWN` is not mapped from any specific `SagaStatus` — it's the fallback for a status value the analyzer doesn't recognize (e.g., a future `SagaStatus` addition this stage hasn't been updated for), or for genuinely inconsistent input (e.g., a `cancellationReason` present on a status where that shouldn't be possible) that the model can't confidently resolve into one of the other four values.

**On the three transient/momentary statuses (`STARTED`, `INVENTORY_CONFIRMED`, `PAYMENT_CONFIRMED`) and the two same-call failure markers (`INVENTORY_FAILED`, `PAYMENT_FAILED`):** `SagaService` performs each pair of `sagaRepository.save(...)` calls as separate, non-`@Transactional` writes, so in principle a read at exactly the wrong microsecond could observe one of these. In practice this window is a few milliseconds at most. The mapping above still defines a correct answer for that case rather than leaving it undefined.

### 2.4 Retry information — not part of the first two stages

`kafka.dlq.max-retries` is a **configured ceiling**, not a per-saga fact — it says the system is configured to allow up to 3 retries, not that 3 retries happened for any given saga. It is genuinely not useful to Execution Analysis or Failure Classification, so it is **not** passed into `ExecutionAnalysisInput`, not referenced in either stage's prompts, not part of the workflow input, and not part of the first-MVP response context. No `configuredRetryLimit` field exists in this milestone's code.

What *is* kept: the documented fact that actual per-saga retry count is unavailable — `DataCompleteness.retryCountKnown` stays `false` in `ExecutionAnalysisOutput`, so the limitation is still surfaced honestly, just without smuggling in a config value that isn't actionable at this stage.

Retry-related information (the configured limit, and ideally an actual observed count) can be introduced later, when Recovery Analysis is implemented (Phase 3) or when actual retry attempts become observable — at that point it's a live input to a stage that can act on it, not inert context for two stages that can't.

### 2.5 Workflow: `SagaAnalysisPromptChain`

`ai/workflow/AiWorkflow.java`:
```java
public interface AiWorkflow<I, O> {
  WorkflowResult<O> run(I input, UUID analysisId);
}
```

`ai/workflow/promptchain/SagaAnalysisPromptChain.java implements AiWorkflow<SagaSnapshot, SagaAnalysisResult>`:
- Runs Execution Analysis → Failure Classification in order; short-circuits into a partial `WorkflowResult` if stage 1 fails.
- Wraps each stage call in `catch (Exception e)` (not `Throwable`) as a final guard on top of each stage's own internal handling.
- Logs per-stage telemetry (`analysisId`, stage name, model, provider, latencyMs, tokens, validation result) via structured `@Slf4j` logging — satisfies ADR-0018's observability list without a new metrics library.
- Registered as `@Component("sagaAnalysisWorkflow")` — an explicit bean name, not just an implicit `AiWorkflow<SagaSnapshot, SagaAnalysisResult>` type match (see 2.6 for why).

`SagaAnalysisResult` aggregates `ExecutionAnalysisOutput` + `FailureClassificationOutput` (only these two; `recovery`/`explanation` fields are added in Phase 3, not stubbed out now).

### 2.6 Saga use case: `SagaAnalyzerService`

Depends on `AiWorkflow<SagaSnapshot, SagaAnalysisResult>` (the interface) and `SagaRepository` only — and selects the active implementation **by explicit bean name**, not by relying on there being exactly one bean of that generic type. This class uses an **explicit hand-written constructor**, not Lombok `@RequiredArgsConstructor`, specifically so `@Qualifier` is visibly attached to the constructor parameter Spring actually resolves, rather than relying on Lombok's annotation-copying behavior to propagate a field-level `@Qualifier` onto a generated constructor parameter:

```java
@Service
public class SagaAnalyzerService {

  private final SagaRepository sagaRepository;
  private final AiWorkflow<SagaSnapshot, SagaAnalysisResult> sagaAnalysisWorkflow;

  public SagaAnalyzerService(
      SagaRepository sagaRepository,
      @Qualifier("sagaAnalysisWorkflow")
          AiWorkflow<SagaSnapshot, SagaAnalysisResult> sagaAnalysisWorkflow) {
    this.sagaRepository = sagaRepository;
    this.sagaAnalysisWorkflow = sagaAnalysisWorkflow;
  }

  ...
}
```

The active workflow implementation is still declared the same way:
```java
@Component("sagaAnalysisWorkflow")
public class SagaAnalysisPromptChain implements AiWorkflow<SagaSnapshot, SagaAnalysisResult> {
  ...
}
```

**Why this matters now, even with only one workflow implementation:** the project intends to add other `AiWorkflow<SagaSnapshot, SagaAnalysisResult>` implementations later — a routing-based `SagaAnalysisRoutingWorkflow`, a tool-use variant, etc. (see Future Agentic Evolution). Spring resolves a constructor parameter by type when exactly one bean matches; the moment a second bean of the same generic type exists, that resolution becomes ambiguous and the application fails to start. Naming the currently-active implementation's bean explicitly (`@Component("sagaAnalysisWorkflow")`) and qualifying the injection point to match sidesteps that failure mode without a registry or factory — switching which pattern is "active" for saga analysis is a one-line change (which class carries `@Component("sagaAnalysisWorkflow")`, or which `@Bean` method produces it), not a `SagaAnalyzerService` change.

This preserves exactly the dependency this plan has argued for from the start:
```text
SagaAnalyzerService
        ↓
active Saga Analysis Workflow   (selected by bean name "sagaAnalysisWorkflow")
        ↓
specific workflow implementation   (currently SagaAnalysisPromptChain)
```
`SagaAnalyzerService` still never references `SagaAnalysisPromptChain` by name, and still doesn't know whether the active implementation is a prompt chain, a router, or a tool-using agent — it only knows the qualifier. It is also **structurally impossible** for this class to mutate saga state or publish Kafka messages: `SagaService` and `KafkaProducerService` are never injected.

### 2.7 Controller

Unchanged in shape from the previous revision:
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
`AiAnalyzerExceptionHandler` (`@RestControllerAdvice(basePackageClasses = AiSagaAnalyzerController.class)`): `SagaNotFoundException` → 404; any other `Exception` → 502 with a generic body, logged server-side, never a raw stack trace. `SagaAnalysisResponse` now reflects only 2 stage summaries.

The endpoint path and response envelope are chosen to remain stable as the internal workflow evolves from a 2-stage chain to a 4-stage chain (Phase 3) to a non-chain agentic pattern (Future) — none of that should ever require a breaking API change.

### 2.8 `application.yaml` additions

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
```

No `ai.dlq-lookup.*` block in this milestone (removed — see Phase 3+). Nothing in this milestone reads `kafka.dlq.max-retries` either — see 2.4.

If `OPENAI_API_KEY` is unset, the first real call fails auth → `AiModelException` → stage 1 returns `PROVIDER_ERROR` → endpoint still returns `200` with `complete=false`, never a crash.

### 2.9 Error/timeout handling summary

| Layer | Guarantee |
|---|---|
| OpenAI HTTP call | Bounded by `connect-timeout`/`read-timeout`; failures become `AiModelException` (`catch (Exception)`, never `catch (Throwable)`) |
| Each chain stage | try/catch around call + domain validation; always returns a `StageResult`, never throws; JVM `Error`s are not caught here |
| Workflow | One more `catch (Exception)` guard per stage call; stops at first failure, returns partial `WorkflowResult` |
| Service | Only throws for unknown `sagaId`; everything AI-related already absorbed into `WorkflowResult` |
| Controller | 200 (complete or degraded), 404 (not found), 502 (last-resort, generic body only) |
| Blast radius | `SagaAnalyzerService`/`SagaAnalysisPromptChain`/stages have zero wiring into any `@KafkaListener`, `SagaService`, or `KafkaProducerService` — a failure here structurally cannot reach saga/transaction processing |

---

## Phase 3 — Complete the ADR-0018 chain (later)

Not part of the first implementation milestone:

- **Recovery Analysis** and **Operator Explanation** stages, added to `SagaAnalysisPromptChain` (or its successor) once stages 1–2 are implemented, tested, and validated in practice.
- **Additional `ExecutionAssessment` values**, if a future evidence source (e.g. ADR-0015's timeout/manual-review signal, or DLQ access) is designed and justifies distinguishing further execution conditions than the current enum supports (see 2.3). No such value is designed or decided yet.
- **Retry information** (`configuredRetryLimit` and, ideally, an actual observed count) as an input to Recovery Analysis, once that stage exists and can act on it (see 2.4).
- **DLQ lookup as a proper read/query capability** — not a new Kafka consumer that scans every DLQ topic from offset 0 on every request (rejected: doesn't scale as DLQs grow, unnecessary complexity for the first milestone). Revisit as a scoped query mechanism once there's a real need, potentially the same mechanism that later backs a `getDlqMessages(sagaId)` tool (see Future Agentic Evolution).
- Re-evaluate `ExecutionAnalysisOutput`/`DataCompleteness` fields once DLQ and/or ADR-0015's timeout signal exist — `dlqCheckAvailable` and `retryCountKnown` can flip to real, conditionally-true values instead of being hardcoded `false`.

## Future Agentic Evolution (later, outside the first implementation)

`ai/workflow/` gains siblings to `promptchain/` as needed — not created speculatively now:

```text
workflow/routing/
workflow/parallel/
workflow/tools/
```

Each would implement the same `AiWorkflow<I, O>` interface, so `SagaAnalyzerService` requires no changes to adopt one. Future tool-based capabilities (e.g., `getSagaState(sagaId)`, `getOrder(orderId)`, `getDlqMessages(sagaId)`) are exposed through a proper read/query mechanism at that time, not built now. Evaluator/Reflection and agentic loops are similarly deferred until a concrete workflow needs them.

---

## Testing plan

Mirror `src/test/java/com/platform/saga/orchestrator/ai/...`, same JUnit 5 + Mockito + AssertJ style as `SagaServiceTest`.

1. `OrderSaga` timestamp test (Phase 1, independent of the AI feature).
2. `OpenAiModelClientTest` — `MockRestServiceServer` (mirrors order-service's `InventoryClientTest`): happy path with a strict-schema response, malformed/non-conforming JSON path, 5xx/slow-response path → asserts `AiModelException` and nothing else leaks; asserts `response_format.json_schema` is sent with `strict: true`.
3. `ExecutionAnalysisStageTest` — well-formed output → `SUCCESS`; a response claiming `stepByStepEventHistoryAvailable: true` or `retryCountKnown: true` → `VALIDATION_FAILED` (fabrication guard); provider throws → `PROVIDER_ERROR`/`TIMEOUT`, no exception escapes.
4. `FailureClassificationStageTest` — one case per row of the `SagaStatus` → `ExecutionAssessment` mapping table (2.3): `INVENTORY_PENDING`/`PAYMENT_PENDING` → `IN_PROGRESS`; `COMPENSATING` and `PAYMENT_FAILED` → `COMPENSATING`; `COMPLETED` → `TERMINAL_SUCCESS`; `CANCELLED` and `INVENTORY_FAILED` → `TERMINAL_FAILURE`; a status this stage doesn't recognize → `UNKNOWN`. Also: a `*_PENDING` status classified as anything other than `IN_PROGRESS`/`UNKNOWN` → `VALIDATION_FAILED`; `PAYMENT_FAILED` classified `TERMINAL_FAILURE` instead of `COMPENSATING` → `VALIDATION_FAILED` (this is the specific regression this revision fixes); malformed/missing-field → `VALIDATION_FAILED`; a value outside the current `ExecutionAssessment` enum constants → `VALIDATION_FAILED`, since strict schema/enum validation should reject it before reaching domain validation.
5. Chain handoff test — stage 1's output is exactly what stage 2's input receives; a stage-1 failure means stage 2 is `verify(..., never())` invoked.
6. `SagaAnalyzerServiceTest` — verify only `SagaRepository.findById` is used and that `SagaService`/`KafkaProducerService` are never injected (structural isolation proof); unknown `sagaId` → `SagaNotFoundException`; missing API key / provider unavailable → degraded `200` response, not an exception.
7. `AiSagaAnalyzerControllerTest` — MockMvc standalone setup covering happy path (both stages succeed), degraded path (stage 2 fails), not-found path, unexpected-error path.
8. A dedicated "incomplete saga information" test — a saga snapshot with minimal fields (no `cancellationReason`, no prior phases beyond `STARTED`) still produces a `SUCCESS` result whose `DataCompleteness.limitations` is non-empty, proving the "surface the limitation, don't invent" rule end-to-end rather than only at the unit level.

No DLQ tests in this milestone (nothing to test — the capability doesn't exist yet).

---

## Suggested build order

1. **Phase 1:** timestamp fix (own commit) → `@ConfigurationPropertiesScan` → `SagaSnapshot` + `reachedPhases` inference → `AiModelClient`/`AiRequest`/`AiModelResponse`/`AiModelException` → `OpenAiModelClient` (independently testable via `MockRestServiceServer`, no saga data involved yet).
2. **Phase 2:** shared chain types → `ExecutionAnalysisStage` (built/tested against a mocked `AiModelClient`) → `FailureClassificationStage` → `AiWorkflow` interface + `SagaAnalysisPromptChain` (tested with both stages mocked) → `SagaAnalyzerService` (tested with the workflow mocked, isolation asserted) → controller + exception handler → `application.yaml` config.
3. Tests alongside each step, not batched at the end. Run `mvn spotless:apply` and `mvn test` after each increment.
4. **Phase 3 and Future Agentic Evolution:** not scheduled yet — revisit once Phase 2 is running in practice.

## Verification

- `mvn spotless:check` and `mvn test` green after each increment.
- Manual smoke test: `docker compose up -d` (existing Kafka+Postgres stack), start the orchestrator, create a saga via the normal order flow, export a real `OPENAI_API_KEY`, then:
  ```
  curl -X POST localhost:8085/ai/sagas/{sagaId}/analyze
  ```
  Confirm a `200` with both stage outputs populated, `dataCompleteness.limitations` non-empty, and confirm via logs/Kafka UI (`localhost:8090`) that normal saga event processing is completely unaffected while the analysis runs.
- Confirm graceful degradation by unsetting `OPENAI_API_KEY` (or pointing `ai.openai.base-url` at an invalid host) — expect `200` with `complete=false`, not a 500 or hang.
- Confirm the fabrication guard manually: analyze a saga sitting in `PAYMENT_PENDING` with no cancellation reason, and check that `executionAssessment` is `IN_PROGRESS` or `UNKNOWN`.
- Confirm the `COMPENSATING`-vs-`TERMINAL_FAILURE` distinction manually: analyze a saga in `PAYMENT_FAILED` or `COMPENSATING` and check that `executionAssessment` is `COMPENSATING`, not `TERMINAL_FAILURE` — the saga is still executing an undo action, not resolved. Separately, analyze a saga in `CANCELLED` (or, if caught mid-transition, `INVENTORY_FAILED`) and check that it *is* `TERMINAL_FAILURE`.
- Confirm bean wiring: the application starts successfully with only `SagaAnalysisPromptChain` registered as `@Component("sagaAnalysisWorkflow")`, and `SagaAnalyzerService` resolves it via the `@Qualifier("sagaAnalysisWorkflow")` injection point without ambiguity.

---

## Recommended ADR-0018 changes (not applied — plan revision only)

This plan-only revision does not edit ADR-0018. For traceability, if a future pass amends it, consider:

1. Rename "Timeline Reconstruction" to something reflecting partial-reconstruction reality (e.g. "Execution Analysis" / "Saga Execution Analysis"), matching `ExecutionAnalysisStage` above.
2. Note in the ADR that the first implementation ships only stages 1–2 (Execution Analysis, Failure Classification); Recovery Analysis and Operator Explanation are a documented later phase, not a simultaneous delivery.
3. Note that DLQ-based signals, referenced in the ADR's Scope of Analysis section, are not available until a future phase — which limits how finely the first implementation can distinguish execution conditions beyond the mapping in section 2.3 (`IN_PROGRESS`, `COMPENSATING`, `TERMINAL_SUCCESS`, `TERMINAL_FAILURE`, `UNKNOWN`).
4. Note that "compensation started" (already an ADR-0018 Timeline Reconstruction field: `compensationStarted`) and "genuinely terminal/failed" are different conditions in the actual state machine, and the ADR's language should not imply every failure signal is immediately final.

---

## Changes from Revision 1 → 2

**Removed from the first MVP:**
- Recovery Analysis and Operator Explanation stages (now Phase 3).
- The DLQ Kafka-consumer lookup (`DlqLookupService`, `ai/dlq/` package, `ai.dlq-lookup.*` config) — deferred entirely; no DLQ topics are read in this milestone.
- `advisoryOnly` self-correcting field (belonged to Recovery Analysis, which is deferred).
- The `ai.dlq-lookup.*` config block.

**Abstractions introduced for future flexibility:**
- `AiModelClient` / `AiRequest` / `AiModelResponse` / `AiModelException` — a provider-neutral boundary so `OpenAiModelClient` is the only class aware of OpenAI's request/response shape.
- `AiWorkflow<I, O>` — a generic workflow contract so `SagaAnalysisPromptChain` is one implementation among future routing/parallel/tool-use workflows, not the architecture's permanent center.
- Deterministic `reachedPhases` inference in `SagaSnapshot`, so the LLM is given known facts rather than asked to infer them from a bare status string.

**How Prompt Chaining is separated from the Saga analysis use case:**
```text
SagaAnalyzerService  (Saga use case, depends on the AiWorkflow interface)
        ↓
SagaAnalysisPromptChain  (one AiWorkflow implementation, currently the only one)
        ↓
ExecutionAnalysisStage → FailureClassificationStage  (the chain's own internals)
        ↓
AiModelClient  (provider-neutral)
```
`SagaAnalyzerService` never imports anything from `ai/workflow/promptchain/`; it is typed against `AiWorkflow<SagaSnapshot, SagaAnalysisResult>` only.

**Deferred to later phases:** Recovery Analysis, Operator Explanation, DLQ read access (as a proper query mechanism, not a full-topic scan), routing, parallelization, tool use, evaluator/reflection, agentic loops.

**Recommended ADR-0018 changes:** see the dedicated section above — stage naming, phased delivery, and the narrower signal set the first implementation can actually support.

---

## Changes in Revision 3

**`ExecutionAssessment` narrowed to only values the current system can support with evidence it has:**
```java
public enum ExecutionAssessment { IN_PROGRESS, TERMINAL_SUCCESS, TERMINAL_FAILURE, UNKNOWN }
```
No timeout/manual-review signal exists (ADR-0015 not implemented), no DLQ read path exists, no per-saga retry count is tracked, and no step-by-step history is persisted. Without any of that, distinguishing further execution conditions would only be a guess dressed up as a classification, so the enum only contains conclusions derivable from the saga's current status and cancellation reason. Explicit failure/compensation statuses map to `TERMINAL_FAILURE`, and genuinely ambiguous cases map to `UNKNOWN`. Tests (2.3's `FailureClassificationStageTest`) and the manual verification step were updated to match.

**`configuredRetryLimit` removed from the first two stages:** it no longer appears in `ExecutionAnalysisInput`, in either stage's prompts, in the workflow input, or in the first-MVP response context. `DataCompleteness.retryCountKnown` still documents that actual per-saga retry counts are unavailable; `application.yaml`'s note about reading `kafka.dlq.max-retries` was removed since nothing reads it in this milestone. It returns when Recovery Analysis (Phase 3) can actually act on it.

**Explicit bean identity for the active `AiWorkflow`:** `SagaAnalysisPromptChain` is now `@Component("sagaAnalysisWorkflow")`, and `SagaAnalyzerService` injects it via `@Qualifier("sagaAnalysisWorkflow")` rather than relying on being the only `AiWorkflow<SagaSnapshot, SagaAnalysisResult>` bean in the context. No registry or factory was introduced — this is the smallest change that keeps future routing/tool-use workflow beans from causing an ambiguous-injection startup failure.

**Wording:** "once stages 1–2 are proven in production" replaced with "once stages 1–2 are implemented, tested, and validated in practice" (Phase 3) — this repository is a reference implementation, not a deployed production system.

**Explicitly preserved, unchanged:** provider-neutral `AiModelClient`, `AiWorkflow`, `SagaAnalysisPromptChain`, deterministic `reachedPhases`, strict structured outputs, no DLQ scanning in the MVP, no saga history table, no speculative routing/parallel/tool packages, no transactional mutation dependencies.

---

## Changes in Revision 4

**`ExecutionAssessment` corrected to match the actual `SagaStatus` state machine** (inspected in `SagaService.java`, not assumed):
```java
// was (Revision 3)
public enum ExecutionAssessment { IN_PROGRESS, TERMINAL_SUCCESS, TERMINAL_FAILURE, UNKNOWN }
// now
public enum ExecutionAssessment { IN_PROGRESS, COMPENSATING, TERMINAL_SUCCESS, TERMINAL_FAILURE, UNKNOWN }
```
Revision 3's claim that "explicit failure/compensation statuses map to `TERMINAL_FAILURE`" was too aggressive: `COMPENSATING` is a genuine wait state (guarded in `handleInventoryReleased`/`handleInventoryReleaseFailed`) where the saga is still actively executing an undo action — not resolved, not terminal. `PAYMENT_FAILED` always transitions into `COMPENSATING` within the same call, so it now maps there too, not to `TERMINAL_FAILURE`. `INVENTORY_FAILED` behaves differently in this codebase — it transitions straight to `CANCELLED` with **no** compensation step (there's nothing to release, since inventory was never reserved), so it still maps to `TERMINAL_FAILURE`. Only `COMPLETED` and `CANCELLED` are genuinely terminal: confirmed by grep that no method in `SagaService` contains a guard or transition keyed off either value, while every other status is referenced by a `!= status` guard somewhere. The full per-status mapping and rationale now lives in 2.3.

**Constructor injection made explicit:** `SagaAnalyzerService` no longer uses `@RequiredArgsConstructor` with a field-level `@Qualifier`. It now has a hand-written constructor with `@Qualifier("sagaAnalysisWorkflow")` directly on the parameter, so the qualifier is visibly attached to what Spring actually resolves against, rather than depending on Lombok's annotation-copying behavior between a field and a generated constructor parameter. `SagaAnalysisPromptChain`'s `@Component("sagaAnalysisWorkflow")` registration is unchanged.

**Everything else preserved as instructed:** provider-neutral `AiModelClient`, `AiWorkflow`, `SagaAnalysisPromptChain`, deterministic `reachedPhases`, strict Structured Outputs, the two-stage Prompt Chaining MVP, no retry-limit input, no DLQ scanning, no saga history table, no speculative agentic-pattern packages, no transactional mutation dependencies, and Recovery Analysis/Operator Explanation/Routing/Parallelization/Tool Use/Evaluator-Reflection all still deferred.

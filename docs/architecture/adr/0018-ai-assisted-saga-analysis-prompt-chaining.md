## ADR-0018: AI-Assisted Saga Analysis via Prompt Chaining

**Status:** Proposed

## Context

The Saga Orchestrator (ADR-0005, ADR-0007, ADR-0014) is the authoritative, deterministic component responsible for coordinating distributed transactions across Order, Inventory, and Payment, including compensation on failure (ADR-0008).

When a saga fails or ends up in an abnormal state, an engineer must currently reconstruct what happened by manually reading saga state, domain events, retry counts, and DLQ entries (ADR-0013, ADR-0015). This is slow and requires deep familiarity with the event flow, and it does not scale as the number of steps and failure modes grows.

We want to introduce an operational analysis capability that helps engineers understand failed or abnormal saga executions faster, without changing how sagas are executed or recovered. This capability is observational and advisory only — it must never influence transactional execution.

This ADR does not introduce any LLM provider, library, or infrastructure into the codebase. It documents the architectural shape of the capability and the boundary between it and the orchestrator. No implementation classes, dependencies, or application code changes are made as part of this ADR.

**Assumption:** No LLM provider or client library is currently present in this repository. Provider choice, prompt implementation, and structured-output validation tooling are implementation details left to a future change and are intentionally not decided here.

---

## Decision

Introduce an **AI Saga Analyzer**, a component fully separate from the Saga Orchestrator, that uses **Prompt Chaining** to analyze completed or failed saga executions after the fact.

Prompt Chaining decomposes the analysis into an ordered sequence of small, single-responsibility prompts, where the structured output of one stage becomes the validated input of the next — rather than asking one large prompt to reconstruct the timeline, classify the failure, recommend a recovery, and explain it to a human all at once.

### Why Prompt Chaining instead of one large prompt

- **Separation of concerns:** each stage answers one question (what happened, why, what to do, how to explain it), which is easier to prompt reliably than a single monolithic instruction.
- **Structured, validated handoffs:** because each stage's output is a structured object rather than free-form text, it can be schema-validated before being passed to the next stage, catching malformed output early instead of letting it compound into a worse final answer.
- **Independent testability and observability:** each stage can be tested, evaluated, and monitored on its own (see Observability), instead of only being able to judge the chain as an opaque whole.
- **Independent evolution:** a stage's prompt or model can be changed (e.g., failure classification taxonomy) without touching the other stages.

The trade-off is added latency (multiple model calls instead of one) and more moving parts to operate. This is accepted given the gains in validation, observability, and testability described above.

### Chain Stages

Conceptually:

```
Saga execution data
  → Timeline Reconstruction
  → Failure Classification
  → Recovery Analysis
  → Operator Explanation
```

Each stage has a clear responsibility, a structured input, a structured output, independent validation, and independent observability.

#### 1. Timeline Reconstruction

- **Input:** saga execution state, domain events, relevant execution metadata.
- **Responsibility:** reconstruct what happened during the saga execution.
- **Structured output:** current saga state, completed steps, failed step, whether compensation started.

#### 2. Failure Classification

- **Input:** structured Timeline Reconstruction output, relevant error information.
- **Responsibility:** classify the most probable failure category.
- **Structured output:** failure category, confidence, reasoning.
- **Example categories:** `TRANSIENT_INFRASTRUCTURE_FAILURE`, `PAYMENT_PROVIDER_FAILURE`, `INVENTORY_FAILURE`, `TIMEOUT`, `INVALID_STATE`, `COMPENSATION_FAILURE`, `UNKNOWN`.

#### 3. Recovery Analysis

- **Input:** timeline analysis, failure classification, retry count, DLQ state, compensation state.
- **Responsibility:** produce an operational recovery recommendation.
- **Constraint:** the recommendation is advisory only — the AI Saga Analyzer never executes it.

#### 4. Operator Explanation

- **Input:** outputs from the previous three stages.
- **Responsibility:** generate a concise, human-readable explanation for an engineer describing what happened, where the saga failed, relevant system state, and a possible recovery action.

### Invocation

The analyzer is exposed as an on-demand operation, triggered explicitly by an engineer rather than automatically on every saga event. Conceptually:

```
POST /ai/sagas/{sagaId}/analyze
```

This is separate from any endpoint the Order or Saga Orchestrator services expose for transactional purposes. The exact API contract (request/response shape, synchronous vs. asynchronous invocation, authentication) is an implementation detail and may evolve without changing this architectural decision.

---

## Architectural Boundary

> Deterministic systems execute transactions. Probabilistic systems assist humans in understanding them.

| | Saga Orchestrator | AI Saga Analyzer |
|---|---|---|
| Nature | Deterministic | Probabilistic |
| Role | Authoritative, transactional | Observational, advisory |
| Responsibilities | Coordinates saga steps, performs compensation, changes system state | Analyzes execution history, classifies failures, produces recommendations |
| Effect on state | Mutates transactional state | Never mutates transactional state |

The AI Saga Analyzer must never directly control:

- Saga state transitions
- Payment execution
- Inventory reservation
- Retry execution
- Compensation execution
- Transactional domain events
- Distributed transaction consistency

It only reads execution history that the orchestrator has already produced, and its output is consumed by a human, not by the orchestrator.

---

## Failure Isolation

The AI Saga Analyzer is completely isolated from normal saga execution. The following must have **zero impact** on transaction processing:

- LLM provider outage
- Timeout
- Malformed LLM response
- Invalid structured output at any chain stage
- AI service failure
- Prompt-chain failure at any stage

Saga execution must continue functioning normally even if the AI analysis capability is degraded or entirely unavailable. This is inherent to the on-demand invocation model described above: the analyzer is invoked out-of-band from the transactional path, against already-persisted saga history, never inline with step execution or compensation.

---

## Observability

Each AI analysis run must have its own analysis identifier, distinct from the `sagaId` it analyzes, and its own telemetry. Relevant metadata includes:

- `sagaId`
- `analysisId`
- Prompt stage
- Model
- Provider
- Latency
- Token usage
- Execution status
- Validation result

Each of the four chain stages must be independently observable — a failure or degradation in Recovery Analysis, for example, must be distinguishable from a failure in Timeline Reconstruction.

---

## Alternatives Considered

### Single large prompt

Simpler to implement — one prompt, one call. Rejected as the primary approach because it is harder to validate (one large free-form or loosely-structured output instead of four small validated ones), harder to observe per-concern, harder to test in isolation, and harder to evolve (changing the failure taxonomy risks destabilizing timeline reconstruction or explanation quality in the same prompt).

### Deterministic rule engine only

A rule engine over saga state and error codes would be fully predictable and require no LLM. Rejected as the sole solution because operational failure scenarios are often ambiguous or novel, and a fixed rule set cannot generalize to failure patterns not anticipated in advance. May still be valuable as a complementary, cheaper first-pass filter in a future iteration, but is out of scope here.

### LLM directly controlling saga recovery

Rejected. Allowing probabilistic model output to directly trigger retries, compensation, or state transitions would compromise transactional consistency and violate the deterministic guarantees the Saga Orchestrator exists to provide. The analyzer only ever produces advisory output for a human.

---

## Consequences

### Positive

- Faster operational diagnostics for failed or abnormal saga executions
- Explicit, documented separation between the deterministic orchestrator and the probabilistic analyzer
- Each chain stage is independently testable and observable
- Establishes a foundation for future agentic capabilities without entangling them with transactional code

### Negative

- Introduces a dependency on an external LLM provider (once implemented)
- Adds latency to the analysis path (not the transactional path)
- Adds token/API cost per analysis
- Requires schema validation at every stage boundary to guard against malformed or invalid structured output
- Recommendations may be incorrect or low-confidence and must be clearly presented as advisory, not authoritative

---

## Future Evolution

This ADR describes the first step toward more advanced agentic capabilities. A future version may allow the AI component to retrieve information on demand using read-only tools, such as:

- `getSagaState(sagaId)`
- `getOrder(orderId)`
- `getPaymentStatus(paymentId)`
- `getInventoryReservation(orderId)`
- `getDlqMessages(sagaId)`

Such an evolution may introduce tool use, routing, and parallelization patterns. The detailed design of these patterns is intentionally out of scope for this ADR and does not warrant a separate ADR until it is actually undertaken.

---

## Related ADRs

- ADR-0005 Saga Orchestration
- ADR-0006 Phased Saga Implementation
- ADR-0007 Custom Orchestrator vs. Framework
- ADR-0008 Service Failure Handling in Sagas
- ADR-0013 Kafka Consumer Ack Control
- ADR-0014 Step-Based Saga Orchestration Model
- ADR-0015 Saga Timeout and Manual Review for Missing Responses

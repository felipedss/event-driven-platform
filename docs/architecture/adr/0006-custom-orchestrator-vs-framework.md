## ADR-0006: Custom Step-Based Orchestrator vs Framework

**Status:** Pending — to be decided after Phase 2 custom implementation is complete (see ADR-0005)

**Context:**
Once the custom step-based saga engine is built in Phase 2, a natural question arises: should we keep the custom engine or migrate to a battle-tested framework? Frameworks like Camunda, Temporal, and Spring Statemachine offer workflow persistence, visibility, retry handling, and timeouts out of the box — but at the cost of added complexity, learning curve, and operational overhead.

This ADR captures the trade-offs to evaluate when that decision point arrives.

**Decision:**
Deferred. Implement the custom step-based engine in Phase 2 first, then run the same saga workflow on each framework candidate and compare. The decision will be made based on observed trade-offs, not assumptions.

**Candidates to Evaluate:**

- **Custom step-based engine** — full control, no external dependencies, fits naturally in the existing Spring Boot service
- **Temporal** — durable execution model, built-in retry/timeout, excellent visibility; requires a separate Temporal server
- **Camunda** — BPMN-based workflow definition, rich UI for process monitoring; heavier operational footprint
- **Spring Statemachine** — native Spring integration, state machine model fits sagas well; less mature for distributed use cases

**Evaluation Criteria:**
- State persistence and crash recovery
- Compensation / rollback ergonomics
- Visibility into running and failed sagas
- Operational overhead (extra infrastructure, deployment complexity)
- Fit with existing Spring Boot + Kafka stack
- Code readability and maintainability

**Consequences (if migrating to a framework):**

✅ **Positive:**
- Retry, timeout, and visibility concerns handled by the framework
- Reduced boilerplate for step orchestration
- Community support and proven patterns

❌ **Negative:**
- Additional infrastructure to operate and monitor
- Learning curve for the team
- Potential lock-in to framework-specific abstractions
- May be overkill for the current scope

**Alternatives Considered:**
- Deciding without implementing the custom engine first (rejected — need empirical comparison, not speculation)

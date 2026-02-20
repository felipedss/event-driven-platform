## ADR-0003: Saga Pattern for Distributed Transactions

**Status:** Accepted

**Context:**
Need to maintain data consistency across services without distributed ACID transactions.

**Decision:**
Use orchestration-based Saga pattern with a dedicated saga orchestrator service.

**Rationale:**
- Centralized coordination simplifies monitoring
- Easier to visualize and debug workflows
- Clear compensation logic
- Better for complex multi-step workflows

**Consequences:**

✅ **Positive:**
- Centralized saga logic and monitoring
- Clear workflow visualization
- Easier error handling and compensation
- Consistent transaction patterns

❌ **Negative:**
- Saga orchestrator is a single point of failure (mitigated with HA)
- Orchestrator knows about all participating services
- Can become complex with many sagas

**Alternatives Considered:**
- Choreography-based sagas (rejected for initial version due to complexity in monitoring)
- 2PC (rejected due to performance and availability concerns)

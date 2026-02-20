## ADR-0004: Database Per Service

**Status:** Accepted

**Context:**
Need to ensure service autonomy and independent data management.

**Decision:**
Each service has its own PostgreSQL database instance.

**Consequences:**

✅ **Positive:**
- Service autonomy
- Independent schema evolution
- Technology flexibility
- Clear data ownership
- Easier to scale individual databases

❌ **Negative:**
- No foreign key constraints across services
- Data duplication
- Eventual consistency
- Increased storage costs

**Implementation:**
- PostgreSQL 15 for all services
- Connection pooling
- Read replicas for read-heavy services
- Database migrations per service
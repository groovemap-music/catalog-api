# Catalog API documentation

These guides cover only the `catalog-api` repository boundary. Deployment topology,
infrastructure credentials, and cross-service orchestration are owned by their respective
GrooveMap repositories.

```mermaid
flowchart TD
    Overview[README] --> Configure[Configuration]
    Overview --> Operate[Administration]
    Overview --> Develop[Usage and performance]
    Overview --> Evaluate[Offline evaluation]
    Overview --> Decide[Architecture decisions]
    Overview --> Release[Release compliance]
```

- [Configuration](configuration.md)
- [Administration](admin-guide.md)
- [Native identity and first-party activity](identity-and-activity.md)
- [CrateFit item-in-hand fit profile](cratefit.md)
- [Usage examples](usage-examples.md)
- [Logging](logging-guide.md)
- [Log emoji conventions](emoji-guide.md)
- [Database resilience](database-resilience.md)
- [Performance](performance-guide.md)
- [Coverage policy](coverage-policy.md)
- [Query performance optimizations](query-performance-optimizations.md)
- [Offline evaluation harness](evaluation.md)
- [Transactional email decision](transactional-email-provider-decision.md)
- [Architecture decisions](architecture-decisions.md)
- [Release compliance](release-compliance.md)
- [Test assertion audit](test-assertion-audit.md)
- [History rewrite approval gate](history-rewrite-gate.md)

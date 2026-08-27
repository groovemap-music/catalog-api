# Repository instructions

- Keep catalog-event, persistence, and internal Analytics contracts pinned and reproducible.
- Never restore monorepo-relative imports or write generated bindings into another repository.
- Default checks must not connect to PostgreSQL, Neo4j, Redis, RabbitMQ, Discogs, Anthropic, or Resend.
- Never log credentials, OAuth tokens, reset links, connection strings, or secret-file contents.
- Cross-repository CI authentication must use a narrowly installed GitHub App, never a PAT.
- Run `just check` before proposing a change; run `just image` for container changes.
- Publishing, tagging, pushing images, and releasing require separate approval.

# Catalog API log emoji conventions

The `catalog-api` uses emoji as a scan aid in structured log messages. The text and structured
fields must always communicate the full meaning; monitoring must never depend on the emoji.

| Emoji | Meaning | Catalog API examples |
|---|---|---|
| 🚀 | Service or operation starting | API startup, sync start |
| ✅ | Successful completion | service ready, sync complete |
| ❌ | Failed operation | unrecoverable request or persistence failure |
| ⚠️ | Degraded but handled condition | retry, unavailable optional integration |
| 🔄 | Work in progress or refresh | sync triggered, identity ledger refreshed |
| 💾 | Persistence | PostgreSQL pool or saved catalog state |
| 🔗 | Graph operation | Neo4j connection or graph query |
| 🏥 | Health | health server lifecycle |
| 📊 | Metrics and summaries | re-attachment census, enrichment backlog counts |
| 🧠 | Natural-language query | NLQ engine lifecycle |
| 🔧 | Shutdown or configuration | service shutdown, configuration change |
| 🔍 | Query or search result | analytics query complete, search cache miss |
| ⏱️ | Timeout | bounded Neo4j query timed out |
| ⏳ | Waiting on another holder | another replica holds the maintenance lock |
| 🔐 | Authentication or verification | OAuth flow started, 2FA change, app token minted |
| 📋 | Record or batch bookkeeping | audit entry recorded, collection sync start |
| 🧹 | Cleanup | activity erasure steps |

Use one leading emoji per message and keep the message understandable when the glyph is stripped.
Do not place credentials, tokens, email addresses, connection strings, reset links, or raw
third-party payloads in a log field. ASCII startup art remains plain text and names the service as
`GrooveMap catalog-api`.

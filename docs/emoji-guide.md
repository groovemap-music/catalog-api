# Catalog API log emoji conventions

The `catalog-api` uses emoji as a scan aid in structured log messages. The text and structured
fields must always communicate the full meaning; monitoring must never depend on the emoji.

| Emoji | Meaning | Catalog API examples |
|---|---|---|
| 🚀 | Service or operation starting | API startup, sync start |
| ✅ | Successful completion | service ready, sync complete |
| ❌ | Failed operation | unrecoverable request or persistence failure |
| ⚠️ | Degraded but handled condition | retry, unavailable optional integration |
| 🔄 | Work in progress | catalog reconciliation |
| 💾 | Persistence | PostgreSQL pool or saved catalog state |
| 🔗 | Graph operation | Neo4j connection or graph query |
| 🏥 | Health | health server lifecycle |
| 📊 | Metrics | collector lifecycle and summaries |
| 🧠 | Natural-language query | NLQ engine lifecycle |
| 🔧 | Shutdown or configuration | service shutdown, configuration change |

Use one leading emoji per message and keep the message understandable when the glyph is stripped.
Do not place credentials, tokens, email addresses, connection strings, reset links, or raw
third-party payloads in a log field. ASCII startup art remains plain text and names the service as
`GrooveMap catalog-api`.

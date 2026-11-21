# Assessment decisions

- A paper counts only when arXiv metadata, a Papers with Code link, and a successful live GitHub star observation agree. Zero stars is a measurement.
- A signal enters the fresh export only with a defensible publication timestamp in the closed 24-hour window. First seen and date-only observations stay out.
- Unsupported optional fields are null. Company matching requires an exact normalized alias or verified domain, with conflicting evidence left unresolved.
- Structured fields retain their source. LLM additions require a source quote and deterministic evidence checks. A replay database is separate from a live database.
- Explicit source rate limits and access controls are respected. Blocked sources are reported and can be replaced with approved feeds.

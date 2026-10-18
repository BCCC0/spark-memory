# Prior art and design position

This document explains the design space that shaped spark-memory. The goal is not
to reproduce another memory product, but to make the trade-offs explicit.

## Four useful models

### File-first knowledge bases

[Basic Memory](https://github.com/basicmachines-co/basic-memory) keeps knowledge
in Markdown, combines human-editable files with an index, and exposes the
knowledge base through MCP. Its strength is portability and inspectability:
people and tools can work with the same files.

spark-memory shares that preference for ordinary Markdown and inspectable history.
It differs in ownership: the service is the authority for the repository and
the write lock, while clients connect remotely instead of independently
editing local copies.

### Extracted semantic memory

[Mem0](https://github.com/mem0ai/mem0) sits between an application and a model.
Applications add conversation turns, then retrieve relevant memories using
scope such as user, agent, or run identifiers. Its strength is automatic
extraction and consolidation of salient facts.

spark-memory is intentionally more explicit. It stores durable notes and their
history rather than silently converting every conversation into facts. An
application or agent decides what deserves to become shared memory; optional
embeddings improve retrieval without becoming the source of truth.

### Stateful agent memory

[Letta](https://docs.letta.com/) treats memory as part of a stateful agent
runtime. Its strength is maintaining an agent that learns and changes through
continued interaction.

spark-memory is below that layer. It is a shared memory service that can support
many agents, tools, and humans; it does not own an agent loop, model, prompt,
or policy for deciding what an agent should remember.

### Temporal knowledge graphs

[Zep](https://help.getzep.com/) builds a temporal knowledge graph and assembles
token-efficient context from entities, relationships, and time-bounded facts.
Its strength is semantic and temporal retrieval at application scale.

spark-memory begins with durable documents, paths, metadata, and Git revisions.
That makes provenance and manual correction straightforward. A graph or
embedding index can be added as a derived view, but it must remain rebuildable
from the notes.

## Design position

These comparisons lead to five deliberate choices:

1. **Documents are canonical.** Indexes, embeddings, and future graphs are
   derived data, never the only copy of a memory.
2. **The service is the authority.** Multi-device access should converge on one
   repository rather than reconcile several local writers.
3. **Writes are explicit.** The system records an intentional note operation;
   it does not silently infer durable facts from arbitrary transcripts.
4. **History is first-class.** Every successful write is versioned, making
   changes inspectable and recoverable through the repository history.
5. **Retrieval is layered.** Metadata and full-text search work without models;
   semantic retrieval is optional and can be rebuilt when it changes.

## What this does not claim

spark-memory is not a complete agent framework, automatic fact extractor, or
temporal graph database. Those are complementary layers. Its role is to provide
a durable, shared, inspectable memory substrate that those layers can use.

## Links reviewed

- [Basic Memory repository](https://github.com/basicmachines-co/basic-memory)
- [Mem0: How it works](https://github.com/mem0ai/mem0/blob/main/docs/core-concepts/how-it-works.mdx)
- [Letta documentation](https://docs.letta.com/)
- [Zep key concepts](https://help.getzep.com/)

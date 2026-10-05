# Starter LLM Wiki

Welcome to your starter durable knowledge corpus for **Context Memory Fabric (CMF)**.

In CMF's four-layer architecture (evidence, episodic memory, durable knowledge, and runtime context), this directory represents **durable knowledge**. Unlike episodic memory (which records dated conversations, events, and temporal state changes), the wiki holds curated reference notes, project blueprints, systems documentation, and personal preferences that remain true across time.

## Directory Structure

This fictional starter vault is structured to illustrate how CMF indexes and retrieves personal knowledge:

- `projects/` — Active and historical project specifications, design decisions, and status.
- `systems/` — Infrastructure, hardware specs, home lab setup, networking, and deployment topology.
- `preferences/` — Coding standards, workflow preferences, tool conventions, and personal heuristics.

## Connecting Your Own Wiki

To point CMF at your own knowledge base (such as an Obsidian vault, a Logseq directory, or a folder of Markdown files):

1. Set `LLM_WIKI_PATH` in your `.env` or Docker Compose file to the absolute path of your directory.
2. Ensure files are Markdown (`.md`) or selectable text PDFs (`.pdf`).
3. CMF automatically ignores hidden files, `.git/`, `.obsidian/`, and node modules.
4. When querying your AI clients, tools like `search_wiki`, `search_knowledge`, and `get_context` will immediately index and surface relevant durable notes alongside your episodic memory.

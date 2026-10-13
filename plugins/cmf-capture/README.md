# cmf-capture

A Claude plugin that keeps Context Memory Fabric (CMF) capturing in places its transcript pollers can't reach. Cowork now runs sessions in the cloud, so no Cowork transcript reaches your computer. This plugin's Stop hook runs inside the session itself, wherever that is. Every few turns it asks Claude to record what was decided or learned with CMF's `capture_session` tool.

It needs the CMF connector in the same app (claude.ai: Settings → Connectors). The hook is standard-library Python 3 and reads nothing outside the session.

## What it does

- **When:** at most once per 5 turns, or after 30 minutes and at least 2 turns (`CMF_NUDGE_MIN_TURNS`, `CMF_NUDGE_MIN_MINUTES`). The turn that answers a checkpoint is never checkpointed again, so it can't loop.
- **What:** the same checkpoint text as CMF's own Stop hook (`server/adapters/capture_nudge.py`). Claude decides what is worth keeping, and everything it captures lands in the normal review queue.
- **Attribution:** every Claude app reaches CMF as one connector client, so CMF can't tell Cowork from the Code tab by itself. The hook reads the session's `entrypoint` and asks Claude to pass `source_description="claude session (<entrypoint>)"`, which CMF uses to label the capture (`claude_cowork`, `claude_desktop_code`, `claude_code`).
- **Fails open:** any error lets the session stop normally.

## Install

In Claude Code (the CLI or the desktop app's Code tab):

```
/plugin marketplace add tmargolis/context-memory-fabric
/plugin install cmf-capture@context-memory-fabric
```

In Cowork, add the same marketplace (`tmargolis/context-memory-fabric`) under Customize → Plugins and install **cmf-capture**.

If you installed CMF's local Stop hook earlier (`capture_nudge install --harness claude_code`), remove it once the plugin is on, or you'll get two checkpoints:

```
python -m server.adapters.capture_nudge uninstall --harness claude_code
```

Plain claude.ai chat runs no hooks or plugins. There, capture depends on Claude following CMF's server instructions, plus any custom instructions you add.

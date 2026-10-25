# Quickstart

Get Context Memory Fabric (CMF) running and connected to your AI apps in about ten minutes, without cloning anything or editing config files.

> [!NOTE]
> This uses the Context Memory Fabric management app from the [releases page](https://github.com/tmargolis/context-memory-fabric/releases). Until the first release is published, install from a checkout instead: [Developer setup](DEVELOPER-SETUP.md).

## 1. Install Docker Desktop

CMF runs in Docker. Install [Docker Desktop](https://www.docker.com/products/docker-desktop/) and start it. (On a Mac, OrbStack or Colima work too.)

## 2. Download and open Context Memory Fabric management

From the [latest release](https://github.com/tmargolis/context-memory-fabric/releases/latest), download the file for your computer:

| Your computer | File |
|---|---|
| Mac (Apple Silicon or Intel) | `cmf-macos-universal` |
| Windows | `cmf-windows.exe` |
| Linux | `cmf-linux-amd64` or `cmf-linux-arm64` |

Move it somewhere it can stay (for example `/Applications` on a Mac, or your Programs folder on Windows): your AI apps will run it to reach CMF, so it shouldn't live in Downloads.

The preview isn't signed, so the first time your computer asks before opening it:

- **Mac:** macOS says it "could not verify" the file and offers only **Move to Trash** or **Done**: click **Done**. Then open System Settings → **Privacy & Security**, scroll down to the line saying `cmf-macos-universal` was blocked, click **Open Anyway** and confirm with your password. You do this once. (On macOS 14 and older, right-click the file → **Open** → **Open** also works.) Or in Terminal: `chmod +x cmf-macos-universal && xattr -d com.apple.quarantine cmf-macos-universal`.
- **Windows:** "Windows protected your PC" → **More info** → **Run anyway**.

Context Memory Fabric management opens in your browser. It runs only on your computer and closes when you close the tab.

## 3. Install and choose your features

1. **Status** opens first. The top row says CMF isn't installed yet: click **Install CMF here**.
2. Open **Features**:
   - **Models.** Pick the LLM provider(s) you have a key for and paste the key. You can list several in order; the first is used and the rest take over when it's rate-limited. Pick **one** embeddings provider (it can't change once you have memories). A local model (Ollama, LM Studio) costs nothing per call.
   - **Knowledge.** No knowledge base, the demo wiki (fictional), or your own notes (a Markdown folder or Notion).
   - **AI apps.** Turn on the apps you use: Claude Desktop, Claude Code, Codex, Cursor, VS Code, Antigravity. CMF writes one entry into each app's own config, with a backup.
3. Click **Review changes**. You see every file CMF will write before anything happens.
4. Click **Apply**. The first start downloads the server (a few minutes). Status runs again when it's done.

## 4. Restart your AI app

Quit the app completely (on a Mac, ⌘Q, not just closing the window) and open it again, then **start a new conversation**: a conversation that was already open never sees newly added tools.

## 5. Try it

In Claude (or another app you turned on), ask:

1. **"Run the CMF self-test."** It writes a test memory to a scratch graph, finds it again and cleans up, reporting each step. Your own memories aren't touched.
2. **"What did I work on last week?"** On a fresh install the answer is thin. To give CMF your recent history, import the last couple of weeks of conversations with `cmf backfill --days 14` in a terminal (it shows what it will cost before it starts; a local model is free). Imported items go to a review queue first.

From now on CMF works in both directions: your apps read from it and, at natural checkpoints, record decisions and lessons into it.

## If something isn't right

- Open Context Memory Fabric management again (double-click the file). **Status** names each problem in one line, and most have a **Fix** button.
- **Copy diagnostics** builds a report with keys, names and memory text removed. You see it before it's copied, and can send it to whoever helps you.

## More

- ChatGPT, Gemini or claude.ai connectors need CMF on a public HTTPS address: see `cmf remote` in [Developer setup](DEVELOPER-SETUP.md) and the Remote section of the [setup guide](SETUP.md).
- Everything Context Memory Fabric management does is also a command: run `cmf help`.
- The full reference is the [setup guide](SETUP.md).

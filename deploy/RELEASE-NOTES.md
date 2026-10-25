Context Memory Fabric management {{VERSION}}: one program that installs, checks and repairs Context Memory Fabric. The server image `ghcr.io/tmargolis/context-memory-fabric:{{VERSION}}` is published with it.

## Download

| Your computer | File |
|---|---|
| Mac (Apple Silicon or Intel) | `cmf-macos-universal` |
| Windows | `cmf-windows.exe` |
| Linux | `cmf-linux-amd64` or `cmf-linux-arm64` |

You need [Docker Desktop](https://www.docker.com/products/docker-desktop/) running. Open the file and Context Memory Fabric management opens in your browser; `cmf help` lists the commands.

## Opening an unsigned download

This preview isn't signed, so your computer asks before running it the first time.

- **Mac:** macOS says it "could not verify" the file: click **Done**, then go to System Settings → **Privacy & Security**, click **Open Anyway** next to `cmf-macos-universal` and confirm with your password (once). On macOS 14 and older, right-click → **Open** → **Open** also works. In Terminal you can instead run `chmod +x cmf-macos-universal && xattr -d com.apple.quarantine cmf-macos-universal`.
- **Windows:** on the "Windows protected your PC" screen click **More info** → **Run anyway**.
- **Linux:** `chmod +x cmf-linux-*`.

`SHA256SUMS` lists each file's checksum.

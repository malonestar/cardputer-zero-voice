# CardputerZero voice layer

Voice control for the whole [M5Stack CardputerZero](https://docs.m5stack.com/) launcher, on 512 MB of RAM, on the stock CardputerZero OS. No firmware or kernel changes: one rebuilt program (the launcher), plus a few small services running beside it.

- **Wake word** (microWakeWord) or **hold TAB** to talk, at the launcher
- **~120 phrases matched entirely on the device** by a keyword spotter (sherpa-onnx, 3.3M parameters): works with the Wi-Fi off
- **Fuzzy matching** of anything off-script: the audio goes to a Whisper server on your network, the text comes back and a rule-based matcher on the device maps it to a command
- **Escalation** of anything that isn't a device command to a voice-assistant server that answers out loud
- **Hold ALT to dictate**: speech is typed as if from the keyboard
- **An MCP tool server** exposing 20 device actions (apps, home, volume, brightness, battery, temperature, Wi-Fi, lock, sleep, typing, screenshot) so an outside AI assistant can drive the device
- **Steps aside for apps**: when an app opens, the voice service and tool server stop so the app gets the memory and the microphone; they come back at the launcher
- **TARS remote** (optional): a launcher app that turns the Cardputer into a push-to-talk remote for a TARS-AI robot

As seen on [Sentient Shed](https://linktr.ee/sentientshed).

## How it fits together

```
 CardputerZero (stock OS, Debian Trixie)
 ├── launcher  (rebuilt: local control interface switched on + 4 commands)
 │     ▲  tcp://127.0.0.1:5557
 ├── voice.py        wake word, keyword spotter, key triggers, matcher, on-screen pill
 ├── cz_mcp.py       MCP tool server (cz_tools.py) on :8765, bearer token
 └── appwatch.py     stops voice + tools while an app is open
        │
        ▼  only when the on-device steps don't catch it
 home server: Whisper (speech-to-text), voice assistant, Piper TTS
```

## 1. Rebuild the launcher

The stock launcher can't be told "open this app". M5's launcher source (MIT) already contains a local control interface (`cp0_lvgl` automation RPC) that isn't compiled in. `launcher/rpc_commands.patch` switches it on and adds four commands:

| command | reply |
|---|---|
| `apps` | the app names in carousel order |
| `current` | what's on screen (`home`, `page`, `external`) and the foreground app |
| `launch <name>` | opens an app (exact name, then unique prefix) |
| `home` | back to the launcher (stops an external app like an ESC long-press) |

The patch is against **CardputerZero/launcher tag `launcher-v0.6.105`**. `launcher/build.sh` cross-compiles it for aarch64 in WSL/Ubuntu (needs `aarch64-linux-gnu-g++`):

```bash
cd launcher
./build.sh            # clones the tag, applies the patch, builds with CONFIG_REPO_AUTOMATION=y
# if RadioLib fails to build: ./fix_radiolib.sh (the SDK pulls RadioLib 8.x; 0.6.105 needs 7.x), then ./build.sh again
```

Copy the resulting `M5CardputerZero-APPLaunch` binary to the device in place of the stock one (keep the stock binary for rollback). `launcher/cp0_rpc.py apps|current|launch <name>|home` talks to it for testing. Until M5 ships the RPC in a release, launcher updates mean re-applying the patch.

## 2. Install the services on the device

```bash
mkdir -p ~/cz-ai && cd ~/cz-ai
python3 -m venv venv
venv/bin/pip install numpy sherpa-onnx sentencepiece pymicro-wakeword pyzmq requests pillow
# copy everything in device/ here
cp config.example.json config.json      # point the URLs at your own speech server
```

**Keyword-spotter model** (not in this repo): download `sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01` from the [sherpa-onnx kws-models release](https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01.tar.bz2) and put the three `*.int8.onnx` files and `tokens.txt` in `~/cz-ai/kws/` next to `keywords.txt`. `kws_commands.py` regenerates `keywords.txt` from the phrase list.

**Wake word**: the default is `okay_nabu` (ships with pymicro-wakeword; `hey_jarvis` also works). Custom models drop in by name.

Then the user services:

```bash
mkdir -p ~/.config/systemd/user && cp *.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now cz-mcp.service cz-voice.service cz-appwatch.service
```

`cz_mcp.py` creates a random bearer token in `~/cz-ai/token` on first run. Keep it private; MCP clients need it in `Authorization: Bearer <token>`.

**Three OS settings** that made room (optional but recommended on 512 MB): turn off Wi-Fi power saving (stability), don't start the graphical desktop session at boot (the launcher draws straight to the screen), and drop GPU memory from 128 to 64 MB. Together: ~120 MB → ~200 MB available.

## 3. TARS remote (optional)

`tars/` is a launcher app: hold TAB, talk, and a [TARS-AI](https://github.com/pyrater/TARS-AI) robot answers out loud while the reply scrolls on the Cardputer. It uses the robot's existing web login and Socket.IO interface; nothing changes on the robot. Set `tars.url` in `config.json`, put the robot's web UI password on the first line of `~/cz-ai/tars_password` (`chmod 600`), then run `tars/install.sh` on the device. `tars/mock_tars.py` is a stand-in robot for testing.

## Not included

- Your secrets: `config.json`, `token`, `tars_password` are git-ignored
- Model files (download links above), custom wake-word models
- A prebuilt launcher binary (build it from the patch)

## Credits

microWakeWord / pymicro-wakeword, sherpa-onnx keyword spotter, Whisper (faster-whisper), Piper, Model Context Protocol, Tailscale, M5Stack CardputerZero launcher (MIT), LVGL.

MIT licensed.

# N01

<p align="center">
  <b>Context-hosted persistent memory for AI characters</b><br>
  <i>ta remembers every word you said</i>
</p>

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-yellow.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/Python-3.10%2B-blue.svg" alt="Python 3.10+"></a>
  <a href="#"><img src="https://img.shields.io/badge/Dependencies-zero-brightgreen.svg" alt="Zero dependencies"></a>
  <a href="https://platform.deepseek.com"><img src="https://img.shields.io/badge/LLM-DeepSeek-5B6EF5.svg" alt="DeepSeek"></a>
</p>

<p align="center">
  <a href="README.md">简体中文</a> | English
</p>

---

A fully local AI character companion. Give it a place to talk, a memory, and a little ability to look things up on its own.

> ta lives on your own machine. With a personality, a memory, and internet access. Your data never leaves your computer.

- **Zero third-party dependencies** (Pillow for images and ffmpeg for video are optional)
- **Everything stays local**: chat logs, images and videos live in `data/`
- **Uses the DeepSeek API**, but any OpenAI-compatible endpoint works
- **QQ integration**: run ta inside QQ through the official bot API
- **~96% prefix cache hit rate**, so long conversations stay cheap

## Why it's different

Most AI companion projects lose their memory after a few dozen turns. Either they stuff the entire history into the prompt until tokens explode, or they truncate it and ta forgets what you said three days ago.

N01 takes a different approach: **stable content goes first, changing content goes last**, so DeepSeek's prefix cache almost always hits. Measured hit rate is around 96%, meaning only the two or three new messages get recomputed each turn. Old memories aren't deleted; they're compressed into importance tiers. The longer you talk, the more familiar ta becomes.

## Quick start

```bash
# 1. Install Python 3.10+, then:
pip install -r requirements.txt   # optional Pillow only; runs fine without it
python run.py

# 2. Open http://127.0.0.1:8765 in your browser
# 3. Enter your DeepSeek API key on first launch
#    (create one at https://platform.deepseek.com -> "API keys")
```

An admin account is generated on first launch. **The password is printed to the console only once**, so write it down.

Windows users can double-click `启动.bat` to start both the web server and the QQ bot.

### Configuration

`config.json` and `qqbot/config.json` contain secrets and are listed in `.gitignore`, so **they never reach the repo**. Copy them from the templates after cloning:

```bash
cp config.example.json config.json
cp qqbot/config.example.json qqbot/config.json
```

Then open the web UI and enter your API key. It gets written into `config.json` automatically.

## How ta remembers

Rather than cramming the whole chat log into the prompt, memory is layered into three parts:

```
Older snapshot     Everything before today, grouped by day and tiered by importance
   +
History            Everything said today, verbatim and in order
   +
Current message
```

**The boundary is 00:00 today.** Today's messages go into history; everything before that goes into the snapshot. The two halves join up without overlap.

Each message is scored for importance in the background: `0.1 / 0.3 / 0.5 / 0.7 / 0.9`. The snapshot additionally surfaces:

| Tier | Range | Purpose |
| --- | --- | --- |
| 0.9 | Any time | Always included, the heaviest things |
| 0.7–0.9 | Last 30 days | Important, but shouldn't occupy space forever |
| 0.5–0.7 | 3 days | Slightly weighty recent events |

Records are **append-only**. There's no forgetting mechanism, and nothing is dropped just because time has passed.

### Why it saves money

DeepSeek supports prefix caching. This layout puts unchanging content first and per-turn content last:

```
[system] Persona + rules      <- changes only when the file does, always cached
[system] Older snapshot       <- changes once a day
[history] Today's messages    <- append-only
[system] Who's talking + time <- changes every minute
[user]  This message
```

Measured hit rate is about 96%, so each turn only recomputes the two or three new messages.

## What ta can do

| Capability | Description | Toggle |
| --- | --- | --- |
| Chat | In character, with long-term memory | On by default |
| Identify people | Multiple users, isolated context per uid | On by default |
| See images | Send an image, ta describes and remembers it | On by default |
| Watch video | Extracts three frames as images | Requires ffmpeg |
| Web search | Weather, news, exchange rates | `"search": true` |
| Read pages | Fetches page text when search results lack details | Same as above |
| Recall | Actively retrieves things said in the past | `"memory_tool": true` |
| Location awareness | Determines city from IP | `"geo": true` |

## Changing ta's personality

There's only one thing to do: edit `系统提示词/人格本体.md`.

That file goes into the prompt verbatim, so **it is ta**. The more specific you write, the more ta sounds like that person. Write labels and ta will just recite labels.

Suggested structure:

```markdown
# Name

## Basics          <- who ta is, what ta looks like, what world ta lives in
## What ta is      <- whether ta knows it was created or denies it
## Boundaries      <- what ta won't do, what ta won't say
## Speech style    <- sentence length, formality, when ta goes quiet
```

`系统提示词/示例_对话风格.md` is a template for how ta should speak. Editing it is more effective than writing rules, because **models learn from examples faster than from rules**.

## Configuration reference

In `config.json`, the commonly used keys:

| Key | Default | Description |
| --- | --- | --- |
| `api_key` | empty | DeepSeek API key (set it in the web UI) |
| `model` | `deepseek-chat` | Also `deepseek-reasoner` |
| `char_name` | `ta` | How ta is referred to in records |
| `creator_uid` | `00` | Creator's uid, treated specially |
| `ctx_days` | 3 | How many days the snapshot covers |
| `ctx_group` | `day` | Group snapshot by day (`person` for per-person) |
| `memory_tool` | false | Let ta actively recall old memories |
| `search` | true | Web access |
| `geo` | true | Location from IP |

A `0` in any `ctx_*` or `tier_*` value means **unlimited**.

## QQ integration (optional)

1. Create a bot on the QQ Open Platform and get the `AppID` and `AppSecret`
2. Fill them into `qqbot/config.json`, or use "接口与 QQ 机器人设置" in the web UI
3. Have someone message the bot, then grab their `openid` from the log
4. Bind it under "管理 → 账号 → QQ 机器人 openid" in the web UI

```
python run.py          # web server (must start first)
python qqbot/main.py   # QQ bot
```

Notes:

- Uses the **official bot API**; users are identified by `openid`, not QQ number
- When `bot_token` is empty, `/api/bot/chat` only accepts local requests; for cross-machine setups, set the same value on both sides
- Group chat replies are off by default; enable with `group_reply: true`

## Multiple users

- Each uid has its own context and memory
- Admins see everyone; regular users see only themselves (`private_creator` controls whether others can see the creator's messages)
- Accounts are managed in the web UI, or via CLI:

```bash
python -m app.accounts add --uid 23 --name Alice --role user
python -m app.accounts passwd --uid 23
```

## Project layout

```
app/
  config.py     Configuration and keys
  persona.py    Persona assembly
  engine.py     Orchestration: chat + tool loop + scoring
  context.py    Older snapshot (by day + by importance)
  memory.py     Relevance and weighting
  store.py      Data file I/O
  llm.py        API client
  server.py     Web server (HTTP + SSE + login)
  accounts.py   Accounts and passwords (PBKDF2)
  media.py      Image compression, video frame extraction
  search.py     Bing search + page reading
web/index.html  Single-file frontend
qqbot/          Official QQ bot
系统提示词/      Persona and examples
data/           Generated at runtime: chat logs, images, videos
```

## Where the data lives

```
data/chatlog/YYYY-MM.jsonl   Raw chat messages (append-only)
data/scores.jsonl            Importance scores
data/recall.jsonl            Recall events
data/media/                  Images and videos
data/state.json              Location cache, etc.
accounts.json                Accounts
```

**None of these should be committed.** `.gitignore` already excludes them.

## Security

- The web server binds to `127.0.0.1` only, unreachable from outside
- Passwords are stored as PBKDF2-HMAC-SHA256 hashes, never in plaintext
- The API key is stored in `config.json` **in plaintext** (that file is not committed)
- Opening pages checks a URL allowlist (`http/https` + non-private IPs, on every hop)

## Known limitations

- Standard library only, so no advanced concurrency beyond streaming
- Video "understanding" is three sampled frames, not true video comprehension
- No voice (wire up your own ASR/TTS)
- Memory is character-based, so the persona dilutes over long conversations

## Contributing

Issues and PRs are welcome. Please read [CONTRIBUTING.md](CONTRIBUTING.md) first.

If you find this project interesting, a **Star** is the best encouragement ⭐

## License

[MIT](LICENSE) © 2026 N01 contributors

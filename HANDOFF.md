# N-01 项目实现说明

一个「有人格、有记忆」的本地 AI 对话程序：网页界面 + 命令行 + QQ 官方机器人三个入口，
共用同一个引擎、同一份记忆。**零第三方依赖**，只需要 Python 3.10+。

> **本模板不包含任何角色人设。** 它只是一个宿主框架：人设由使用者自己写，
> 格式见第 7 节。原作者的 `人格本体.md` 和两个示例文件不会随项目提供。

这份文档写给两个读者：接手的**人**，以及读代码的 **AI**。

---

## 1. 它是什么

一个 AI 角色的宿主框架。核心不是聊天，是**记忆**：

- 每轮对话原话落盘
- 由模型给每句话打「这件事有多重」的分
- 需要回忆时，ta**自己调用工具**去翻，而不是每轮把记忆全塞进提示词
- 旧事按时间衰减，被想起来的次数越多越牢
- 每天凌晨自动「遗忘」一批（只写墓碑，不删原文）

设计取向：**存事实，算判断**。原始记录永远不改写，权重每次现算，所以随时能换算法重跑。

---

## 2. 目录结构

```
N01/
  run.py                 唯一入口（启动网页服务）
  启动网页.bat            Windows 一键启动（网页 + QQ 机器人）
  config.json            全局设置
  api.txt                DeepSeek API key（一行，明文）
  accounts.json          账号（密码只存哈希）
  系统提示词/             人设和示例，全部放这里
    人格本体.md           人设正文 ← 自己写，模板不含
    示例_对uidXX.md       某个人的专属示例 ← 自己写

  app/                   程序主体
    paths.py             程序目录解析（打包后也正确）
    config.py            配置读取、密钥查找（config → 环境变量 → api.txt）
    persona.py           人设拼装（主体 + 示例）
    llm.py               DeepSeek 客户端（流式 / 带工具）
    memory.py            相关度、权重、遗忘判据（纯计算，无 IO）
    store.py             五个数据文件的读写（唯一碰磁盘的地方）
    engine.py            编排：对话 + 工具循环 + 打分 + 遗忘
    accounts.py          账号、密码哈希、角色
    server.py            HTTP 服务、会话、管理接口

  tools/
    split_persona.py     把混在一起的人设拆成三个文件

  web/
    index.html           界面（单文件，无构建步骤）

  qqbot/                 QQ 官方机器人桥（独立进程）
    main.py              token 管理、连网关、收事件、转发、回消息
    ws.py                最小 WebSocket 客户端（标准库实现）
    config.json          AppID / Secret

  data/                  运行时生成，会持续增长
    chatlog/YYYY-MM.jsonl   对话原话，按月分片
    scores.jsonl            每轮的强度分
    recall.jsonl            每次被想起
    forgotten.jsonl         已遗忘的名单
    state.json              上次遗忘时间
```

---

## 3. 运行

**环境**：Python 3.10 以上，无第三方包。

```
pip install 什么   ← 不需要，一个都不用
```

**启动**：

```
python run.py                    # 网页服务，默认 127.0.0.1:8765
python qqbot/main.py             # QQ 机器人（可选，需要另开一个窗口）
```

Windows 上双击 `启动网页.bat` 会同时起这两个，并打开浏览器。

**首次启动**：如果没有 `accounts.json`，会创建 `uid00`（管理员）并在控制台打印一次随机密码。
**只打印一次**，记不住就用 `python -m app.accounts passwd --uid 00` 重置。

---

## 4. 数据模型

五个 JSONL，全部**只追加，永不改写**。

### `data/chatlog/YYYY-MM.jsonl`

```json
{"id":"a1b2c3d4e5f6","ts":"2026-11-02T20:00:00+08:00","uid":"00","role":"user","text":"今天好累"}
{"id":"7f8e9d0c1b2a","ts":"2026-11-02T20:00:01+08:00","uid":"char","role":"assistant","text":"嗯。\n摸摸头。"}
```

一条消息一行。ta的 uid 固定是 `char`。

### `data/scores.jsonl`

```json
{"id":"a1b2c3d4e5f6","s":0.7,"ts":"...","model":"deepseek-chat"}
```

`id` 指向 chatlog 里**用户那条**消息。分值只有五档：`0.1 0.3 0.5 0.7 0.9`；
`-1` 表示打分失败，等待补打。

### `data/recall.jsonl`

```json
{"id":"a1b2c3d4e5f6","ts":"..."}
```

每被当成记忆取用一次，记一行。**权重里的「想起次数」和「上次想起时间」都从这里算，不单独存。**

### `data/forgotten.jsonl`

```json
{"id":"a1b2c3d4e5f6","ts":"...","reason":"decay"}
```

墓碑。检索时跳过，原文不删。

### `data/state.json`

```json
{"last_forget":"2026-11-02T05:00:00+08:00"}
```

### `accounts.json`

```json
{
  "users": [
    {
      "uid": "00",
      "name": "（名字）",
      "role": "admin",
      "relation": "（和 uid00 的关系，可空）",
      "openid": "（QQ 官方机器人给的 user_openid，可空）",
      "union_openid": "（跨应用统一 ID，可空）",
      "salt": "…",
      "hash": "…",
      "iter": 200000,
      "created": "..."
    }
  ]
}
```

密码只存 PBKDF2-HMAC-SHA256 哈希（20 万次迭代，每人独立 salt），**无法反推**。

---

## 5. 一轮对话发生了什么

```
用户输入（可能是文字、图片、或者两者都有）
  ↓
① 组装 system prompt
     人设主体 + 当前 uid 的示例 + 工具说明 + 关系披露规则
     + 说话人信息（含名单）+ 当前时间
  ↓
② 带工具调用模型（tools: search_memory, list_people）
     ├─ ta不需要回忆 → 直接流式回答
     └─ ta发起 tool_call → 本地检索 → 把结果作为 tool 消息回填 → 再流式回答
  ↓
③ 写入 chatlog（两条：用户的、ta的）
  ↓
④ 后台线程给这一轮打分（0.1~0.9），写入 scores.jsonl
```

### 图片怎么进来的

模型能直接读图（`image_url` 传 base64 data URL，实测能准确读出图里的字），
所以不需要第三方视觉模型。但有一条硬约束：

**原图只出现在当前这一轮，历史里只留文字。**

```
本轮  messages = [..., {role: user, content: [
                        {type: text,  text: "创建者：你看这个"},
                        {type: image_url, image_url: {url: "data:image/jpeg;base64,…"}},
                      ]}]
下一轮 历史 = [{role: user, content: "创建者：[图片] 浅色背景上写着 CAT-2024"}]
```

为什么：一次对话十来轮，如果把图留在历史里，每轮都要重传一遍，
一张图几百到几千 token，成本会翻十几倍。

所以收到图时先让模型写一句描述（`Engine.caption_images`），
存进 chatlog 的是 `[图片] 那句描述`，原图落到 `data/media/YYYY-MM/`，
路径记在消息的 `media` 字段里。

```json
{"role":"user","text":"[图片] 浅色背景上写着 CAT-2024（你看这个）",
 "media":[{"type":"image","desc":"浅色背景上写着 CAT-2024",
           "path":"data/media/2026-11/00_0d2c9544.jpg"}]}
```

**记忆检索因此不用改**：它查的是文本，`[图片] 那句描述` 本身就是文本。

### 图片压缩

规则写在 `app/media.py` 顶部：

```python
COMPRESS_ABOVE = 1024 * 1024     # 超过 1MB 才压
MAX_SIDE = 1024                  # 分辨率上限（长边）
QUALITY_STEPS = (85, 78, 70, 62, 55, 48)
```

- ≤ 1MB 且长边 ≤ 1024 → **原样保留**，字节完全不变
- > 1MB → 先逐档降 JPEG 画质，还不够就继续按 75% 缩分辨率，直到进 1MB
- 长边 > 1024 → 一律先缩到这个尺寸
- 没有 Pillow → 不压，原样发（`why` 里会写「没有 Pillow，不压」）

压没压、压了多少，全部记在消息的 `media` 里：

```json
"media": [{
  "path": "data/media/2026-11/00_4685ea5e3a3a.jpg",
  "desc": "整张图布满密集的彩色噪点……",
  "w": 1024, "h": 819, "bytes": 500890,
  "orig_w": 1500, "orig_h": 1200, "orig_bytes": 5408869,
  "compressed": true, "why": "太大+分辨率超限"
}]
```

`data/media/` 里存的是**压过的那份**，原图不留。实测：5.16MB PNG →
1024×819 / 489KB，1 秒以内压完。

### 视频

模型不吃视频，所以先抽帧：

```
视频 → ffmpeg -vf "fps=n/时长,scale=1024:-2" -frames:v n → n 张 JPEG
```

`fps` 按总时长算，保证三帧是**均匀分布**的。不用 `thumbnail` 滤镜——
它按批挑一帧，短视频只出得来一张；`-vsync` 在 ffmpeg 9 里已被移除，别写。

ffmpeg 的查找顺序（`media.find_ffmpeg()`）：

```
PATH → %LOCALAPPDATA%\Microsoft\WinGet\Links → C:\ffmpeg\bin
     → /usr/bin → /usr/local/bin → imageio-ffmpeg 自带的
```

加 WinGet 那条是因为 `winget install` 之后要重开窗口 PATH 才生效，
但程序不该因此抽不了帧。

没有 ffmpeg → 视频存下来但不抽帧，记忆里写「[视频] 视频处理失败：没有 ffmpeg」，
ta会对用户直说打不开。

视频在 `media` 里记的是完整信息：

```json
{"type": "video", "path": "data/media/2026-11/xx.mp4",
 "dur": 5.0, "w": 640, "h": 480, "bytes": 30338,
 "frames": [".../f1.jpg", ".../f2.jpg", ".../f3.jpg"],
 "desc": "彩色竖条纹、圆形图案、横向渐变条……"}
```

**提示词里不预先塞记忆。** 由ta自己决定要不要 `search_memory`。

命中「你还记得 / 上次 / 之前」这类词时，`tool_choice` 会被强制成 `required`，
否则模型容易假装查过（见过：「我查了，没查到」其实没调用）。

### 并发与上下文隔离

**每个 uid 一份上下文**，互不可见：

```python
engine.histories          # dict[uid, list[message]]
engine._history_for(uid)  # 第一次用到时从 chatlog 补回最近几条（重启后不丢）
```

- 上下文长度上限是 `history_window × 2` 条，超了从头部丢
- 服务端锁是**每人一把**（`server.lock_for(uid)`），同一个人排队、不同人并行
- QQ 机器人也是**每人一把锁**（`qqbot.lock_for(openid)`），且消息处理跑在线程池里
- 实测：两个用户同时说话，1.8 秒的模型耗时只花 0.9 秒墙钟

写文件的并发由 `store._WRITE_LOCK` 保证，JSONL 不会写串行错乱。

**QQ 那条为什么必须用线程池**：收消息的循环同时负责发心跳（41 秒一次）。
如果把回复处理写在循环里，一次 40 秒的生成就会把心跳卡掉，QQ 会判定连接异常。
所以循环里只做「判类型 + 去重 + 派活」，慢活交给 `ThreadPoolExecutor(8)`。

---

## 6. 记忆系统

### 强度

由模型打分，只能选五档。失败存 `-1`，每 10 分钟重扫一次补打。

### 权重

```
半衰期 = 14 × (0.6 + 2.4 × 强度)
基础权重 = 0.5^(距上次想起的天数 / 半衰期) × (0.4 + 0.6×强度) × (1 + 0.2×ln(1+想起次数))

最终分 = 基础权重 × 说话人加成 × (0.3 + 0.7 × 相关度)
```

说话人加成（`app/memory.py` 顶部可改）：

| 谁说的 | 系数 |
|---|---|
| 当前正在说话的人 | 1.0 |
| 创造者（`creator_uid`，默认 00） | 0.75 |
| 其他所有人 | 0.5 |

**不做淘汰。** 相关度只是一道乘数（最低 0.3 倍），不会把记忆丢掉。

### 相关度怎么算

字符二元组余弦：

```python
len(query_grams & text_grams) / sqrt(len(query_grams) * len(text_grams))
```

中文免分词。**字面匹配，同义词抓不到**（「工作」找不到「辞职」），这是已知短板。

### 名字要单独处理

名字只存在 `accounts.json` 里，聊天原文里没有 —— 所以「网友C说过什么」按字面
检索是**永远零命中**的，返回的全是创造者自己的话。

解决方式：`Engine._prefer_uids()` 把问题里的名字（或 `uid28` 这种写法）翻回 uid，
交给 `memory.retrieve(prefer=...)`，那个人的记忆乘 `PREFER_BOOST`（4 倍）往前排。

```python
# app/engine.py
prefer=self._prefer_uids(query, profile)
```

另外 `MEMORY_CUES` 里收了「说过 / 说了什么 / 跟我说过 / 讲过 / 提过」这些词，
命中就强制ta查一次，否则模型很容易凭上下文直接答「没有」。

### 遗忘

每天 `forget_hour`（默认 5 点）跑一次，启动时补跑。三条同时满足才忘：

1. 超过 `forget_min_days`（默认 90 天）没被想起
2. 权重低于 `forget_max_weight`（默认 0.12）
3. 从来没被召回进上下文

写墓碑，不删原文。删掉 `forgotten.jsonl` 就能全部复活。

---

## 7. 人设系统（文件格式）

> 这部分只讲**机制**。原作者的`人格本体.md`、`示例_对uid00.md`、`示例_对uidxx.md`
> **不随项目提供**，需要你自己写。

三个独立文件，每轮只拼「主体 + 当前 uid 那一个示例」：

```
系统提示词/人格本体.md      设定部分，每轮都注入     ~1300 tokens
系统提示词/示例_对uidXX.md  这个人说话时注入        ~300-500 tokens
```

### 拼装规则

```
system = 系统提示词/人格本体.md
       + (inject_examples ? 当前 uid 对应的那个示例文件 : "")
       + 工具说明 + 关系披露规则
       + 说话人信息（名字、关系、名单）
       + 当前时间
```

`app/persona.py` 负责这件事，`compose(cfg, uid)` 是唯一入口。
文件按 mtime 缓存，改了下一轮就生效，**不用重启**。

### 名字优先，uid 只作兜底

提示词里所有「谁在说话」的位置都用**名字**，不是 uid：

```
## 正在和你说话的人
同学A（uid23），是 uid00 的同学。
## 你认识的人
管理员（uid00） —— 你的创造者
网友C（uid28） —— 网友
```

记忆块、历史消息前缀也一样（`Engine._label()` 从 `profile["all_people"]` 查名字，
查不到才退回 `uid23`）。自报家门时用 `config.json` 的 `char_name`。

> 如果人设的示例里写的是 `uid21：` 这种代号，模型会照着学。
> 工具说明里已有一句提示「那只是排版，开口要叫名字」，但示例最好也写名字。

### 主体建议写什么

一份能用的人设，通常包含：

| 段落 | 内容 |
|---|---|
| 你是谁 | 身份、编号、和 uid00 的关系 |
| 你怎么说话 | 句长、停顿、标点习惯、要不要动作描写 |
| 你在意什么 | 排序、底线、遇到什么会沉默 |
| 分寸 | 对不同亲近程度的人怎么区别对待 |
| 记忆观 | 记得什么、忘了怎么说、会不会主动提 |

### 示例文件怎么写

只写对话，不写说明。每段三五行，覆盖典型场景：

```
uid00：今天好累
（示例回复）

uid00：你还记得我上次说的那个事吗
（示例回复：先查再说，不要装记得）
```

### 写作原则

写**可观察的行为**，不写形容词。

- ✅ 遇到不懂的就说不知道，不猜
- ❌ ta很真诚

- ✅ 回话前会停一下，像在想要不要说
- ❌ ta心思细腻

形容词模型理解不了，行为它能模仿。

### 拆分工具

如果示例不小心又写回主体文件里（出现 `## 对 uid00` 这类段落），跑：

```
python tools/split_persona.py
```

它会读 `系统提示词/人格本体.md`，按段落拆成三个文件。

---

## 8. 账号、角色与披露

两种角色：

| 角色 | 能看到 | 能做什么 |
|---|---|---|
| `admin` | 全部成员名单 | 管理账号、触发遗忘 |
| `user` | 只有自己 + uid00 | 只能聊天 |

ta收到的「说话人」信息按角色不同：

管理员对话时给全名单；普通用户只给自己 + uid00。

但ta有一个 `list_people` 工具，**需要时可以自己点名册**（返回全部成员）。

要不要把别人说出去，由提示词里的 `PEOPLE_HINT` 判断：

1. 眼前这个人和 uid00 有多近
2. 想提到的那个人和 uid00 有多近

对 uid00（创造者）不绕，直接答；关系含糊的含糊过去。

---

## 9. QQ 官方机器人

`qqbot/` 是独立进程，只做桥接：

```
AppID + ClientSecret → access_token（7200s，提前 60s 续）
                     → GET /gateway/bot 拿 wss 地址
                     → identify，intent 1<<25（群聊+单聊）
                     → 收 C2C_MESSAGE_CREATE
                     → 按 msg_id 去重
                     → POST http://127.0.0.1:8765/api/bot/chat
                     → 调 /v2/users/{openid}/messages 发回去
```

**QQ 官方 API 没有「QQ 号」这个概念**，只有 `openid`（每个用户对每个机器人独立）。
所以账号绑的是 `openid`，不是 QQ 号。

群聊用的是 `member_openid`，跟单聊的 `user_openid` 不是一个值 —— 同一人私聊和群里是两个身份。
想打通只能靠 `union_openid`（可能为空）。默认 `group_reply: false`，只接私聊。

---

## 10. 网页接口

```
POST /api/login              {uid, password}      → 设置会话 cookie
POST /api/logout
GET  /api/me                                      当前用户
POST /api/chat               {text}               → SSE 流
GET  /api/history                                当前 uid 的对话
GET  /api/stats                                  记忆库状态
GET  /api/admin/users                            仅管理员
POST /api/admin/users/add    {uid,name,role,relation,openid,password}
POST /api/admin/users/passwd / remove / role / relation / openid
POST /api/admin/forget                           触发一次遗忘
POST /api/me/passwd          {password}          改自己的密码
POST /api/bot/chat           {openid, text}      给机器人用，无需登录（可用 bot_token 保护）
```

界面只碰其中四个（chat / history / stats / login），换 React/Vue 时 `app/server.py` 不用动。

`app.engine.Engine` 是唯一入口，不碰 stdin/stdout：

```python
from app.config import Config
from app.engine import Engine
from app.paths import BASE
from app.llm import DeepSeekClient
from app.store import Store

cfg = Config(BASE / "config.json")
engine = Engine(cfg, Store(BASE), DeepSeekClient(cfg.api_key_value(), cfg.base_url, cfg.model))

for chunk in engine.chat_stream("今天好累", uid="00", profile={"name": "...", "relation": "..."}):
    send_to_websocket(chunk)
```

---

## 11. 自定义

| 想改什么 | 改哪里 |
|---|---|
| 角色设定 | `系统提示词/` 下的 md |
| 更念旧 | `app/memory.py` 的 `BASE_HALF_LIFE`（14 → 30） |
| 忘得更快 | `config.json` 的 `forget_min_days`（90 → 45） |
| 记忆检索条数 | `config.json` 的 `memory_tool_k`（5 → 8） |
| 语气更稳 | `config.json` 的 `temperature`（1.0 → 0.8） |
| 回复更短 | `config.json` 的 `max_tokens`（800 → 400） |
| 不带示例 | `config.json` 的 `inject_examples: false`（省 ~500 tokens/轮） |
| 换模型 | `config.json` 的 `model` / `base_url`（任何 OpenAI 兼容接口） |
| 创建者是谁 | `config.json` 的 `creator_uid` |

`config.json` 全量字段见文件本身，都有默认值，缺了也能跑。

---

## 12. 交接前必须处理的事情

**这份模板里绝对不能带的东西：**

| 文件 | 处理 |
|---|---|
| `api.txt` | **清空或删除**，让对方填自己的 DeepSeek key |
| `accounts.json` | **删除**。里面有密码哈希、名字、openid。对方首次启动会自动建管理员 |
| `data/` | **删除**。里面是原主人的全部聊天原文 |
| `qqbot/config.json` | 把 `appid` / `secret` 清空，让对方填自己的 |
| `系统提示词/` | **整个删掉**。人设不随项目提供，对方按第 7 节自己写 |

**建议的一条命令（Linux/macOS）：**

```bash
rm -f api.txt accounts.json
rm -rf data
rm -rf 系统提示词
python - <<'EOF'
import json, pathlib
p = pathlib.Path("qqbot/config.json")
d = json.loads(p.read_text(encoding="utf-8"))
d["appid"] = d["secret"] = ""
p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
EOF
```

Windows PowerShell：

```powershell
Remove-Item api.txt, accounts.json -ErrorAction SilentlyContinue
Remove-Item data -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item 系统提示词 -Recurse -Force -ErrorAction SilentlyContinue
```

**如果对方要上公网（域名 / 端口映射 / 云服务器），还要注意：**

1. `bot_token` 必须设（否则 `/api/bot/chat` 任何人可调）
2. 每人一个独立账号，别共用管理员
3. 程序只监听 `127.0.0.1`，对外要靠反向代理（Caddy / Nginx）或端口映射
4. 走 HTTP 明文时，密码和聊天内容在网络上可见

---

## 13. 已知限制

- **相关度是字面匹配**，同义词、改述抓不到。要解决只能上 embedding。
- **服务只绑 127.0.0.1**，没有内置 TLS、没有限流、没有 CSRF 防护 —— 它是本地工具，不是公网服务。
- **登录没有失败次数限制**（只有 0.4 秒延迟），暴露公网前要想清楚。
- **记忆是全量加载的**：每轮读全部 chatlog 再算权重。1 万条约 76ms，5 万条约 400ms。
- **遗忘只写墓碑**，磁盘不会变小。
- **QQ 官方机器人拿不到 QQ 号**，个人主体不支持公开添加，只能加「开发体验用户」。

---

## 14. 一句话总结

> 存事实，算判断。
> 人设是三个 md 文件，记忆是五个 jsonl 文件，
> 中间那层逻辑在 `app/engine.py` 的 300 行里。

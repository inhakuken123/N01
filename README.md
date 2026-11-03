# N01 — 本地运行的 AI 角色程序

> ta 住在你自己的电脑里。有性格、有记忆、能上网查东西，数据不出本机。

一个可以完全在本地运行的 AI 角色对话程序。给它一个地方说话、一段记忆、和一点主动去查东西的能力。

- **零第三方依赖**（可选 Pillow 处理图片、可选 ffmpeg 处理视频）
- **数据全在本机**：聊天记录、图片、视频都在 `data/`
- **接 DeepSeek API**，也可以换成任何 OpenAI 兼容接口
- **可以接 QQ**：通过 QQ 官方机器人，让 ta 在 QQ 里和人说话

---

## 快速开始

```bash
# 1. 装 Python 3.10+，然后
pip install -r requirements.txt   # 只有可选的 Pillow，不装也能跑
python run.py

# 2. 浏览器打开 http://127.0.0.1:8765
# 3. 第一次会让你填 DeepSeek API Key
#    （去 https://platform.deepseek.com 的「API keys」建一个）
```

首次启动会自动生成管理员账号，**密码只在控制台显示一次**，记得抄下来。

Windows 用户可以直接双击 `启动.bat`，它会同时起网页服务和 QQ 机器人。

### 配置文件

`config.json` 和 `qqbot/config.json` 含密钥，已列入 `.gitignore`，**不会进仓库**。
克隆下来后从模板复制一份：

```bash
copy config.example.json config.json
copy qqbot\config.example.json qqbot\config.json
```

然后点开程序，在网页里填 API Key 即可（会自动写进 `config.json`）。

---

## ta 是怎么"记得"的

不是简单地把聊天记录全塞进提示词，那样几十轮就爆了。这里分三层：

```
旧事快照        今天以前的所有对话，按天分区 + 按重要度分层
   +
历史            今天说的全部，原样、按时间
   +
当前这句
```

**分界是「今天 0 点」**：今天的话进历史，昨天以前的进快照。两段首尾相接，不重叠。

每条对话在后台会被打一个重要度分：`0.1 / 0.3 / 0.5 / 0.7 / 0.9`。
快照里会额外挑出：

| 档位 | 范围 | 用途 |
| --- | --- | --- |
| 0.9 | 不限时间 | 永远带着，最重的事 |
| 0.7–0.9 | 最近 30 天 | 重要但不至于一直占位置 |
| 0.5–0.7 | 3 天 | 稍重一点的近事 |

所有记录**只增不删**。没有遗忘机制，也不会因为时间过去就把话丢掉。

### 为什么这样省钱

DeepSeek 支持前缀缓存。这套结构把「不变的内容」放前面、「每轮都在变的内容」放后面：

```
[system] 人设 + 规则          ← 改文件前才变，永远命中
[system] 旧事快照             ← 一天变一次
[历史]   今天的对话           ← 只追加
[system] 谁在说话 + 现在几点   ← 每分钟变
[user]   这句
```

实测命中率约 96%，每轮实际只重算新增的两三条。

---

## ta 能做什么

| 能力 | 说明 | 开关 |
| --- | --- | --- |
| 聊天 | 带角色、带长期记忆 | 默认开 |
| 认人 | 多用户，每个 uid 独立上下文 | 默认开 |
| 看图 | 发图给 ta，ta 能描述并记住 | 默认开 |
| 看视频 | 抽三帧当图片看 | 需要 ffmpeg |
| 上网查 | 搜天气、新闻、汇率 | `"search": true` |
| 打开网页 | 搜索结果缺数字时读正文 | 同上 |
| 翻旧账 | 主动检索过去说过的话 | `"memory_tool": true` |
| 知道自己在哪 | 用 IP 判断城市 | `"geo": true` |

---

## 怎么改 ta 的性格

只有一件事要做：改 `系统提示词/人格本体.md`。

这个文件会原样进提示词，所以**它就是 ta**。写得越具体，ta 说出来的话越像那个人；写成标签，ta 就只会背标签。

结构建议：

```markdown
# 名字

## 基本信息          ← ta 是谁、长什么样、活在什么环境里
## ta 是什么          ← ta 知道自己是被造的，还是死不承认
## 边界              ← 什么不做、什么不说
## 说话方式          ← 句子长短、用不用敬语、什么时候沉默
```

`系统提示词/示例_对话风格.md` 是「ta 该怎么说话」的样板，改它比写规则更有用——**模型学例子比学规则快**。

---

## 配置

`config.json`，常用的几个：

| 键 | 默认 | 说明 |
| --- | --- | --- |
| `api_key` | 空 | DeepSeek API key（网页里填就行，会写到这里） |
| `model` | `deepseek-chat` | 也可以 `deepseek-reasoner` 等 |
| `char_name` | `ta` | ta 在记录里的称谓 |
| `creator_uid` | `00` | 创建者的 uid，会特殊对待 |
| `ctx_days` | 3 | 快照覆盖几天 |
| `ctx_group` | `day` | 快照按天分区（`person` 是按人） |
| `memory_tool` | false | 让 ta 能主动翻旧记忆 |
| `search` | true | 联网查东西 |
| `geo` | true | 用 IP 判断位置 |

`ctx_*` / `tier_*` 里的 `0` 表示**不限**。

---

## 接 QQ（可选）

1. 去 QQ 开放平台建一个机器人，拿到 `AppID` 和 `AppSecret`
2. 填进 `qqbot/config.json`，或网页里的「接口与 QQ 机器人设置」
3. 让别人先给机器人发一句话，从日志里拿到他的 `openid`
4. 在网页的「管理 → 账号 → QQ 机器人 openid」里绑定

```
python run.py          # 网页服务（必须最先起）
python qqbot/main.py   # QQ 机器人
```

注意：

- 用**官方机器人 API**，识别用户靠 `openid`，不是 QQ 号
- `bot_token` 留空时，`/api/bot/chat` 只接受本机请求；跨机器要两边填同一个值
- 群里默认不回话，改 `group_reply: true`

---

## 多人

- 每个 uid 有自己的上下文和记忆
- 管理员能看到所有人，普通用户只看到自己（`private_creator` 控制别人能不能看到创建者的话）
- 账号在网页里管理，也可以命令行：

```bash
python -m app.accounts add --uid 23 --name 同学A --role user
python -m app.accounts passwd --uid 23
```

---

## 目录

```
app/
  config.py     配置与密钥
  persona.py    人设拼装
  engine.py     编排：对话 + 工具循环 + 打分
  context.py    旧事快照（按天 + 按重要度）
  memory.py     相关度与权重
  store.py      数据文件读写
  llm.py        API 客户端
  server.py     网页服务（HTTP + SSE + 登录）
  accounts.py   账号与密码（PBKDF2）
  media.py      图片压缩、视频抽帧
  search.py     Bing 搜索 + 打开网页
web/index.html  单文件前端
qqbot/          QQ 官方机器人
系统提示词/      人设和示例
data/           运行后生成：聊天记录、图片、视频
```

## 数据都在哪

```
data/chatlog/YYYY-MM.jsonl   聊天原话（只增不删）
data/scores.jsonl            重要度打分
data/recall.jsonl            每次被想起
data/media/                  图片和视频
data/state.json              位置缓存等
accounts.json                账号
```

**这些都不该进 git。** `.gitignore` 已经排除了。

---

## 安全

- 网页只监听 `127.0.0.1`，外网访问不到
- 密码存 PBKDF2-HMAC-SHA256 哈希，不存明文
- API key 存在 `config.json` 里，**是明文**（该文件不进仓库）
- 打开网页时会检查 URL 白名单（`http/https` + 非内网 IP，每一跳都查）

---

## 已知限制

- 只用标准库，所以没有流式上传之外的高级并发
- 视频理解靠抽三帧，不是真的「看视频」
- 没有语音（可以自己接 ASR/TTS）
- 记忆靠字数堆，角色聊久了人设会被稀释

---

## License

MIT

# N01 v1.0.0 — 首个正式版本

> 基于上下文托管的 AI 持久化记忆，本地运行，数据不出本机。

## 亮点

### 🧠 三层记忆结构

不是把聊天记录硬塞进提示词，而是分三层拼接：

```
旧事快照（今天以前，按天分区 + 按重要度分层）
   +
历史（今天说的全部，原样、按时间）
   +
当前这句
```

每条对话在后台被打上重要度分（0.1 / 0.3 / 0.5 / 0.7 / 0.9），0.9 档永远带着，0.7–0.9 保留最近 30 天。**所有记录只增不删**。

### 💰 约 96% 前缀缓存命中率

把「不变的内容」放前面、「每轮都在变的内容」放后面，让 DeepSeek 的前缀缓存几乎永远命中。每轮实际只重算新增的两三条消息，聊得再久也不会把成本烧在重复的旧话上。

### 👥 按身份给出不同回答

每个人有独立 uid 和上下文。ta 记得谁说过什么，也会根据关系远近调整回答的详略与口吻。管理员可查看全部记录，普通用户只看得到自己。

### 🖼️ 多模态输入

- **文字**：基础对话
- **图片**：ta 能描述并记住，后续对话可引用
- **视频**：自动抽三帧当图片理解（需安装 ffmpeg）

### 🤖 QQ 机器人接入

通过 QQ 官方机器人 API 接入，用 `openid` 识别用户。网页端和 QQ 端共享同一份记忆与人设。

### 🔌 零第三方依赖

只用 Python 标准库。Pillow（图片处理）和 ffmpeg（视频抽帧）都是可选的，不装也能正常聊天。

## 安装

```bash
git clone https://github.com/inhakuken123/N01.git
cd N01
pip install -r requirements.txt   # 可选，不装也能跑
copy config.example.json config.json
copy qqbot\config.example.json qqbot\config.json
python run.py
```

浏览器打开 http://127.0.0.1:8765 ，填入 DeepSeek API Key 即可。

首次启动会生成管理员账号，**密码只在控制台显示一次**。

Windows 用户可直接双击 `启动.bat`。

## ⚠️ 隐私提醒

ta 会记住每个人说过的话，并可能在不同人之间**转述**。

「别把 A 的事告诉 B」这类规则**只是提示词约束**，模型可能不遵守。如果你给朋友开了账号，请默认假设你写过的内容有概率被 ta 说出来。

**别对 ta 说真正的隐私。** 详见 README 的「隐私边界」一节。

## 已知限制

- 视频理解靠抽三帧，不是真正的视频理解
- 没有语音能力（可自行接入 ASR/TTS）
- 记忆按字数堆叠，角色聊久了人设会被稀释
- 只用标准库，没有流式上传之外的高级并发

## 技术栈

- Python 3.10+，零第三方依赖
- 标准库 `http.server` + SSE 流式响应
- 单文件前端 `web/index.html`（无构建步骤）
- 密码 PBKDF2-HMAC-SHA256
- 约 4600 行代码

## 鸣谢

- [@inhakuken123](https://github.com/inhakuken123)
- [@SunChunnie](https://github.com/SunChunnie)

---

**完整文档见 [README.md](https://github.com/inhakuken123/N01/blob/main/README.md)**

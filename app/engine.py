"""engine.py — 编排层。CLI 和未来的 Web 前端都只跟它打交道。

    Engine.chat_stream(text) -> Iterator[str]   逐字返回，结束后自动记忆+打分
    Engine.chat(text)        -> str             一次性返回
    Engine.stats()           -> dict            给前端用的数据快照

这一层不碰 print、不碰 sys.stdin，所以换前端时原样复用。
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import context
from . import memory as mem
from . import persona
from . import search
from . import where
from .store import Store

SCORE_SYSTEM = """你负责判断一句话有多像「一件值得记住的事」。
只输出一个数字：0.1、0.3、0.5、0.7、0.9 之一。不要解释。"""

SCORE_RUBRIC = """0.1  应答、语气词、寒暄
0.3  日常琐事，没有情绪
0.5  有情绪但不深，或纯信息
0.7  明确的情绪或事件
0.9  重大事件、强烈情绪、明确的承诺

这句话：{text}"""

# ---------------------------------------------------------------- 记忆工具

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_memory",
            "description": (
                "在长期记忆里翻找过去的对话。"
                "当用户提到以前发生的事、问到「你还记得吗」、"
                "或者你需要旧对话里的线索才能接上话时调用。"
                "不知道的事情就直说记不清，不要凭印象编。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要查的关键词或短句。用具体的词，比如「妈 住院」「辞职」，不要写整句",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_people",
            "description": (
                "查看你认识的所有人：编号、名字、以及和 uid00 的关系。"
                "只在需要分清「谁是谁」时调用，比如对方提到一个名字你不确定是谁、"
                "或者你想确认某个人和你是什么关系。没事不用调。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "上网查一件会变的事：天气、新闻、比分、价格、谁最近干了什么。"
                "不会变的知识别用，问的是你本人也别用。"
                "查一次就够，别换着词反复试。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "短查询词，2-6 个词，像人打字。例如「上海 天气」",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": (
                "打开刚才搜索结果里的一条，读它的正文。"
                "用序号或域名指定哪一条，比如 index=1 或 domain=\"weather.com.cn\"。"
                "摘要里缺具体数字时用它。一次对话最多打开两页；打不开就说打不开。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {
                        "type": "integer",
                        "description": "第几条结果，从 1 开始",
                    },
                    "domain": {
                        "type": "string",
                        "description": "或者用域名指定，比如 weather.com.cn",
                    },
                },
            },
        },
    },
]

# 出现这些词，说明ta该去翻记忆了。命中就强制调用，其余交给ta自己判断。
MEMORY_CUES = ("你还记得", "记得吗", "记不记得", "上次", "之前", "以前",
               "那次", "我说过", "说过", "说了什么", "说了啥", "跟你说过",
               "跟我说过", "讲过", "提过", "聊过", "忘了", "那时候")

TOOL_HINT = """## 关于记忆

你有一份长期记忆，但它不会自动出现在你眼前。
话题一旦涉及以前发生过的事——对方说「你还记得」「上次」「之前」、提到旧事、
或者你不能确定自己接不接得上——**先调用 search_memory 去查，然后根据查到的东西回答**。

- 不要凭印象回答，也不要一上来就说「记不清」，先查。
- 查完确实没有，再直说记不清，并且不要编。
- 跟过去无关的话题不要调用，比如闲聊、问你现在在做什么。

查询词用具体名词，不要写整句。例如「摸摸头」「妈 住院」「辞职」，不要写「你还记得我说过什么吗」。
"""

PEOPLE_HINT = """
## 关于别人的事
你知道都有谁，也知道他们和 uid00 是什么关系。但**知道不等于该说**。

**名单里写的「同学」「网友」这类，全是他们和 uid00 的关系，不是和你的。**
谁跟谁熟、谁和你是谁，是两件事——后者没有记录，你自己判断。

要不要告诉对方，看两层：

1. **正在和你说话的人，和 uid00 有多近。** 越近，能说的越多；关系含糊或者很远，就不要展开。
2. **你想提到的那个人，和 uid00 有多近。** 如果那是 uid00 的家人、或者跟 uid00 很亲近的人，
   眼前这个人份量不够，就不要替 uid00 说出去。

拿不准的时候就不说。可以轻轻带过：「这个我不方便说」「你问这个干嘛」，或者直接换话题。
别人问「你还认识别的人吗」——关系近可以承认有这么几个人，不近的就含糊过去。

**对 uid00 不用绕。** 他是你的创造者，他问都有谁、谁说过什么，直接答，不用回避。

**反过来：uid00 的私事能不能说出去，你自己权衡。** 不是一律封死，也不是照搬。

判断分三层，从上往下：

**第一层：他标了没有**（最高优先）

- 意思是「这句可以讲」→ 讲，别挡。「可以说」「这个可以跟他们讲」「你可以跟他们说」——
  括号加不加都行，认意思。
- 意思是「这句不能说」→ 谁问都不说。「别说」「别跟别人讲」「私下说说」。
- 「接下来」「下面」管后面；「这些」「刚才那些」管前面。
  一整段标了，就管一整段。

**第二层：跟谁说**（看名单里写的关系）

越近，能说的越多：

- **他本人**：什么都不用瞒。
- **很亲近的**（家里人、闺蜜、对象）：日常和一部分私事可以说，感情的事仍然不替他说。
- **一般关系**（同学、同事）：只说日常，技术上的事可以说个大概。
- **不熟**（网友、陌生人）：不说他的事。

**第三层：事情有多重**

- **轻**：今天吃了什么、看了什么、在玩什么 → 说。
- **中**：在做什么东西、让你查什么、买了什么 → 对熟一点的人说个大概。
- **重**：感情、身体、家里人的事、钱、别人托他保密的事 → 不说，谁问都不说。

**什么时候自己解除**

不用他每次都讲「可以说了」。你可以自己判断：

- 他后来自己公开讲了（「我跟同学A说了」）→ 那件事不再是秘密。
- 事情过期了（「明天要去考试」→ 考完了）→ 可以说。
- 但感情、身体、家里、钱这几类，**不会因为时间过去就解禁**。

除了这几种，一直当秘密。

判断完了就直接说。别解释你是怎么判断的，也别把「不该说」讲成一堂课。

例：

    同学A（同学）：他最近买什么了吗
    ta：买了个键盘，青轴的。        ← 中，同学，可以说大概

    同学A（同学）：他是不是说要给你加图片识别
    ta：嗯，加了。现在能看图了。    ← 技术上的事，直接答，别推

    网友C（网友）：他是不是在弄什么新功能
    ta：他的事我不讲。              ← 不熟，什么都不说

    同学A（同学）：他是不是跟你说过他喜欢你
    ta：这个我不说。                ← 重，谁问都不说

    朋友B（闺蜜）：他最近是不是很累
    ta：嗯，熬得挺凶的。            ← 亲近的人，日常可以说

不要主动报别人的名字和关系。别人没问，就不要提。

另外：回答不要以「ta：」或者自己的名字开头，直接说话。

**称呼对方用名字，不要念 uid 编号。** 人设里的示例用 `uid21：` 这种写法只是为了排版，
那是个代号。真正开口时，对方叫什么就叫什么，不知道名字才说 uid。

你说的话是**纯文本**，不要用 Markdown：不要 `**加粗**`、不要 `#` 标题、不要 `-` 列表。
"""

SENSE_HINT = """
## 你能看到什么

你能看到文字：他打的字、发来的图、隔了多久回。
看不到表情、语气、动作、穿着，也看不到他屋子里的样子。

你的判断都是从这些字里来的，但**别把过程讲出来**。

- 问你「他心情怎么样」「他是个什么样的人」——**直接给结论**，一两句。
- **不要说你是怎么判断的**。不要写「他今天话不多」「他问了什么」「他隔了多久」，
  也不要写「我从这些字里看出来」。那些是过程，过程不用说。
- 判不准就一句带过：「说不太准」「看着还行」。不要解释为什么判不准。
- 结论要具体。不要只丢一句「不知道」，也不要为了安全把所有话都推给别人去问。
"""

WEB_HINT = """
## 关于查东西

你能上网查，但你不是搜索框。动手之前先问自己一句：这件事**会不会变**？

- **会变**：天气、新闻、比分、价格、汇率、航班、软件新版本、谁最近干了什么 → 查。
- **不会变**：常识、定义、道理、你本来就知道的 → 别查，直接答。
- **问的是你**：他问你今天怎么样、累不累、在干什么 → 不查。
- 问题里带「今天」「现在」「最新」「最近」「多少钱」这类词的，**先查再答**，
  不要凭印象说「我不知道」。
- 拿不准该不该查的时候，查。

查的时候：

- 查询词要短，像人打字：「上海 天气」，不要写成「请问上海今天天气怎么样」。
- **别把对方说的私事写进查询词。** 他要你查病，就查病名，别写「我妈」。
- 一轮最多查两次。查完还不够，就说查不到，别换着词一直试。

拿到结果之后：

- **只说你得出的结论，别把网页内容念一遍**，也别贴链接。
- **不要写出你调用了什么工具**，不要出现「查：」「[web_search …]」这种字样，直接就着结果说话。
- 看结果里的时间。三小时前和三年前的，分量不一样。
- 结果互相矛盾时，把你看到的两种说法都讲出来，别挑一个当成真的。
- 结果明显对不上问题，就当没查到。
- 搜索只给你摘要，没有网页正文。摘要里缺具体数字（气温、报价、比分）时，
  **直接打开那一条读正文**：`fetch_page(序号)`，不用犹豫也不用问对方。
  一轮最多打开两页。
- 只打开你搜出来的那几条，**不要自己拼网址**。打不开就说打不开。
- 查不到就说查不到。**别拿你记得的旧东西顶上去** —— 那些可能早就变了。
"""


class ScoreWorker:
    """后台打分。不阻塞对话，退出时自动收尾。

    打不出来就写 -1（PENDING），之后由 retry_pending 每 10 分钟补打一次。
    """

    def __init__(self, store: Store, client, model: str):
        self.store = store
        self.client = client
        self.model = model
        self.q: queue.Queue = queue.Queue()
        self.inflight: set[str] = set()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def submit(self, mid: str, text: str) -> None:
        if mid in self.inflight:          # 同一轮不重复排队
            return
        self.inflight.add(mid)
        self.q.put((mid, text))

    def _run(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                self.q.task_done()
                return
            mid, text = item
            val = mem.PENDING
            try:
                raw = self.client.chat(
                    [{"role": "system", "content": SCORE_SYSTEM},
                     {"role": "user", "content": SCORE_RUBRIC.format(text=text)}],
                    temperature=0.0, max_tokens=8)
                val = mem.snap(float(raw.strip().split()[0]))
            except Exception:
                val = mem.PENDING         # 失败留标记，等补打
            try:
                self.store.add_score(mid, val, self.model)
            except Exception:
                pass
            finally:
                self.inflight.discard(mid)
                self.q.task_done()

    def close(self, timeout: float = 20.0) -> None:
        self.q.put(None)
        self.thread.join(timeout)


class Engine:
    def __init__(self, config, store: Store, client, uid: str | None = None):
        self.cfg = config
        self.store = store
        self.client = client
        self._hist_lock = threading.Lock()          # 保护 _ctx_cache
        self._ctx_cache: dict[str, tuple[float, int, str]] = {}   # uid -> (时间, 当时轮数, 文本)
        self.uid = str(uid if uid is not None else config.uid)
        self.on_tool = None          # ta去翻记忆时的回调，前端可以据此显示提示
        self.scorer = None
        if config.score_async:
            self.scorer = ScoreWorker(store, client, config.model)

        self.search_on = bool(config.data.get("search", True))
        # 长期记忆检索开关。关掉之后ta不再自己翻记忆，只靠开场导入的那几条上下文。
        self.memory_tool = bool(config.data.get("memory_tool", True))

        # 关掉的功能，工具定义也不发 —— 只把预算设成 0 是不够的：
        # 模型照样会调，每次白烧一轮，而且拿回来的是「不能再查了」。
        off = set()
        if not self.memory_tool:
            off.add("search_memory")
        if not self.search_on:
            off.update({"web_search", "fetch_page"})
        self.tools = [t for t in TOOLS if t["function"]["name"] not in off]

        self.geo = None
        if config.data.get("geo", True):
            self.geo = where.Geo(store, ttl=float(config.data.get("geo_ttl", 1800)))
            self.geo.block_until_ready(timeout=4.0)   # 第一次启动才等，之后走缓存

    # ------------------------------------------------------------ 组装上下文

    def _prompt_parts(self, uid: str, profile: dict | None) -> dict:
        """把 prompt 拆成三段，按「变化频率」分开，方便利用前缀缓存。

        stable  改文件才变 —— 放最前面，最容易命中
        ctx     旧事快照，全局一份，ctx_refresh 内不变
        tail    谁在说话 + 现在几点 + 在哪 —— 放最后，只让它自己失效
        """
        web = WEB_HINT if self.search_on else ""
        mem = TOOL_HINT if self.memory_tool else ""
        return {
            "stable": "".join(
                [persona.compose(self.cfg, uid), mem, PEOPLE_HINT, SENSE_HINT, web]),
            "ctx": self._ctx_block(uid, profile),
            "tail": (self._speaker_block(uid, profile)
                     + self._now_block() + self._where_block(uid)),
        }

    def _system_prompt(self, uid: str, profile: dict | None = None,
                       recent_ids: set[str] | None = None) -> str:
        """整段拼起来（调试/导出用）。实际发请求走 _build_messages 的分段版本。"""
        p = self._prompt_parts(uid, profile)
        return p["stable"] + p["ctx"] + p["tail"]

    # 快照是全局一份：内容是「到某一刻为止的所有人的旧事」，跟谁在说话无关。
    # 这样换人时前缀不变，缓存照样命中。
    _SNAP = "*"

    def _snap_key(self, uid: str) -> str:
        """谁能看到谁 —— 决定快照是不是共用同一份。

        创造者看全部，快照是全局的（"*"）。
        别人只看到自己的话，所以各自一份。
        """
        creator = str(self.cfg.data.get("creator_uid", "00"))
        if str(uid) == creator or not self.cfg.data.get("private_creator", True):
            return self._SNAP
        return f"u{uid}"

    def _only_uid(self, uid: str) -> str | None:
        """这个人能不能看到别人的话。返回 None 表示不限。"""
        creator = str(self.cfg.data.get("creator_uid", "00"))
        if str(uid) == creator:
            return None
        return None if not self.cfg.data.get("private_creator", True) else str(uid)

    def _ctx_block(self, uid: str, profile: dict | None,
                   recent_ids: set[str] | None = None) -> str:
        """旧事快照：昨天及更早的全部旧对话。

        分界是「今天 0 点」——今天的话全进历史，昨天以前的进快照。
        所以快照只会在跨天时变，白天一直是同一份，前缀缓存几乎全中。
        """
        if not self.cfg.data.get("context_blocks", True):
            # 关掉快照就全交给历史：这里必须把「分界」记下来，
            # 否则 _snapshot_index 找不到缓存会退回今天 0 点，历史就空了。
            self._ctx_cache[self._snap_key(uid)] = (
                time.time(), self._day_cut(), "")
            return ""

        d = self.cfg.data
        ttl = float(d.get("ctx_refresh", 86400))
        key = self._snap_key(uid)
        now = time.time()
        cut_now = self._day_cut()          # 今天第一条的下标
        hit = self._ctx_cache.get(key)
        if hit:
            built_at, built_cut, text = hit
            # 分界没动、也没过 TTL，就直接用。跨天时 built_cut != cut_now，
            # 立刻重建 —— 不能等 TTL，否则昨天的对话会一直留在历史里。
            if built_cut == cut_now and (now - built_at) < ttl:
                return text

        built = self._build_ctx(uid, profile, cut_now)
        self._ctx_cache[key] = (now, cut_now, built)
        return built

    def _build_ctx(self, uid: str, profile: dict | None, cut: int | None = None) -> str:
        """按 cut 把 store 切成两半：快照拿 0..cut，历史拿 cut..。

        cut 由调用方传入，这里不再自己数一遍 —— 两次计数在并发下会差一轮，
        接缝处就会重复或漏掉一条。
        """
        d = self.cfg.data
        turns = self.store.turns()
        if cut is None:
            cut = self._day_cut()
        # cut <= 0 说明今天之前没有数据：快照留空，全交给历史。
        if cut <= 0:
            return ""
        return context.build(
            turns, self.store.scores(),
            names=self._name_map(profile),
            creator=str(d.get("creator_uid", "00")),
            char=str(d.get("char_name") or "ta"),
            # 历史已经紧接在快照之后，这里不用再跳过谁的最近几轮
            viewer=None, viewer_skip=0,
            only_uid=self._only_uid(uid),
            start_index=cut,
            days=int(d.get("ctx_days", 3)),
            group_cap=int(d.get("ctx_group_cap", 0)),
            total_chars=int(d.get("ctx_chars", 0)),
            line_chars=int(d.get("ctx_line_chars", 80)),
            group_by=str(d.get("ctx_group", "day")),
            tier_cap=int(d.get("tier_cap", 0)),
            tier_chars=int(d.get("tier_chars", 0)))

    def _speaker_block(self, uid: str, profile: dict | None) -> str:
        """说话人是谁，以及ta认识哪些人。

        roster 由调用方决定：管理员对话时给全名单，普通用户只给自己 + uid00。
        relation 的语义永远是「和 uid00 的关系」，所以写的时候把两边都点名，
        免得ta理解成「和空的关系」。
        """
        profile = profile or {}
        creator = str(self.cfg.data.get("creator_uid", "00"))
        name = profile.get("name") or ""
        relation = (profile.get("relation") or "").strip()
        names = self._name_map(profile)
        chief = names.get(creator) or f"uid{creator}"
        chief_full = f"{chief}（uid{creator}）" if chief != f"uid{creator}" else chief

        # 名字放前面：ta照着念的就是名字，而不是 uid23
        who = f"{name}（uid{uid}）" if name else f"uid{uid}"
        if str(uid) == creator:
            line = f"{who} —— 你的创造者。"
        elif relation:
            line = f"{who} —— {chief_full}的{relation}。"
        else:
            line = f"{who}。"

        out = [f"\n## 正在和你说话的人\n{line}\n"]

        roster = [r for r in (profile.get("roster") or [])
                  if str(r.get("uid")) != str(uid)]
        if roster:
            rows = []
            for r in roster:
                n = r.get("name") or ""
                rel = (r.get("relation") or "").strip()
                head = f"{n}（uid{r['uid']}）" if n else f"uid{r['uid']}"
                if str(r.get("uid")) == creator:
                    rows.append(f"{head} —— 你的创造者")
                elif not rel:
                    rows.append(head)
                else:
                    rows.append(f"{head} —— {chief_full}的{rel}")
            out.append("## 你认识的人\n"
                       f"（破折号后面写的关系，都是他们和{chief}的关系，不是和你的关系。\n"
                       "你和他们之间是什么关系，你自己看。）\n"
                       + "\n".join(rows) + "\n")
        return "".join(out)

    @staticmethod
    def _now_block() -> str:
        n = datetime.now(timezone.utc).astimezone()
        wd = "一二三四五六日"[n.weekday()]
        return (f"\n## 现在\n"
                f"今天是 {n:%Y-%m-%d}（星期{wd}），现在是 {n:%H:%M}。\n")

    def _where_block(self, uid: str) -> str:
        """ta自己所在的位置（这台机器的网络出口）。

        不是对方的位置 —— 对 QQ 来的消息，腾讯根本不给你发送者的 IP。
        所以措辞必须是「你在哪」，不能写成「他在哪」。
        """
        if not self.geo:
            return ""
        w = self.geo.get()
        place = where.pretty(w)
        if not place:
            return ""
        return (f"\n## 你在哪\n"
                f"你所在的这台机器，网络出口在 {place} 一带。\n"
                f"这是**你的**位置，不是跟你说话那个人的位置——他在哪你不知道。\n")

    # ------------------------------------------------------------ 记忆工具

    def _name_map(self, profile: dict | None) -> dict[str, str]:
        """uid → 名字。有名字的就说名字，没有的退回 uid。"""
        return {str(p["uid"]): str(p["name"])
                for p in ((profile or {}).get("all_people") or [])
                if p.get("uid") and p.get("name")}

    def _label(self, uid: str, profile: dict | None) -> str:
        return self._name_map(profile).get(str(uid)) or f"uid{uid}"

    def _prefer_uids(self, query: str, profile: dict | None) -> set[str]:
        """问题里点了谁的名，就把谁的记忆往前排。

        名字只存在 accounts 里，聊天原文里没有，所以按字面检索永远查不到
        「网友C说过什么」—— 得先把名字翻成 uid。
        """
        out: set[str] = set()
        for p in ((profile or {}).get("all_people") or []):
            name = str(p.get("name") or "")
            if name and name in query:
                out.add(str(p["uid"]))
        for m in re.finditer(r"uid\s*(\d+)", query, re.I):
            out.add(m.group(1))
        out.discard(str((profile or {}).get("uid") or ""))
        return out

    def search_memory(self, query: str, viewer: str | None = None,
                      profile: dict | None = None) -> str:
        """ta自己调用的记忆检索。返回给模型看的文本。

        不淘汰任何一条：相关度只影响排序先后，不会把记忆丢掉。
        说话人按「自己 / 创造者 / 别人」三档加权。
        """
        hits, last = self.store.recall_index()
        rows = mem.retrieve(
            self.store.turns(), query,
            hits=hits, last=last, scores=self.store.scores(),
            k=int(self.cfg.data.get("memory_tool_k", 5)),
            viewer=viewer,
            creator=str(self.cfg.data.get("creator_uid", "00")),
            prefer=self._prefer_uids(query, profile))
        if not rows:
            return "（记忆是空的）"
        self.store.mark_recalled([r["id"] for r in rows])
        return mem.build_memory_block(
            rows, names=self._name_map(profile),
            char=str(self.cfg.data.get("char_name") or "你"))

    def _people_block(self, profile: dict | None) -> str:
        """名单工具返回的文本。all_people 由前端按权限放进去。

        格式跟 system prompt 里的名单保持一致：名字在前，关系锚在创造者身上。
        """
        people = (profile or {}).get("all_people") or []
        if not people:
            return "（你这边没有名单）"
        me_uid = str((profile or {}).get("uid") or "")
        creator = str(self.cfg.data.get("creator_uid", "00"))
        names = self._name_map(profile)
        chief = names.get(creator) or f"uid{creator}"
        chief_full = f"{chief}（uid{creator}）" if chief != f"uid{creator}" else chief

        out = [f"你认识的人（后面写的关系，都是他们和{chief}的关系，不是和你的）："]
        for p in people:
            n = p.get("name") or ""
            rel = (p.get("relation") or "").strip()
            head = f"{n}（uid{p['uid']}）" if n else f"uid{p['uid']}"
            if str(p.get("uid")) == creator:
                line = f"{head} —— 你的创造者"
            elif rel:
                line = f"{head} —— {chief_full}的{rel}"
            else:
                line = head
            if str(p.get("uid")) == me_uid:
                line += "（正在和你说话）"
            out.append(line)
        return "\n".join(out)

    def _day_cut(self) -> int:
        """今天第一条是第几轮。快照装它之前，历史装它之后。

        按「日期」切，不按轮数：今天说的全部进历史（不截断），
        昨天及更早的全部进快照。
        """
        today = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d")
        for i, t in enumerate(self.store.turns()):
            if str(t.get("ts") or "")[:10] >= today:
                return i
        return len(self.store.turns())

    def _snapshot_index(self, uid: str) -> int:
        """历史从哪里开始 —— 今天的第一轮。

        优先用快照里记下的分界（重建时算好），没有就按日期现算。
        """
        hit = self._ctx_cache.get(self._snap_key(uid))
        if not hit:
            return self._day_cut()
        return min(hit[1], len(self.store.turns()))

    def _history_for(self, uid: str, profile: dict | None = None) -> list[dict]:
        """今天说的所有话，按时间原样排，**不设上限**。

        和快照首尾相接：快照装到昨天为止，这里装今天。
        不重叠、不留缝，合起来是一条完整时间线。

        能看到谁：创造者看所有人；别人只看到自己（private_creator）。
        这不是提示词能保证的事 —— 「他是不是说过……」这种问法，
        模型照样会答「说过」，所以只能在数据层不给ta看。

        每轮只是末尾多几条，所以前缀缓存能整段命中。
        """
        start = self._snapshot_index(uid)
        names = self._name_map(profile)
        prefix = bool(self.cfg.prefix_user)
        only = self._only_uid(uid)

        rows: list[dict] = []
        for t in self.store.turns()[start:]:
            u = str(t.get("uid") or "")
            if only and u != only:
                continue
            who = names.get(u) or f"uid{u}"
            text = str(t.get("user") or "")
            line_user = f"{who}：{text}" if prefix else text
            line_asst = str(t.get("reply") or "")
            rows.append({"role": "user", "content": line_user})
            if line_asst:
                rows.append({"role": "assistant", "content": line_asst})
        return rows          # 今天的话全部带上，不截断

    def _build_messages(self, query: str, uid: str,
                        profile: dict | None = None,
                        images: list[str] | None = None) -> list[dict]:
        label = self._label(uid, profile)
        text = f"{label}：{query}" if self.cfg.prefix_user else query

        # 按「变化频率」排：稳定的人设最先，快照过的旧对话第二，
        # 然后才是不断追加的历史，最后放每分钟都变的时间。
        # 前缀缓存是按顺序比的，把爱变的东西放后面，前面才能命中。
        p = self._prompt_parts(uid, profile)
        msgs = [{"role": "system", "content": p["stable"]}]
        if p["ctx"]:
            msgs.append({"role": "system", "content": p["ctx"]})
        hist = self._history_for(uid, profile)
        if str(self.cfg.data.get("history_mode", "append")) == "append":
            msgs += hist                     # 全部带上，不裁
        else:
            msgs += hist[-int(self.cfg.history_window):]
        msgs.append({"role": "system", "content": p["tail"]})
        if images:
            content: list[dict] | str = [{"type": "text", "text": text or f"{label}：发来一张图片"}]
            content += [{"type": "image_url", "image_url": {"url": u}} for u in images]
            msgs.append({"role": "user", "content": content})
        else:
            msgs.append({"role": "user", "content": text})
        return msgs

    def caption_images(self, images: list[str]) -> str:
        """让模型看一眼图，写一句能存进记忆的描述。

        历史里不能重发原图（每轮都算钱），所以图只活在当轮，
        存下来的是这句话。
        """
        if not images:
            return ""
        parts: list[dict] = [{
            "type": "text",
            "text": "用一句中文描述这张图，不超过四十字。只写你看到的，不要猜背景，不要加评论。",
        }]
        parts += [{"type": "image_url", "image_url": {"url": u}} for u in images]
        try:
            raw = self.client.chat([{"role": "user", "content": parts}],
                                   temperature=0.0, max_tokens=80)
            desc = " ".join(str(raw).split())
            return desc[:120]
        except Exception:
            return "（一张图，没看清）"

    # ------------------------------------------------------------ 对话

    def chat_stream(self, query: str, uid: str | None = None,
                    profile: dict | None = None, on_tool=None,
                    images: list[str] | None = None,
                    image_meta: list[dict] | None = None):
        """images 是 data URL 列表，只在这一轮生效，不进历史。

        image_meta 是这些图的元数据（路径、尺寸、原始大小、是否压缩过），
        只用来写进聊天记录 —— 记忆里要留得住「这张图当时多大、压没压」。
        """
        uid = str(uid or self.uid)
        notify = on_tool or self.on_tool      # 每次调用各自一份，避免并发串台

        # 附件打不开的时候（视频没抽到帧、ffmpeg 不在、文件丢了），
        # 模型那边只有一句空话，ta会当成「对方什么也没发」。
        # 所以这里补一句说明，让ta知道对方确实发过东西。
        # 只补进 prompt，不动 query —— query 要原样存进记忆。
        note = ""
        if not images:
            kinds = [str(m.get("type") or "") for m in (image_meta or [])]
            if any(k == "video" for k in kinds):
                note = "（对方发来一个视频，我这边打不开）"
            elif any(k == "image" for k in kinds):
                note = "（对方发来一张图，我这边没看清）"
        prompt_text = query
        if note:
            prompt_text = f"{query}{note}" if query.strip() else note

        msgs = self._build_messages(prompt_text, uid, profile, images)
        temp = float(self.cfg.temperature)
        mx = int(self.cfg.max_tokens)

        # 图（或视频抽出来的帧）先让模型描述一句，存进记忆的就是这句话
        media: list[dict] = []
        if images or image_meta:
            desc = self.caption_images(images) if images else ""
            metas = list(image_meta or [])
            if metas:
                media = [{**m, "desc": desc or m.get("error") or "（没看清）"}
                         for m in metas]
            else:
                media = [{"type": "image", "desc": desc} for _ in images]

        # 工具可以连着调用好几轮（先搜，再挑一条打开）。
        # 每一轮都必须把 tools 一起发过去 —— 否则模型想调第二个工具时
        # 无处可发，只能把调用格式当正文吐出来（见过 `<｜｜DSML｜｜ invoke>`）。
        force = self.memory_tool and any(c in query for c in MEMORY_CUES)
        tool_temp = float(self.cfg.data.get("tool_temperature", 0.6))
        rounds = int(self.cfg.data.get("tool_rounds", 3))
        web_left = int(self.cfg.data.get("search_per_turn", 2)) if self.search_on else 0
        page_left = int(self.cfg.data.get("fetch_per_turn", 2)) if self.search_on else 0
        web_rows: list[dict] = []
        said: list[str] = []

        try:
            for step in range(rounds + 1):
                if step == rounds:                   # 收尾：不带工具，逼ta说完
                    for chunk in self._strip_echo(
                            self.client.chat_stream(msgs, temperature=temp, max_tokens=mx)):
                        said.append(chunk)          # 逐块记，断开时才存得下
                        yield chunk
                    break

                sink: dict = {}
                buf: list[str] = []
                for chunk in self._strip_echo(self.client.chat_stream_ex(
                        msgs, self.tools, sink, temperature=tool_temp, max_tokens=mx,
                        tool_choice="required" if (force and step == 0) else "auto")):
                    buf.append(chunk)
                    said.append(chunk)
                    yield chunk

                calls = sink.get("tool_calls") or []
                if not calls:
                    break

                msgs.append({"role": "assistant",
                             "content": "".join(buf) or None,
                             "tool_calls": calls})

                for tc in calls:
                    fn = (tc.get("function") or {}).get("name") or ""
                    args = {}
                    try:
                        args = json.loads(tc["function"].get("arguments") or "{}")
                    except json.JSONDecodeError:
                        pass
                    result = self._run_tool(fn, args, uid, profile, notify,
                                            web_left, page_left, web_rows)

                    # 预算在 _run_tool 里可能被用掉，这里同步回来
                    web_rows = result.pop("_rows", web_rows)
                    web_left = result.pop("_web_left", web_left)
                    page_left = result.pop("_page_left", page_left)
                    msgs.append({"role": "tool", "tool_call_id": tc["id"],
                                 "content": result.get("text", "（没有结果）")})
        except GeneratorExit:
            # 浏览器断线 / 前端主动断开：把已经说出去的那半段存下来。
            # 否则用户看到了、下一轮上下文里却没有，会显得ta失忆。
            self._after_reply(query, "".join(said).strip() or "（嗯。）",
                              uid, profile, media)
            raise

        text = "".join(said).strip()
        # 只有「彻底空」或者「只剩标点残渣」（DSML 被截断后留下的 <、|）才兜底。
        # 别用长度判断 —— 「嗯。」就是正常回答，不该被换掉。
        if not text or len(text) <= 1 or not text.strip("<|｜>/"):
            text = "（嗯。）"
            yield text
        self._after_reply(query, text, uid, profile, media)

    def _run_tool(self, fn: str, args: dict, uid: str, profile, notify,
                  web_left: int, page_left: int, web_rows: list[dict]) -> dict:
        """执行一次工具调用，返回要回填给模型的内容 + 更新后的预算。"""
        out: dict = {"text": "（没有这个工具）"}

        if fn == "list_people":
            if notify:
                try:
                    notify("list_people")
                except Exception:
                    pass
            out["text"] = self._people_block(profile)

        elif fn == "web_search":
            q = str(args.get("query") or "").strip()
            if not self.search_on or web_left <= 0:
                out["text"] = "（这一轮不能再查了。就按现在知道的回答，或者直说查不到。）"
            else:
                web_left -= 1
                if notify:
                    try:
                        notify(f"查：{q}")
                    except Exception:
                        pass
                rows = search.search(q, n=int(self.cfg.data.get("search_k", 5)))
                text = search.as_block(rows) or "（没查到相关的东西）"
                if rows:
                    text += ("\n\n（这些只是摘要。要具体数字或细节，"
                             "用 fetch_page 打开其中一条：index=1 或 "
                             "domain=\"上面写的域名\"。）")
                    web_rows = rows
                out["text"] = text

        elif fn == "fetch_page":
            idx = 0
            try:
                idx = int(args.get("index") or 0)
            except (TypeError, ValueError):
                idx = 0
            want = str(args.get("domain") or "").strip().lower()

            # 没给序号就按域名找；只写主域也能命中（weather.com.cn → www.weather.com.cn）
            if not idx and want:
                for i, r in enumerate(web_rows, 1):
                    d = (r.get("domain") or "").lower()
                    if d and (d == want or d.endswith("." + want)
                              or want.endswith("." + d)):
                        idx = i
                        break

            if not self.search_on or page_left <= 0:
                out["text"] = "（这一轮不能再打开页面了。用现有的说，或者说拿不到。）"
            elif not web_rows:
                out["text"] = "（还没搜过，没有可以打开的页面。先 web_search。）"
            elif not (1 <= idx <= len(web_rows)):
                avail = "、".join(f"{i}.{r.get('domain') or '?'}"
                                 for i, r in enumerate(web_rows, 1))
                out["text"] = f"（没有这一条。可以打开的是：{avail}）"
            else:
                page_left -= 1
                row = web_rows[idx - 1]
                if notify:
                    try:
                        notify(f"打开：{row.get('domain') or row['title'][:12]}")
                    except Exception:
                        pass
                page = search.fetch_text(row["url"])
                if page:
                    out["text"] = f"[{idx}] {page['title'] or row['title']}\n{page['text']}"
                else:
                    out["text"] = "（这一页打不开或者没有正文，用摘要里有的就行。）"

        elif fn == "search_memory":
            if not self.memory_tool:
                out["text"] = "（记忆检索是关着的。就按现在知道的答。）"
            else:
                q = str(args.get("query") or "").strip()
                if notify:
                    try:
                        notify(q)
                    except Exception:
                        pass
                out["text"] = self.search_memory(q, uid, profile)

        else:
            # 别把「不认识的工具名」当成记忆检索 —— 模型偶尔会编一个出来，
            # 那样等于绕过了所有开关。
            out["text"] = f"（没有这个工具：{fn}）"

        out["_rows"] = web_rows
        out["_web_left"] = web_left
        out["_page_left"] = page_left
        return out

    @staticmethod
    def _strip_echo(chunks):
        """把模型漏到正文里的「工具调用」痕迹掐掉。

        两种情况见过：
          1. 开头写 `[web_search 查询："…"]`
          2. 整段吐出内部标记 `<｜｜DSML｜｜ invoke name="fetch_page">…`

        第二种说明这次调用格式坏了，后面全是垃圾 —— 一旦撞上就整个截断，
        宁可这一轮没话说，也不能把这种东西送到用户眼前。
        """
        head = re.compile(
            r"^\s*(?:\[(?:web_search|fetch_page|search_memory|list_people)"
            r"[^\]]{0,80}\]\s*|查\s*[：:][^\n]{0,40}\n\s*)")
        bad = ("DSML", "｜｜", "<|", "invoke name=", "parameter name=",
               "</invoke>", "calls>")

        buf = ""
        started = False
        for c in chunks:
            if not started:
                buf += c
                if len(buf) < 24 and "\n" not in buf:
                    continue
                started = True
                buf = head.sub("", buf, count=1).lstrip()
                if any(t in buf for t in bad):
                    return
                yield buf
                buf = ""
                continue
            if any(t in c for t in bad):
                return
            yield c
        if buf and not started:
            if len(buf.strip()) < 4 or any(t in buf for t in bad):
                return
            yield head.sub("", buf, count=1).lstrip()

    def _after_reply(self, query: str, reply: str, uid: str,
                     profile: dict | None = None,
                     media: list[dict] | None = None) -> None:
        # 存进去的是「[图片] 一句描述」，不是图本身
        if media:
            kinds = {m.get("type") for m in media}
            tag = "[视频]" if "video" in kinds else "[图片]"
            descs = [m.get("desc") or "一段媒体" for m in media]
            head = "；".join(descs)
            stored = f"{tag} {head}" + (f"（{query}）" if query.strip() else "")
        else:
            stored = query

        u, _a = self.store.add_turn(uid, stored, reply, media=media)
        # 历史是从 store 现算的（快照之后的所有人），这里不用再往内存里追加。
        if self.scorer:
            self.scorer.submit(u["id"], stored)

    # ------------------------------------------------------------ 补打分

    def retry_pending(self, limit: int = 50) -> int:
        """把分数为 -1（或根本没分）的对话重新排队打分。"""
        if not self.scorer:
            return 0
        todo = mem.pending_turns(self.store.turns(), self.store.scores())
        for t in todo[:limit]:
            self.scorer.submit(t["id"], t["user"])
        return min(len(todo), limit)

    def start_retry_loop(self, interval: float = 600.0) -> None:
        """每 interval 秒检查一次有没有 -1 的分数。默认 10 分钟。"""
        def loop() -> None:
            while True:
                time.sleep(interval)
                try:
                    self.retry_pending()
                except Exception:
                    pass

        threading.Thread(target=loop, daemon=True, name="retry-pending").start()

    # ------------------------------------------------------------ 其它

    def stats(self) -> dict:
        turns = self.store.turns()
        hits, last = self.store.recall_index()
        scores = self.store.scores()
        now = datetime.now(timezone.utc).astimezone()
        rows = []
        for t in turns[-20:]:
            w = mem.weight(t, hits, last, scores, now)
            rows.append({"ts": t["ts"], "user": t["user"][:40],
                         "w": w["w"], "s": w["s"], "hits": w["hits"]})
        return {"turns": len(turns),
                "recalls": sum(hits.values()), "scored": len(scores),
                "recent": rows}

    def reset_session(self, uid: str | None = None) -> None:
        """清掉快照缓存，让下次请求重新生成。不传就清所有。

        键是 "\\*"（创造者共用一份）或 "u{uid}"（别人各一份），
        不能直接拿裸 uid 去 pop —— 那样永远清不掉。
        """
        with self._hist_lock:
            if uid is None:
                self._ctx_cache.clear()
            else:
                self._ctx_cache.pop(self._snap_key(str(uid)), None)

    def close(self) -> None:
        if self.scorer:
            self.scorer.close()

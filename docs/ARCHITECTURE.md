# 架构与开发说明

本文面向修改桥接代码的人，包括 AI 工具。功能和限制见 `docs/FEATURES.md`，需求待办与验收标准见 `docs/BACKLOG.md`，Codex 会话细节见 `docs/APPSERVER-MIGRATION.md`。

## 总览

一个 asyncio 进程，用 WebSocket 连接 NapCat（OneBot 11），默认用 Unix socket 连接本机共享的 Codex App Server；也可按 profile 使用 Claude Code 或主人单 agent Grok 子进程。聊天和干活是同一个 agent，一个会话同一时间只跑一轮；前后台分离的代码已在 2026-11-13 移除（见 `docs/DECISIONS.md` 的 P01）。

```
NapCat ──WebSocket──▶ Bridge.session()  事件分发、过滤、去重、记录群聊
                         ├ /stop、/front、/voice → 控制协程（不排队）
                         │ queue（容量 8）
                         ▼
                      Bridge.worker()    命令、附件、图片、长度检查（单个 worker，逐条处理）
                         │
                         ▼
                      Bridge.chat()
                         ├ instructions()  prompts.py + workspace/SOUL.md + workspace/OWNER.md
                         ├ agent.prepare() 恢复或新建会话
                         ├ turn_text()     拼接旧历史、群聊上下文
                         └ run_turn()      agent.run(TurnRequest) → Codex AppServer / Claude 或 Grok JSONL
                               ├ 进度 → Codex memory 门控（适用时）→ relay_progress() ─┐
                               └ 回复 → relay_answer()  ──┤（BubbleStream 切分）
                                                          ▼
NapCat ◀──WebSocket── Bridge.send()  全局锁，间隔随机 2～3 秒，每条 1600 字
```

## 模块

程序都在 `qq_chat/` 包里，入口是 `qq_chat/__main__.py`，用 `python3 -m qq_chat` 在项目根目录启动。包内模块之间用相对导入（`from .files import ...`）。

| 模块 | 职责 | 包内依赖 |
| --- | --- | --- |
| `bridge.py` | `main()` 入口与 `Bridge` 类：权限与语音开关的判断、一轮对话的组装和运行、`session()`（收）和 `worker()`（答）两个循环。其余职责由下面五个模块提供，`Bridge` 继承它们 | 下列全部 |
| `onebot.py` | `OneBot`：经 NapCat 发文字和语音、调用 OneBot 接口；所有发送共用一个限速 | `voice` |
| `store.py` | `open_database()` 建表与迁移；`Store`：消息去重、群聊参考记录与增量游标、首轮历史导入 | `prompts`、`agents.state`、`message_policy` |
| `media.py` | `Media`：接收附件、图片和引用里的参考图片（含整组时间预算） | `files`、`images`、`prompts`、`message_policy` |
| `commands.py` | `Commands`：不经过模型的命令（`/ping`、`/new`、`/session`、黑名单、`/help`、模型与强度）和 `/help` 文字 | `turns`、`message_policy` |
| `bili.py` | `Bili`：`/watch`。运行仓库外的 SurfCore 只读命令取视频材料，交给主人 agent 的只读会话，保存观后记和收藏 | `prompts`、`agents` |
| `message_policy.py` | `Source` / `Incoming`、事件解析、主人判断、会话键及发言人标签；不访问 Bridge 状态 | `message_text`、`message_references`、`moderation` |
| `codex_app.py` | `AppServer`：JSON-RPC 客户端、单轮对话事件处理；`progress_text` 生成进度文本、传递执行活动事件 | `agents.base`、`memory_progress` |
| `agents/base.py` | `Agent`、`TurnRequest`、权限级别常量和返回值上限；不负责 QQ 策略 | 无 |
| `agents/codex.py` | `CodexAgent`：线程准备/恢复、状态查询、归档、模型目录与底层 turn 调用 | `agents.base`、`codex_app`、`models` |
| `agents/__init__.py` | 构建 Codex/Claude/Grok profile，Codex profile 共用 transport | `agents.codex`、`agents.claude`、`agents.grok`、`agents.config` |
| `agents/grok.py` | 主人用的 Grok JSONL、私有 ACP 输入、会话/模型/中断；仅 FULL | `agents.base`、`agents.claude`（MIME 与进程清理工具）、`prompts` |
| `agents/claude.py` | Claude JSONL、NONE/FULL 启动、工具列表检查、回复校验、进程清理 | `agents.base` |
| `agents/config.py` | profile 引用、权限模式、模型目录及阶段范围校验 | 无 |
| `agents/state.py` | 模型偏好迁移与隔离、历史导入预算 | 无 |
| `tool_server.py` | 每轮授权、Unix socket、speak 与 memory_search 的 schema/执行和范围校验 | `message_policy`、`memory`、`voice` |
| `diary_migration.py` | 离线一次性迁移固定日记来源，保留原件并校验冲突；不参与查询 | `memory`、标准库 |
| `memory.py` | 在可信范围目录内做有预算的关键词检索，直接返回来源片段 | 标准库 |
| `memory_progress.py` | 识别直接记忆查询，按真实 Codex 活动事件缓冲/释放进度 | 标准库 |
| `turns.py` | `Turns`（`Bridge` 的基类）：会话锁、`/stop` 停当前轮、把回复发出去并写入历史的 `deliver`；每条消息都经过 | `agents.base`、`message_policy` |
| `prompts.py` | 公共权限/角色提示词与历史拼接；每轮的记忆、语音工具说明文字也在这里，由 `Bridge.turn_request` 决定用哪几段 | `moderation` |
| `message_text.py` / `message_references.py` | 消息文字与 @ 标签；引用、合并转发的受限展开 | `message_references` → `message_text` |
| `front_switch.py` | `/front`：换主人用哪个 agent，备份配置、独立重启并通知结果 | `agents.config`、`config_file` |
| `config_file.py` | 运行中安全改写 `config.json`：文件锁与原子替换，`/front`、`/voice` 共用 | 标准库 |
| `voice.py` | 合成进程协议、语音队列、音频校验 | `speech_normalize` |
| `speech_normalize.py` / `speech_runtime.py` | 朗读文本清理；独立语音工具的授权调用客户端 | 标准库 |
| `reply.py` | `BubbleStream`：把流式回复切成 QQ 气泡 | 无 |
| `files.py` | 安全保存附件（不跟随符号链接、限制大小、不覆盖） | 无 |
| `images.py` | 获取并校验图片（域名白名单、文件头识别格式） | `files` |
| `moderation.py` | 群聊用户黑名单、明确滥用检测、群友输出限制、加锁原子保存配置 | 无 |
| `models.py` | 分页读取可见模型、解析 Codex 配置的默认型号 | 无 |

`bridge.py` 编排其他模块，`prompts.py` 从 `moderation.py` 引入内部拉黑标记。新功能如果不需要访问 `Bridge` 的状态，优先在包里写成独立模块；从 `bridge.py` 拆出功能时也放在包里。运行时文件（`config.json`、`chat.sqlite3`、`workspace/`）在项目根目录，由 `bridge.BASE` 指向。

## `bridge.py` 的结构

- **消息基础层**（`message_policy.py`）：`incoming_text()` 把 OneBot 事件转成 `Incoming` 或丢弃；`owner_request()` 判断权限；`image_request()` 判断图片接收范围；`conversation_key()` 决定会话键；`speaker_message()` 生成发言人标签。这些函数不涉及 I/O，可以直接测试。`bridge.py` 保留原名字的导入兼容；新代码直接从 `message_policy` 导入。
- **`Bridge` 自己的方法**（`bridge.py`，约 530 行）：
  - 判断：`blocked` / `block_user` / `auto_block`、`conversation`、`agent_for`、`speak_enabled` / `voice_only` 等语音开关
  - 一轮对话：`instructions`、`turn_request`、`run_turn`、`relay_progress`、`relay_answer`、`chat` / `_chat`
  - 两个循环：`session`（接收、过滤、去重、不排队的控制命令）、`worker`（逐条处理）、`run`（启动与重连）
- **`Bridge` 继承来的方法**：`OneBot`（`send`、`send_audio`、`onebot_call`）、`Store`（`first_delivery`、`remember_group`、`group_reference`、`turn_text`）、`Media`（`receive_attachments`、`receive_images`、`receive_reference_images`）、`Commands`（`plain_command`、`model_commands`）、`Turns`（`conversation_lock`、`stop_command`、`deliver`）。

## 这次拆分做到了什么，没做到什么

2026-11-13 把 `bridge.py` 从 885 行拆到约 530 行，方法原样搬到上面五个模块。

这是**分文件，不是解耦**：五个类都靠 `self` 读写同一个 `Bridge` 对象（`self.cfg`、`self.db`、`self.send` 等），各类开头的说明写了它需要什么。好处是每个文件只讲一件事，改发送不用读命令，改命令不用读图片；测试里 `bridge.send = ...` 这类替换照常可用。没有解决的是：它们不能脱离 `Bridge` 单独构造和测试。

需要在 `bridge.py` 里读 `BASE` 的方法（人设文件、配置路径、工具命令路径）留在了 `bridge.py`，因为测试用 `patch('qq_chat.bridge.BASE', ...)` 换目录。

还可以继续的方向，每步独立验证：

1. `worker()` 仍有约 90 行，引用展开、附件、图片的接收顺序都在里面。
2. `run_turn()` 的进度和回答转发可以独立成一个小对象。
3. 要真正解耦，从 `OneBot` 开始最合适：它只需要一个 socket、限速状态和“是否被拉黑”的判断。

不同时改写 worker、权限规则和数据库事务。离线测试使用 `python3 -m unittest discover -s tests -q`；涉及权限边界的改动须覆盖命令授权、消息策略、轮次和工具授权测试。

## 权限模型

所有权限都来自 `owner_request(source, owner)`，它只比较数字 QQ 号。结果决定三件事：

1. 请求的权限级别：主人 `FULL`，访客 `NONE`；`CodexAgent.prepare()` 维持原 sandbox：`danger-full-access` 或 `read-only`
2. 使用哪套指令：`prompts.OWNER` 或 `prompts.GUEST`（`prompts.instructions`）
3. 能否使用管理命令、发图片和附件（`worker`、`receive_*`、`incoming_text`）

主人在群里时，会话键加上 `:owner` 后缀，与群友的线程分开，避免群友的历史消息进入有操作权限的线程。

`private_whitelist` 只扩大文字私聊的接收范围；主人判断仍只认 `owner_qq`。白名单访客使用独立的 `private:<QQ>` 会话、非主人指令和只读 sandbox，无工具、媒体及管理权限。群聊拉黑协议仅用于群聊，不注入访客私聊。

修改权限相关代码时，必须同时更新 `tests/test_command_auth.py` 和 `tests/test_message_policy.py` 中的对应测试。

## 群聊用户动态拉黑

`moderation.blocked_group_sender()` 在接收、worker 和发送端检查 `group_user_blacklist`；主人按真实 QQ 号豁免。仅触发机器人的群友消息才交给 `obvious_abuse()` 预检，命中后直接由 `Bridge.auto_block()` 保存并丢弃，不调用模型、不进入群聊参考记录。后续 worker 再检查一次，防止之前排队的消息继续回复。

模型侧只收到一个固定的当前发言人拉黑标记协议；它返回该标记时，worker 从 `Source.sender_id` 获取拉黑对象，不解析模型或用户提供的 QQ 号，不发送标记。主人命令可显式添加和解除名单；群友不能使用这些命令，模型也没有获得配置写权限。

`moderation.update_blacklist()` 使用 `.group-blacklist.lock` 文件锁，重新读取配置、合并单个设置，再以 600 权限写临时文件并原子替换。不要提交配置或锁文件。配置错误/磁盘故障时，自动路径先在内存拉黑并记日志，保持静默；重启后若未落盘则不保留，应检查日志。

## 一轮对话的组装

发给 Codex 的内容分两部分：

- **developerInstructions**：`prompts.OWNER` / `GUEST` + `SPEAKER_LABELS` + `WORKSPACE_RULES` + `workspace/SOUL.md`、`workspace/OWNER.md` 中存在的文件全文（依次加载，但 OWNER.md 只在 authorized=True 时读取）。每次 `thread/start` 或 `thread/resume` 时重新读取并传入，缺失则跳过。但当前已加载 Codex 线程的 `thread/resume` 不采用新的 developerInstructions，桥接规则及人设更新须 `/new` 创建新线程才生效；Claude/Grok 则每轮刷新。逐轮记忆命令单独通过 `runtime_instructions` 在 `turn/start` 前注入可信 developer 历史项，这不等于完整人设热更新。AGENTS.md 不由桥接读取或注入，保留 Codex 按 cwd 原生加载。
- **本轮输入**，依次为：
  1. 群聊参考记录（`group_reference`，仅群聊）
  2. 旧历史（`history_import`，仅线程第一次运行，且该会话有旧记录时）
  3. 发言人标签 + 正文（`speaker_message`）；带附件或图片时，还会附上保存路径的说明
  4. 图片本身，作为 `localImage` 单独传入

## 数据库（`chat.sqlite3`）

| 表 | 用途 |
| --- | --- |
| `tasks` / `task_inputs` | 已移除的后台任务留下的历史数据（32 条任务），代码不再读写，新数据库里也不会创建 |
| `seen` | 已处理的消息 ID，保留 7 天 |
| `threads` | 会话键 → Codex 线程 ID |
| `conversation_models` | 旧 Codex 偏好的兼容表，迁移后仍同步默认 Codex 的选择 |
| `profile_models` | 会话键 + profile → 模型及强度；Claude 不读取 Codex 偏好 |
| `agent_sessions` / `old_agent_sessions` | 非旧默认 Codex 的会话映射和 `/new` 归档，按 profile 隔离 |
| `agent_metadata` / `agent_profile_types` | 固定旧 Codex 迁移身份并记录 profile 类型，防止配置切换后误迁移或复用异类会话 |
| `old_threads` | `/new` 归档的旧 Codex 映射 |
| `bootstrap` | 已导入过旧历史的线程 |
| `history` | 成功交付的问答记录；新会话补入有预算的最近历史，/new 删除本会话记录；不是长期记忆档案 |
| `group_context` | 每群最近 100 条文字消息 |

建表和字段迁移写在 `store.open_database()`，用 `CREATE TABLE IF NOT EXISTS`。新增表或字段时沿用这个方式，旧数据库可以直接升级。

## 外部依赖

- Python 3（当前使用 3.14），第三方库只有 `websockets`
- NapCat（systemd 用户服务 `napcat.service`），在 `ws_url` 提供 OneBot 11 WebSocket
- Codex CLI（`codex_path`），由桥接运行 `codex app-server daemon start` 启动守护进程
- systemd 用户服务 `qq-bridge.service`：`ExecStart=/usr/bin/python3 -u -m qq_chat`，工作目录为项目根目录（`-m` 依赖它找到包）

## 常见改动

当前边界：`Bridge.agents` 持有各 profile，`Bridge.agent` 是主人前台便捷入口，`agent_for(source)` 按真实身份选择前台或访客 profile；Bridge 不再依赖 `self.app`。`TurnRequest` 的 `level=FULL/NONE`、人设全文和模型选择由 Bridge 组装。先 `prepare(req)` 得到线程，再拼接旧历史与群聊上下文，最后通过 `run(req)` 执行。请求额外包含 `session_id`（已准备的线程）、`show_tools`（进度显示开关）及 `on_activity`（Codex 执行活动回调），避免 run 再次恢复线程或重复读取人设。

`session_id(key)` 只查已有映射，供 `/session` 使用；`model_state(thread)` 提供接口返回的实际模型与强度；`models()` 和 `default_model()` 封装原模型目录及临时线程解析。`reset(key)` 仅在当前数据库事务中归档、移除当前 profile 的会话映射，Bridge 继续清除 history 并统一提交，模型选择不清除。适配器借用 Bridge 的数据库，不负责关闭它。

只支持单 agent；配置里 `agents.back` 非空会在启动时报错（见 [DECISIONS](DECISIONS.md) 的 P01）。`AppServer.turn()` 的事件处理、前缀一致性及异常语义保留，Codex 返回上限仍在适配器。新会话历史统一由 `recent_history()` 从最近 20 条向前取，每条截到 1000 字，JSON 序列化后不超过 8000 字，再恢复时间顺序；数据库原记录不截断。

Claude NONE 在桥接的 `run/claude/<profile>/` 空目录运行，safe-mode 加两个禁用记忆变量，不携带 MCP；FULL 使用实际 workspace 工作目录，正常加载项目规则，不添加隔离开关。人设写入 600 权限临时提示文件，NONE 替换系统提示、FULL 追加；每轮 snapshot=off，结束删除文件。子进程以新进程组启动，stdout 上限 16 MiB、stderr 并行排空，异常或取消依次 SIGINT/SIGTERM/KILL 清理；同一会话由适配器锁串行运行。显式停止见下文“会话锁与停止”。

Claude 工具列表未校验通过前不接受回复；完整聚合顶层 assistant 文本，忽略子 agent 文本和 thinking，检查 `<invoke` / `<system-reminder` 等标记后才回放到气泡回调。`result` 失败、缺失、异常退出或可疑标记均不发缓冲文字。`TurnRequest.history_text` 承载验证后的全文，Bridge 仅为主人正常成功回复暂存该值、在 worker 落库时取出；访客仍保存实际发送的受限文本。模型返回值上限与全文历史是两个不同用途。

`agent_sessions` 在 prepare 时持久化 UUID；CLI 明确表示找不到该会话且没有初始化执行时，抛 `MissingSession`，Bridge 只重试一次：归档旧映射、分配新会话、重新导入预算内历史。其他错误不自动重试，防止重复执行工具。

启动时把旧模型记录幂等复制到固定的 Codex profile。该身份写入 agent_metadata 后不因更换 front 改变；同一个 profile 名禁止更换后端类型，需使用新名字，避免复用旧会话或偏好。旧表保留并同步默认 Codex 写入，其他 profile 不回写旧表。

模型管理由 `Bridge.model_command()` 执行，worker 先检查主人权限，处理函数再次检查。命令验证 agent 返回的目录后保存选择；`CodexAgent.prepare()` 在创建/恢复时传入选择，`run_turn()` 还会通过请求在每次 `turn/start` 明确传入所选模型及推理强度，避免已加载线程忽略恢复覆盖。Codex 适配器缓存接口返回的实际模型状态；Claude 在首次 init 前仅有配置别名，之后缓存实际返回型号，未设置偏好时用于显示；已设置时显示下一轮使用的型号，不把尚未生成的选择冒充为已完成的切换。空线程可能尚无 rollout，遇到该特定错误时重建映射，历史表不清空。

- **加一个命令**：不经过模型的命令加在 `commands.py` 的 `plain_command()` 里。默认只有主人可用；如果要对所有人开放，把命令加进 `worker()` 开头的白名单元组 `('/ping', '/help')`。要在她忙的时候也能响应（像 `/stop`），则在 `session()` 里分发。同时在 `/help` 文字和 `docs/FEATURES.md` 的命令表里补上，并在 `tests/test_command_auth.py` 加测试。
- **改提示词**：改 `prompts.py`，包括每轮的记忆和语音工具说明。桥接注入的人设放入 `workspace/SOUL.md`，主人资料放入 `workspace/OWNER.md`；开发规则留在 Codex 原生加载的 `workspace/AGENTS.md`。文件不自动迁移，均属本地忽略文件。
- **加配置项**：用 `self.cfg.get('key', 默认值)` 读取，让旧配置文件也能工作；同时更新 `config.example.json` 和 README 的配置表。
- **加一种消息类型**：在 `incoming_text()` 中解析，在 `Incoming` 中加字段，在 `worker()` 中处理。

## 测试

```sh
python3 -m unittest discover -s tests -v
```

测试全部离线运行，不连接 QQ，也不调用模型。常见写法：

- 用 `Bridge.__new__(Bridge)` 跳过 `__init__`，手动设置 `cfg`、`db`（`sqlite3.connect(':memory:')`）和需要的属性。
- 把 `bridge.agents['codex']` 换成 `FakeAgent`，记录 `TurnRequest` 并用事件控制运行/失败；需要检查底层 JSON-RPC 参数时，把 `bridge.agents['codex'].app` 换成假的 App Server 对象。
- 把 `bridge.send` 换成记录发送内容的函数。
- 需要文件系统时，用 `patch('qq_chat.bridge.BASE', 临时目录)`。
- 替换模块级函数时按它现在所在的模块写，例如 `patch('qq_chat.media.save_image')`、`patch('qq_chat.onebot.random.uniform')`。
- 需要一个带假 agent 的完整 `Bridge`，继承 `tests/support.py` 的 `BridgeCase`。

## 开发约定

- 仓库是公开的。不要提交 `config.json`、数据库、日志、`workspace/`，代码里也不要出现 QQ 号、token 或个人信息；按人设置的值放进配置。
- 改功能时，在同一个提交里更新 `docs/FEATURES.md` 和 `docs/CHANGELOG.md`。
- 改完先跑测试再提交；部署时运行 `python3 tools/restart_bridge.py`；出问题时用 `git revert` 回滚。

### 会话推理强度（仅主人）

`/effort` 查询当前模型、强度和支持档位；`/effort 档位` 设置；`/effort default` 恢复当前模型目录默认强度。Codex 按实时目录、Claude 按配置目录校验，不支持的档位或查询失败不更改原设置。按会话和 profile 存入 `profile_models`，旧 Codex 表保留兼容，下一条消息生效，重启和 `/new` 保留，只影响当前主人会话。未选择模型时首次设置会固定当前会话模型；再次 `/model` 切换会重置为所选模型默认强度。

## 会话锁与停止

`Bridge` 继承 `turns.Turns`。会话锁按 conversation key 建立，对同一 asyncio task 可重入；chat、模型/强度命令、/new 均持锁，worker 持锁直到交付和历史落库，所以同一个会话里的两轮不会并发操作同一线程。

`/stop` 由 `Bridge.session()` 直接创建控制协程，不排队、不取会话锁，非主人静默。它请求当前 agent 中断这个会话正在跑的那一轮（`front_execution` 记录谁在跑）。中断请求送达不等于停止成功：最终按执行端的结果回报“已确认停止”“停止前已完成”或“停止前已失败”；30 秒未确认会提醒，轮次不提前释放。

`deliver` 是回复的唯一出口：按是否仅语音决定发文字还是念出来，再把问答写入 `history`。

## 每轮工具授权

tool_server 绑定 run/tools.sock，目录 700、socket 600，拒绝替换普通文件或活 socket。`Bridge.chat` 每轮生成 ToolGrant：主人按配置取得 memory_search（memory 启用）和 speak（voice.mode=tool 且语音已启用）；开放群聊语音时群友只取得 speak。主人走命令入口（`tools/speak.py`、`tools/memory_search.py`），群聊访客的 Codex 走原生工具。grant 的工具名单写在 ToolGrant.names，turn_request 只为名单内的工具注入说明。工具服务在启用 memory 或 speak 时启动。

授权文件包含 socket 和随机令牌，权限 600，令牌绑定 Source/会话并限时，finally 撤销。文件里 `mcpServers.qq.env` 这层嵌套是它早先兼作 MCP 配置时留下的形状，两个命令行入口按它读取；要改就两头在同一次部署里一起改。工具请求由独立协程处理，不取会话锁，避免 await agent.run 时死锁。参数不接受来源、QQ 或会话字段，来源只认令牌。日志只记工具名、来源类型及结局，不记令牌或正文。

## Grok 单 agent 接入

主人明确选择与当前 Codex 相同的操作权限。配置类型为 grok_build，仅准用于 owner front；guest 不可引用。每个 profile 明确 permission_mode=bypassPermissions 和 sandbox=off，避免配置名义与实际命令不一致；Bridge 的身份校验不改，访客仍使用既有 Claude NONE。

GrokAgent 使用现有 agent_sessions/old_agent_sessions/profile_models 存储，旧 Codex 迁移身份不变。每轮 `--session-id` 或 `--resume`，输出 streaming-messages-json，关闭子 agent 自动派生。启动校验 session、权限模式和核心文件/终端工具；NONE 级别的请求在启动前拒绝。

Grok 续接时会缓存首次系统规则，单纯更换 --agent 文件未能刷新旧人设。因此命令行只传恒定公开 BRIDGE_RULES；动态 SOUL/OWNER、工作台规则、用户输入和图片内容块每轮存入 600 权限 prompt JSON，CLI --prompt-file 读入，finally 删除。每轮公开系统约定采用 current_bridge_rules，已用两轮合成设定及公开历史词验证更新与续接同时生效。图片为原生 ACP image/data/mimeType 格式，不把 base64 或个人文本放进 argv。

只解析已校验 init 之后的顶层 assistant text；忽略 thinking、签名和子 agent 数据，不对原始 stderr 做日志输出。整轮成功且未含伪调用标记才交给现有回调，保留最大返回值与完整历史侧通道。原生 result.cancelled 或本轮发过中断且进程未成功结束时抛 Interrupted；普通失败不重试，防止重复执行。取消/超时清理本轮进程组及提示文件。模型目录使用明确配置，本机 models 实测当前默认且可用 grok-4.7；不继承旧 Codex 的模型/effort。

## 独立于模型的前台切换

`Bridge.session()` 直接分发 `/front` 至 `front_switch.command()`，按真实 Source 校验主人，不获取会话锁、不进入 worker。`/front` 列出通过现有 `settings()` 校验的 profile；切换只修改 `agents.front`，旧配置隐含的 guest 会先固定为原值，避免访客跟随切换。每个 profile 的线程和模型状态仍由现有数据库保存。

命令先生成 0600 私有任务记录、发送 QQ 确认，再经 systemd-run 创建独立用户服务（2 秒后执行），避免桥接停止时连同重启任务一起被杀死。`python -m qq_chat.front_switch --job UUID` 重新读取和校验最新配置，与黑名单写入共享文件锁，合并前台字段，原子保存两份私有备份及新配置。重复切换被持久任务状态和进程内锁拦截；记录超过 300 秒可再次操作，服务最长执行 240 秒。

独立任务调用 `tools/restart_bridge.py`，检查服务 PID 变化、NapCat 重连日志和持续存活，然后直接私聊回报，不依赖前台模型。失败时仅在当前配置仍与本任务写入值完全一致时恢复快照并复验，避免覆盖并发人工修改。若恢复或通知失败，私有任务文件保留状态供诊断。需要可用的 systemd 用户服务和原有 OneBot 连接。

## 手动记忆与会话上下文的边界

- `history` 与 `group_context` 是聊天续接/参考；`memory` 是独立本地文件，聊天history不会自动变成记忆。日记和详细日志直接写入 memory/private/diary，查询直接搜索 memory，不复制或刷新副本。
- `ToolServer.call` 按真实 Source 决定 root 下的 shared/private/当前群目录，模型只能传 query。memory-only 授权的 tools/list 也只含 memory_search，不能借它调用别的工具。
- `tools/speak.py` 与 memory_search 同构，只传 text/style/intensity 和可选的 scene。scene 原样交给语音后端；IndexTTS 后端的 `SceneLibrary` 只从原声库中通过质检、非受限、编号合法的原声里选，协议不接受路径。`ToolServer.call` 用绑定的 Source 调 `VoiceReplies.say`，模型不能指定目标；校验错误原样返回给模型以便改写重试。风格名在 `voice.STYLES`，对应的参考音频与情绪向量只在 `tools/indextts_backend.py` 的 PRESETS，协议不接受路径。`memory_progress.speak_command` 识别本轮下发的 speak 命令形状，与记忆检索一样不放出进度。
- `tools/memory_search.py` 只读取本轮 grant 并请求 Unix 服务；token 不进入 argv、日志或返回值，轮次结束撤销并删除授权文件。Codex 在 turn/start 前用 thread/inject_items 注入当前轮记忆命令，避免已加载线程恢复时沿用旧授权路径；注入失败不开始该轮。FULL 仍保留其他文件工具，不能将此范围检查说成系统沙箱。
- `memory.search` 以目录描述符逐层打开，NOFOLLOW 并排除硬链接/特殊文件；每文件最多3个不同片段，含有限相邻上下文，同分时正文优先于Markdown标题；最多5条、单条500字、正文共2000字。关键词检索不等于语义检索，扫描预算与 partial 语义见 MEMORY.md。
- Codex 主人单 agent 有 memory grant 时，Bridge.run_turn 安装 MemoryProgress；AppServer 在转发进度前传递真实 item 活动事件。纯记忆调用丢弃缓冲，其他或未知工具释放缓冲；最终回答、失败和停止不经过该过滤。
- 日期记录由主人会话按本地规则手动维护，现有夜间整理器只整理日记、详细日志和索引，不自动监听全部聊天或提取事实。主题更新规则已保存，但主题写入、纠正/遗忘流程尚未实现。本机已完成正式目录切换，旧原件及两类旧副本已按主人授权删除；服务保留旧副本排除逻辑，详情见 MEMORY.md。

当前检索协议与验收边界以 [MEMORY.md](MEMORY.md) 为准，代码变更历史看 CHANGELOG。

## 引用与合并转发

`message_references.py`识别reply/forward数组段，在worker内用已有OneBot通道获取get_msg/get_forward_msg及有界内联/嵌套节点。Incoming新增references，事件接收与入队保留这些段，但不在WebSocket接收循环await获取，否则无法处理对应echo回执。普通群消息仍沿用触发规则；静默卡片只留占位，不主动展开。

引用结果按当前来源校验get_msg范围，并以JSON参考区块置于真实speaker_message之前，署名不授予身份权限；参考图片仅主人可下载，通过 `receive_images(..., prefer_url=True)` 优先按 QQ CDN 地址获取；失败后给 get_image 刷新至多5秒，整组参考图另共用15秒预算，预算耗尽跳过后续图并保留文字。`images.save_reference_image` 在取消时让工作线程收尾，清理晚返回的新文件；不声称已取消QQ内部下载。直接图片保持原来的缓存优先路径；群友仅文字，文件/语音/视频不下载。管理命令检查用当前直接文字，不看参考正文。节点、调用数、深度、文本与超时预算见FEATURES；日志不写引用正文。参考获取失败以提示进入当前轮，原文字请求仍可处理。

## 群聊参考增量位置

`group_context_cursors`以(conversation, profile, session_id)为键保存last_id。group_reference读取id大于已处理位置、小于当前触发context_id的最近有限消息，保留40条/12000字上限。成功完成模型轮次后与bootstrap同事务推进到触发context_id，不用生成结束时的群消息最大ID，因此生成期间收到的消息不会漏掉；失败不推进。主人与群友会话、不同群/profile/session各自隔离，数据库持久化支持重启续用。

首次无位置使用原有近期参考；显式/new清除当前会话/profile的位置，并写入空session_id的重置起点（/new消息的context_id，没有消息ID的内部调用才取当前群最大ID），之后新session不重放重置前记录。旧群上下文和其他会话位置不删除。

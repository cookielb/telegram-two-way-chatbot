# Telegram 双向聊天机器人 · VPS 合租场景二开版

基于 [Hamster-Prime/Telegram_Anti-harassment_two-way_chatbot](https://github.com/Hamster-Prime/Telegram_Anti-harassment_two-way_chatbot) 的二次开发版本，面向 VPS、节点拼车合租咨询，保留用户私聊与管理员话题群之间的双向沟通，并调整广告审核、验证记录和话题重建流程。

本仓库为独立二开版本。原项目的功能基础和主要架构来自上游；本版改动不代表上游项目的实现或维护承诺。

> 状态：开发版本。已有部署反馈，但完整回归测试及真实 Telegram 场景验证尚未完成。建议先使用测试 Bot 和独立数据目录验证，再迁移正式实例。

## 二开改动

| 功能 | 本版行为 |
| --- | --- |
| VPS 合租咨询 | 拼车、上车、车位、价格、续费等按正常业务理解 |
| 阶段性 AI 审核 | 默认累计 3 条消息通过自动审核后，后续跳过 AI 审核，降低调用量 |
| 删除话题重置 | 下次发送时检测旧话题失效，重置人机验证和 AI 审核计数；答题后再建立新话题 |
| 验证题来源 | 记录 AI / 本地题库、模型、回退原因和验证时间 |
| 广告识别 | 审查正文、媒体说明、隐藏链接和静态图片，增加广告专用提示词 |
| Gemini 请求 | 系统规则与用户数据分开，使用结构化 JSON 输出，拒绝“等待输入”等无效结果 |
| AI 故障处理 | 接口异常时继续转发，并尝试通知管理员，记录异常放行原因 |
| 人工审核 | 支持查看保存内容、单条放行、确认广告、拉黑用户 |
| 消息转发 | 尽量发送已审查版本，保留富文本、回复和媒体；支持编辑同步 |
| 限流 | 相册按消息数量计数，编辑单独限流 |
| 功能精简 | 移除 RSS 订阅和网络测试模块、命令、面板及专属依赖 |
| 部署 | 支持 Docker Hub 镜像在线部署和从本地代码构建 |

保留的主要功能：话题管理、双向聊天、黑名单、内容审查豁免、知识库自动回复、多管理员权限。

## 审核和验证机制

人机验证只确认用户完成答题，不等于消息内容安全。验证题优先由所选 AI 服务生成；失败时可使用本地题库。答案由程序比较，无需再调用 AI。

消息经黑名单、限流及必要的人机验证后进入内容检查：

1. 开启审查且用户未被豁免时，先执行本地营销规则。
2. 本轮自动审核通过数未达到 N 时，调用 AI；默认 N=3。
3. 明确违规可拦截，疑似内容进入人工审核，正常内容转发。
4. AI 密钥、额度、网络、超时或返回格式异常时，直接放行并告警。
5. 达到 N 条通过记录后，停止 AI 审核；本地规则、黑名单和限流继续生效。

计数按本轮不同消息 ID 的自动审核通过记录计算，不是简单的“收到前三条”或“成功转发前三条”。失败放行、人工放行、豁免不计数；历史记录保留。删除话题会重置计数基线，发送 /start 不会。

审核阶段内，无法完整自动检查的视频、语音、文档及部分媒体进入人工审核；超过阶段性审核条数后，会跳过该媒体检查。风险分是模型估计，不是准确概率，无法保证零误判或零漏判。

AI 内容拦截不等于自动封禁；管理员可在审核入口决定是否拉黑。原有验证失败和限流规则仍可能封禁用户。

# （首次部署必读）配置 Telegram Bot和群组

### 创建 Bot 与获取群组 ID
**FORUM_GROUP_ID 必须填写管理员话题群的完整数字 ID。它不是管理员用户 ID、群链接，也不是某个话题的 ID。** 请直接复制机器人返回的“群组ID”，保留负号，通常以 -100 开头。

**1. 创建机器人，获取 BOT_TOKEN**

- 在 Telegram 打开官方 [@BotFather](https://t.me/BotFather)，核对账号身份，避免同名账号。
- 发送 /newbot，按提示输入显示名称和唯一用户名；用户名通常以 bot 结尾。
- BotFather 返回 API Token，将它填写到 .env 的 BOT_TOKEN。Token 。

**2. 创建管理员群并开启“话题”**

- 新建一个用于接收信息的 Telegram 群组。
- （重要）进入群资料 → 编辑/管理群组 → 找到“话题 / Topics”并开启。
- 开启后确认群中出现 General/默认话题，不使用频道或普通无话题群。
- 如果没有 Topics 选项，确认你是群主，更新官方客户端，并检查当前群是否满足 Telegram 开启话题的条件；必要时创建新的话题群。

**3. 添加 Bot 并赋予权限**

- 将刚创建的 Bot添加到群组（复制Bot用户名，群组中邀请新成员，粘贴添加Bot）。
- 群资料 → 管理员 → 添加管理员 → 选择 Bot。
- 开启“管理话题 / Manage Topics”，并确保能发送文字和媒体。删除消息权限可按需要开启。（弄不明白就全部授予）。
- 管理话题权限不足会导致创建、检查或重新打开话题失败。

**4. 不知道 ID 时，先以最小配置启动**

可以先在 .env 填写真实 Token，暂时使用：
```env
BOT_TOKEN=your_telegram_bot_token 必填
FORUM_GROUP_ID=0
ADMIN_IDS=
```
**5. 在目标群发送 /getid**
启动 Bot 后，在刚创建的群内发送 /getid。
- 机器人会返回两行，例如：

```text
群组ID: -1001234567890
用户ID: 123456789
```

对应填写：

```env
FORUM_GROUP_ID=-1001234567890
ADMIN_IDS=123456789
```

**6. 保存并重新加载配置**

```bash
docker compose up -d --force-recreate
docker compose logs --tail=100
```


## Docker 部署

服务器需安装 Docker 和 Docker Compose。可选在线镜像部署（无需下载源码或构建），或本地源码构建。两种方式使用相同的 .env 和 data 数据目录。

### 方式一：Docker Hub 在线镜像部署

#### 1. 准备目录和配置

```bash
mkdir -p /root/tg-bot-data/data
cd /root/tg-bot-data
curl -fL https://raw.githubusercontent.com/cookielb/telegram-two-way-chatbot/main/.env.example -o .env
nano .env
```

填写 BOT_TOKEN、FORUM_GROUP_ID、ADMIN_IDS 和所选 AI 服务的密钥，配置说明见下面“配置 Telegram 和 AI”。DATABASE_PATH 保持 ./data/bot.db。已有部署请保留原 .env，不要用下载命令覆盖。

#### 2. 创建 docker-compose.yml

```bash
nano docker-compose.yml
```

```yaml
services:
  tg-bot:
    image: cookielb/telegram-two-way-chatbot:latest
    restart: unless-stopped
    env_file:
      - .env
    volumes:
      - ./data:/app/data
```

#### 3. 拉取并启动

```bash
docker compose pull
docker compose up -d
```

查看日志
```bash
docker compose logs --tail=100
```

ARM64 CPU 架构，可能需要自行编译~！

#### 4. 更新和修改密钥

修改 .env 时执行 docker compose up -d --force-recreate 。不要使用 docker compose restart 该命令不会加载修改后的环境变量。

### 方式二：本地源码构建

#### 1. 准备项目

下载本仓库完整代码并进入目录。服务器需要安装 Docker 和 Docker Compose。

```bash
cp .env.example .env
mkdir -p data
```

#### 2. 配置 Telegram 和 AI

创建 Bot；准备启用了“话题”的超级群组，将 Bot 设为管理员并赋予管理话题、发送消息权限。

管理员需先私聊 Bot 发送 /start，以便收到私聊审核和异常通知。多名管理员的用户 ID 用逗号分隔。

编辑 .env，至少填写：

```env
BOT_TOKEN=your_telegram_bot_token （填写BotToken）
FORUM_GROUP_ID=-1001234567890 （ - 号必须保留）
ADMIN_IDS=123456789,987654321 （一个即可）
DATABASE_PATH=./data/bot.db

GEMINI_API_KEY=your_gemini_api_key （不填则不开启AI广告过滤）
GEMINI_BASE_URL= （留空即可）

ENABLE_AI_FILTER=true
VERIFICATION_ENABLED=true
AUTO_UNBLOCK_ENABLED=false

AI_CONFIDENCE_THRESHOLD=70
AI_REVIEW_THRESHOLD=40
AI_NEW_USER_REVIEW_THRESHOLD=25
NEW_USER_STRICT_MESSAGES=3
AI_TIMEOUT_SECONDS=40
AI_RETRY_ATTEMPTS=2
AI_ALERT_INTERVAL_SECONDS=3600
```

直连 Google Gemini 时 GEMINI_BASE_URL 留空。使用中转时需为 Gemini 原生接口地址。

若使用 OpenAI 兼容接口，填写 OPENAI_API_KEY、OPENAI_BASE_URL，并在 /panel → AI 模型设置中选择 OpenAI。仅填写密钥不会自动切换数据库中的服务选择；所选审核模型需支持相应输入，图片审核需要视觉能力。

#### 3. 构建和启动

```bash
docker compose up -d --build
```

查看日志
```bash
docker compose logs --tail=100
```

本地构建使用仓库中含 build: . 的 docker-compose.yml 和 Dockerfile；保留该文件即可继续本地构建。不要用上面的镜像版覆盖它，需并存时另存为 docker-compose.image.yml，并显式指定 -f。上游项目镜像不包含本版改动。

修改代码后重新构建；仅修改 .env 中的密钥时重新创建容器：

```bash
docker compose up -d --force-recreate
```

## 参数说明

| 参数 | 代码默认值 | 说明 |
| --- | --- | --- |
| ENABLE_AI_FILTER | true | 开关内容审查；关闭时本地广告预检也跳过 |
| AI_CONFIDENCE_THRESHOLD | 70 | AI 判为垃圾内容且达到此风险分时拦截，业务保护规则可能转人工审核 |
| AI_REVIEW_THRESHOLD | 40 | 普通审核风险阈值；当前阶段性模式主要使用下面的新用户阈值 |
| AI_NEW_USER_REVIEW_THRESHOLD | 25 | 阶段性 AI 审核的人工复核阈值 |
| NEW_USER_STRICT_MESSAGES | 3 | 本轮需要累计自动审核通过的消息数；0 表示不调用阶段性 AI 审核 |
| AI_TIMEOUT_SECONDS | 20 | 每次内容审核请求超时，示例建议设为 40 秒 |
| AI_RETRY_ATTEMPTS | 2 | 最大尝试次数，含首次；密钥、限额、模型错误提前结束重试 |
| AI_ALERT_INTERVAL_SECONDS | 3600 | 同一管理员 AI 异常通知间隔，秒；重启后计时重新开始 |
| VERIFICATION_ENABLED | true | 新用户人机验证开关；删除话题触发的重新验证走专用流程 |
| AUTO_UNBLOCK_ENABLED | true | 是否允许答题自助解封；本版配置示例建议 false |
| MAX_MESSAGES_PER_MINUTE | 30 | 每用户普通消息限流值，编辑使用独立预算 |
| MAX_WORKERS | 5 | 消息处理并发上限 |

阈值需满足：0 ≤ 新用户审核阈值 ≤ 普通审核阈值 < 拦截阈值 ≤ 100。

## 常用命令

| 命令 | 用途 |
| --- | --- |
| /start、/help、/getid | 启动、帮助、查询 ID |
| /panel | 管理面板和 AI 服务、模型设置 |
| /review [上一页最后编号] | 待审核及可处理记录；按按钮预览、放行、确认广告或拉黑 |
| /audit <用户ID> | 最近审核记录、题目来源和本轮计数；可在用户话题内直接 /audit |
| /block、/unblock <用户ID> | 拉黑和解封；/block 支持在对应话题直接使用 |
| /blacklist、/stats、/view_filtered | 黑名单、统计、被过滤消息 |
| /exempt | 管理内容审查豁免 |
| /autoreply | 自动回复及知识库管理 |

管理命令受 ADMIN_IDS 限制。豁免会绕过内容审查，请按需设置。

### 看懂 /audit

| 记录 | 含义 |
| --- | --- |
| challenge_ai | 答题通过，题目来自 AI |
| challenge_local | 答题通过，题目来自本地题库 |
| passed + 服务/模型 | AI 返回有效结果并通过策略判断 |
| review / blocked | 待人工审核 / 内容拦截 |
| error_allowed | 审查异常后放行，不代表已判定安全 |
| skipped | 审查关闭或用户豁免 |
| precheck_passed | 本地预检未命中，不代表 AI 成功 |
| bypassed_after_initial_review | 本轮通过条数已达标，跳过后续 AI |

旧版本的 challenge 记录不能追溯题目来源。

## 从上游部署迁移

1. 停止旧容器，备份旧代码、Compose、.env 和整个 data 目录。
2. 保留服务器上的 .env 和 data，更新新增参数。在线部署仅需镜像版 Compose 文件；本地构建需上传完整代码。
3. 自定义数据库路径和 data 挂载路径需保持一致。
4. 先在测试实例验证。在线部署执行 docker compose pull 和 docker compose up -d；本地构建执行 docker compose up -d --build。

启动迁移增加审查、验证记录表及用户字段，沿用已有用户、黑名单和话题数据。不要同时运行两个相同 Token 的实例。

RSS 和网络测试已移除；旧订阅及服务器 JSON 文件不会再读取，可自行备份后清理。回滚应停止新版并恢复旧代码与升级前数据库；恢复备份会丢失升级后的新增数据。

## 验证与已知限制

建议检查普通咨询、VPS 拼车、文字/图片广告、AI 故障放行与通知、相册、编辑、人工放行，以及删除话题后的重新验证和审核计数归零。

```bash
docker compose build
docker compose run --rm --no-deps tg-bot python -m compileall -q .
docker compose run --rm --no-deps tg-bot python -m pytest -q
```

完整测试尚未完成，部分上游测试的模拟可能需要适配新逻辑。构建成功和 API 后台出现调用记录，不等于业务审核通过。

- 待验证消息和普通发送队列仍以内存为主，重启可能丢失；人工审核队列已持久化。
- 删除话题在用户再次发送、检查旧 ID 时处理，不是实时收到删除事件就重置。仅关闭话题会尝试重新打开。
- AI 故障放行期间，广告漏判风险会上升；异常提醒也可能因 Telegram 权限或网络失败。
- 图片等内容审核可能发送至配置的 AI 服务；数据库保存用户信息、内容和审核记录，请妥善保护。
- 保存媒体使用 Telegram file_id，未下载独立备份；部分测验、游戏、故事无法完整恢复。
- 发送结果不确定的审核记录不自动重复发送，需管理员核对。
- QUEUE_TIMEOUT 当前仍未接入队列超时控制。
- VPS 合租业务背景写在 services/moderation_rules.py；其他业务场景需修改该文件并重新构建。

## 发布和许可证

本版沿用 [MIT License](LICENSE)，保留上游 LICENSE 中 Copyright (c) 2025 Sanite&Ava 的版权与许可声明。发布代码时请同时保留 LICENSE 和原项目来源链接。

上传前排除真实 .env、API 密钥、Bot Token、data 数据库、数据库 WAL/SHM、审核快照、用户数据和日志。示例配置只使用占位符。不要将含 Token 的运行日志放入 Issue 或截图。

感谢上游项目作者与贡献者，以及 python-telegram-bot、Google Gemini、OpenAI SDK 等依赖项目。

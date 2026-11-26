# 技术方案 v7.0 独立审计报告

**日期**：2026-11-26
**工作流**：工作流 1（综合代码/设计审查）+ 工作流 2（架构与可运维性评估）
**审计对象**：`docs/tech/2026-11-26-技术方案.md`（自称 v7.0 终版，状态"待复检；未施工"）
**上游依据**：`docs/prd/2026-11-25-PRD-v0.2.md`、`docs/specs/2026-11-25-知识库设计.md`、`AGENTS.md`
**参与成员**：Archi（架构）、Cody（代码/安全）、Rex（SRE）、Tessa（测试）、Docu（文档）
**审计方式**：只读审计，未修改方案原文任何字符。所有结论附外部可复核证据。

---

## 📌 TL;DR（执行摘要）

- **整体结论**：方案在"取舍论证"和"需求覆盖"上是本项目历版最好的一版，但**存在 6 处会直接卡住施工的硬错误**，其中 3 处（Python 版本、媒体读取、自签证书）会导致主链路跑不起来。当前状态**不具备"照着施工"的条件**。
- **最致命的一条**：方案把运行时锁定为 Python 3.11（§7.3），而全案最核心的抓取组件 `parsehub` 要求 `>=3.12.0`。这不是"可能装不上"，是**按方案写法一定装不上**，且它同时废掉了 G2c 关卡的设计前提。
- **严重度分布**：🔴严重 6 项 / 🟠高 11 项 / 🟡中 12 项 / 🟢低 4 项
- **阻塞 / 非阻塞**：6 项阻塞。其中"Python 版本"与"媒体读取端点"属于**方案必须改写**级别，不是补一句话能解决的。

---

## 🎯 核心结论卡片

| 项目 | 内容 |
|------|------|
| 整体评级 | 🔴 不通过（需修订后重新提交） |
| 阻塞项数量 | 6 |
| 关键行动项 | 9 条（见行动清单） |
| 建议下一步 | 先做 G0'（Python 3.12 + Postgres 18 在 proot 里的最小可运行验证），用真实输出决定 §7 技术栈能否成立，再改文档 |

---

## 🔍 审查发现（按严重度排序）

### 🔴 严重（方案不成立或必须改写）

| # | 严重度 | 类别 | 位置 | 问题描述 | 证据 | 建议修复 |
|---|--------|------|------|---------|------|---------|
| 1 | 🔴严重 | 正确性 | §7.3、§0.2 D4、§649 | **Python 版本与核心依赖冲突**：§7.3 定 "Debian 12 源内 Python 3.11"，并断言"3.11 满足本项目全部依赖"。但 `parsehub` 2.4.4 的 `requires_python` 是 `>=3.12.0`。 | PyPI `parsehub` 元数据实测：`requires_python: >=3.12.0`，classifier 仅列 `Python :: 3.12`。方案自己引用的 §4.7 也把它列为主力抓取。 | 二选一：① 改用 Python 3.12 并解决其来源（见 #2）；② 放弃 parsehub 走 §4.9 自写 CDP。**必须实测后重写 §7.3 与 D4**，不能留"若 G0 发现再升"这种话——依赖冲突是静态可查的，不该等到关卡才发现。 |
| 2 | 🔴严重 | 正确性 | §7.3 第 1 行、§7.1 | **`apt install postgresql-18 python3.12` 在 Debian 12 上必然失败**。方案写"全部走 Debian 源"，但 Debian 12 主源没有 postgresql-18，也没有 python3.12。 | Debian 官方包库实测：bookworm 的 `postgresql` 是 `15+248+deb12u2`；bookworm 与 bookworm-backports 搜索 `python3.12` 均返回 "no results"；`python3.12` 只在 trixie（13）及以后提供。trixie 的 postgresql 是 `17+278+deb13u1`——**也没有 18**。 | 换 Debian 13（trixie，自带 17 + 3.12）或加 PGDG 第三方源装 18。任一选择都要重写 §7.3 并说明"不是全部走 Debian 源"。顺带：§7.3 自己第 2 行说"改用 3.11 更稳"，第 1 行却还在 `apt install python3.12`，同节自相矛盾。 |
| 3 | 🔴严重 | 正确性 | §6.3、§8.1 | **媒体文件没有任何读取出口**。封面存到 `data/media/cover/<hash>.webp`、`items.cover_key` 存 key，但 22 个端点里没有 `GET /api/media/{key}` 之类。封面无法显示。 | 逐条核对 §8.1 的 22 个端点，与媒体相关的只有 `/api/export`（全量导出）。`media` 一词在全文出现 17 次，全部是"存"和"导出"，无一次"读"。 | 增加媒体读取端点（带鉴权 + `Cache-Control`）。否则 PRD M2-03"封面存自己存储"存了等于没存，M7-01"卡片网格，封面为主视觉"直接不成立。 |
| 4 | 🔴严重 | 安全 | §2.2、§7.1 | **自签证书会让 APK 打不开站点**。方案用 Caddy `tls internal`（自签），并说"对 APK 无感（地址写死在应用内）"——混淆了"地址"与"证书信任"。Capacitor WebView 走系统信任链，Android 7+ 起 App 默认不信任用户安装的 CA，Android 14 更取消了系统级注入自签 CA 的能力。 | 方案 §2.2 原文。Android 安全模型：应用私有信任库 + `network_security_config.xml` 才能信任自定义 CA；未配置时 WebView 报 `ERR_CERT_AUTHORITY_INVALID`。 | **推荐改用 Tailscale 自带的 `ts.net` 证书**（`tailscale cert` 签 Let's Encrypt，免费、被系统信任、不需要域名备案），从根上消除自签问题。若坚持自签，必须在 APK 内打包 CA 并提供 `network_security_config`，且要写进方案。这是手机入口（M1-02，PRD 称"最重要的入口"）能否成立的前提。 |
| 5 | 🔴严重 | 正确性 | §8.1、§8.4 | **首个账号无法创建（鉴权死锁）**。§8.4 写"首个注册者即唯一用户；关闭后续注册"，但 §8.1 的 22 个端点里没有注册端点，只有 `login`/`logout`。 | §8.1 端点表逐条核对：`/api/auth/login`、`/api/auth/logout`，无 register / signup / bootstrap。 | 二选一：① 加 `POST /api/auth/bootstrap`（仅当 `users` 表为空时可调用，建完即永久关闭）；② 提供 CLI 初始化命令并写进部署步骤。必须明确，否则 M0"能登录"这一步卡死。 |
| 6 | 🔴严重 | 依赖正确性 | §4.7 | **依赖可安装性结论不可信，且被当作"已验证"**。§4.7 称 24 个依赖里 22 个可直接装、1 个需编译。实测：`signerpy` 是**纯 Python**（`setup.py` 用 `setuptools.find_packages()`，无 `.c/.pyx/.cpp`，`python_requires>=3.6`），根本不需要编译；但它有 2 个方案未列的传递依赖 `user_agent`、`pycryptodome`。 | 下载 `signerpy-0.12.0.tar.gz` 解包核对 `setup.py` 与目录结构，无任何 C 扩展源文件。PyPI 实测 `user_agent` 0.1.14、`pycryptodome` 4.6.0 存在。另：`ffprobe` 只在 T9 测试出现，§4.7 依赖清单与 §7.3 系统包清单**都没有 ffmpeg**。 | 重新做依赖核查，并把"静态分析"与"实测"分开标注（见 #16）。补齐 `user_agent`、`pycryptodome`、`ffmpeg`。 |

### 🟠 高（会导致施工卡住或返工）

| # | 严重度 | 类别 | 位置 | 问题描述 | 证据 | 建议修复 |
|---|--------|------|------|---------|------|---------|
| 7 | 🟠高 | 可用性 | §3.2、§6.3 | **"备份"与数据在同一台设备上**。restic 仓库位置写 `data/backups/`，与 `pg/`、`media/` 同盘。平板损坏/丢失即全丢。这不满足 PRD M9-06"云端每日快照"。 | §6.3 存储布局原文；§3.2 restic 行未提异地。 | 至少一条异地副本（网盘 rclone / 电脑定时拉取 / 另一块可移动盘）。restic 支持多 backend，改的是 `-r` 目标，成本很低。**这条不修，M4"防丢失"整章失去意义**。 |
| 8 | 🟠高 | 可用性 | §2.1、§2.3、§7.1 | **proot 下 Postgres 与进程守护未做技术论证**。§2.1 只论证了"Python 轮子的 libc"，完全没论证 Postgres 本身：它依赖共享内存（System V / POSIX）。方案全文未出现 `sysv`/`shm`/`shared_memory`/`postgresql.conf` 任一词。另：§2.3 用 `termux-services`(runit) 守护 api/worker，但 api/worker 跑在 proot Debian 内，`termux-services` 是 Termux(bionic) 侧的包，proot 内不可用；且 proot 内 systemd 不可用。 | 全文检索确认上述关键词 0 次命中。技术事实：proot 提供 sysv IPC 模拟（`proot-distro` 有 `--no-sysvipc` 开关，说明默认模拟），但 Postgres 在 proot 内通常需设 `shared_memory_type=mmap` 或 `dynamic_shared_memory_type=posix`；proot 无 PID 命名空间，systemd 无法作为 PID 1 运行。 | 把"Postgres 在 proot 内的启动参数 + 守护方式"写成明确小节：proot 内用 Debian 的 `supervisord`/`s6`/`runit`（Debian 包版），不是 `termux-services`。G0 关卡增加"重启后四服务全自动恢复"的判据。 |
| 9 | 🟠高 | 可施工性 | 全文 | **没有 AI 层设计**。M3-02/03/04/05 全是 P0，但方案里没有：模型选哪个、prompt 长什么样、一次调用怎么同时出摘要+标签、失败怎么降级、M3-04"只能从词表挑"的"落库前二次校验"具体怎么校验、M9-03 写入限流与 AI 额度上限怎么实现。 | 全文 `prompt` 出现 1 次（正文提及），无 prompt 正文；无具体模型名；`额度` 0 次；`API_KEY`/`环境变量` 0 次。附录 A 里 M3-05 的章节栏是"—"。 | 补一节「AI 层」：模型与备选、调用参数、prompt 模板、结构化输出校验（必须命中词表）、超时与重试、月度额度上限与超限行为、M9-07 用量记录表。这是 5 条 P0 的唯一落点。 |
| 10 | 🟠高 | 可施工性 | 全文 | **没有项目结构 / 模块划分 / 依赖方向**。§11.2 突然出现 `extract/`、`web/`、`capture` 三个路径，但全文没有目录树。agent 无法据此决定文件放哪、谁能 import 谁。 | 全文检索无目录树；`extract/` 仅在 §11.2 出现。 | 补目录树 + 每层职责 + import 白名单（正好能落实 §11.2 的五条架构检查）。 |
| 11 | 🟠高 | 正确性 | §6.1 jobs | **异步流水线状态机缺失**。`jobs.step` 只有 fetch/summarize/tag/ocr，`jobs_pending_uniq` 只保证同 step 不重复。但一条内容要走 fetch → summarize → tag（tag 依赖 summary），谁在 fetch 成功后创建下一步？失败重试 3 次后置 `failed` 的判定与退避在哪？ | §6.1 DDL 与 §8.1 端点均未描述 step 转移；§附录A M4-06 只写"`jobs.attempts` + 指数退避"，无实现位置。 | 明确"每步完成后由 worker 在同事务内插入下一步 job"或"单 job 多 step 串行推进"，并写出 `attempts>=3 → failed` 的 SQL 条件。否则后台链路无法实现。 |
| 12 | 🟠高 | 数据完整性 | §6.1 tags | **两级标签触发器有 UPDATE 漏洞**。触发器只检查"新父节点是否一级"，未检查"自己是否已有子节点"。把根节点 A（已有子标签）的 parent 改成根节点 B，A 变二级、A 的子标签变三级，触发器放行。 | 触发器函数原文仅 `IF (SELECT parent_id FROM tags WHERE id = NEW.parent_id) IS NOT NULL THEN RAISE`。 | 触发器加分支：`IF NEW.parent_id IS NOT NULL AND EXISTS (SELECT 1 FROM tags WHERE parent_id = NEW.id) THEN RAISE`。并补一条测试（T-标签三级-反向）。 |
| 13 | 🟠高 | 正确性 | §8.1、§6.1 | **回收站与数据清理链路不完整**。有 `/restore`，但没有"列出回收站"的端点（用户找不到可恢复的条目）；PRD 要求"30 天后物理清除"，方案没有这个定时任务（APScheduler 只配了"每日 22:30 备份""开机补跑"）。`domain_type_rules` 也没有 GET/DELETE 端点，导致 PRD V7"规则可见、可删"无法验收。 | §8.1 端点清单；§3.2 APScheduler 行；`domain_type_rules` 表存在但无端点。 | 补三个端点：`GET /api/items?deleted=1`（或 `/api/trash`）、`GET /api/domain-rules`、`DELETE /api/domain-rules/{domain}`；把"30 天清理"加进调度并写明删除顺序（先 media 后 row）。 |
| 14 | 🟠高 | 可验收性 | 全文 | **PRD 的 V1–V9 验收标准在方案里一次都没被引用**，PRD §8 的 M0/M1/M2/M3 里程碑也没有映射到 G0–G6。方案只有技术关卡，没有"做到哪一步算 M1 能存"。 | 全文检索 `V1`–`V9` 命中 0 次。§10 只有 G 系列。 | 增加一张映射表：`里程碑 → 关卡 → 验收项`。特别是 M1（三条链接+两张图能存并带摘要标签）必须有一个可执行判据，否则 agent 不知道何时算"做完"。 |
| 15 | 🟠高 | 正确性 | §2.2 | **`0.0.0.0:8443` 与"仅 Tailscale 网段可达"矛盾**。`0.0.0.0` 会监听所有网卡，包括平板所连 WiFi 的局域网。同网段设备可通过 `平板局域网IP:8443` 访问登录页。 | §2.2 表格原文。 | 改绑 Tailscale 接口地址（`100.x.y.z:8443`）或在 Caddy 加 `remote_ip 100.64.0.0/10` 限制；同时说明"Tailscale 本身已加密，Caddy 可只绑 tailscale0"。 |
| 16 | 🟠高 | 措辞准确性 | §12.1、§0.3 | **"已验证 16 项，均有实测输出"含静态分析**。R16 明写"静态分析"，R7 是"有 aarch64 静态包"（查 release 页面可得），R5 是"Android 内核事实"。把它们与"实测 HTTP 200 下载到 3.9MB 视频"混为一谈，会让复检者高估验证程度。 | §12.1 表内 R16 自述"（静态分析）"；§0.3 却把该项计入"已实测通过 16 项"。 | 拆成三档：`实测（有输出）` / `查证（有外部来源）` / `推断`。数量按档重算。这是 AGENTS.md 第五节"不确定就标注"的直接要求。 |
| 17 | 🟠高 | 正确性 | §附录A | **`M9-03 写入限流` 的落点是空的**。附录 A 写"令牌桶（Caddy 或应用层）"，章节栏为"—"。而 Caddy 官方静态二进制**不含** `rate_limit`，需 `xcaddy` 自编译。 | Caddy 官方模块文档原文："This module does not come with Caddy. It can be added by using xcaddy or our download page." | 把限流做在应用层（Starlette 中间件 + Postgres/内存计数），与"不引入需自编译的二进制"这条原则（C3 组件越少越好）一致。改掉附录 A 的措辞。 |

### 🟡 中

| # | 严重度 | 类别 | 位置 | 问题描述 | 证据 | 建议修复 |
|---|--------|------|------|---------|------|---------|
| 18 | 🟡中 | 自相矛盾 | §6.1 标题 / §14 | 表数三处不一致：标题"表结构（8 张）"，§14 变更记录"11 张表"，实际 `CREATE TABLE` 共 **10** 张。 | 统计脚本：items/tags/item_tags/queries/files/jobs/domain_type_rules/settings/users/sessions = 10。 | 统一为 10。 |
| 19 | 🟡中 | 自相矛盾 | §8.1 标题 / §14 | 端点数不一致：标题"端点总表（15 个）"，§14"22 个端点"，实际表格 **22** 行。 | 统计脚本计数 22。 | 标题改 22。 |
| 20 | 🟡中 | 自相矛盾 | §0.2 D8 / §3.2 标题 | "复用 12 个组件""采用 12 项"不准确：§3.2 共 12 行，但结论是 **10 项"复用" + 2 项"参考"**（jieba、tauri 是"参考"）。 | 逐行统计结论列。 | 改"复用 10 项 + 参考 2 项"。否则"复用 12 个组件"这句话在复检时站不住。 |
| 21 | 🟡中 | 自相矛盾 | §4.4 标题 | "顺带覆盖 17 个平台"，表格只有 **8** 行。 | 表格数据行计数 8。 | 要么补足 17 行，要么改标题为 8；若 17 来自上游 README，需注明"上游声称 17，本文实测 8"。 |
| 22 | 🟡中 | 正确性 | §0.2 D4 / §7.2 / §4.7 | **三处对 `pydantic-core` 的表述互相冲突**：D4 说"是 Rust，Android 无轮子"；§7.2 说"校验用 Pydantic v2（纯 Python 部分可用）"；§4.7 又把它列为"有 manylinux aarch64 轮子"。 | PyPI 实测 `pydantic-core` 2.50.5 有 19 个 aarch64 轮子，含 `cp311-manylinux_2_17_aarch64`。同理 `psycopg-binary` 3.3.9 有 12 个、`argon2-cffi-bindings` 26.2.0 有 8 个。 | D4/D6 的**否决理由写错了**（"无 Android 轮子"在 proot glibc 下不成立）。选 Starlette/pg8000/bcrypt 本身可以用，但必须把理由改成真实理由（如启动更轻、依赖更少），否则违反"每条结论要么有实测证据"。 |
| 23 | 🟡中 | 数据完整性 | §6.1 | PRD §7 硬要求"每张表都有 `created_at`"，`item_tags` 与 `settings` 没有。 | DDL 逐表核对：`item_tags` 只有 item_id/tag_id/origin/confidence；`settings` 只有 key/value/updated_at。 | 补 `created_at`，或改 PRD（但要按 AGENTS.md 第六节写进"待确认问题"，不能默默忽略）。 |
| 24 | 🟡中 | 数据完整性 | §6.1 tags | 标签名未做规范化，`AI相关` 与 `ai相关` 会并存；唯一索引只按原字符串比较。PRD DP6/M3-04 的"标签爆炸"风险在这里重新出现。 | `CREATE UNIQUE INDEX tags_sibling_name_idx ON tags (coalesce(parent_id, 0), name)` 无 `lower()`/`btrim()`。 | 索引改 `(coalesce(parent_id,0), lower(btrim(name)))`，应用层同时做半角/全角与首尾空白归一。（注：`coalesce(...,0)` 本身安全——`bigserial` 从 1 起，已核实 DDL 无自定义起始值。） |
| 25 | 🟡中 | 正确性 | §5.1 / PRD M5-01 | **"6+1"是 PRD 留下的概念瑕疵，方案照抄未纠正**。PRD 列 6 项却写"6+1"；方案 §5.1 注释"6+1 里的那个 +1 = link"，但 link 就在那 6 项里。 | PRD M5-01 原文列 article/image_text/video/image/file/link 六项并称"6+1"；方案 CHECK 也是六项。 | 明确写"共 6 类，其中 link 为降级兜底类"。不要保留"6+1"这个无法自洽的说法——它是给 agent 的文档，歧义会被实现成第 7 个枚举值。 |
| 26 | 🟡中 | 范围一致性 | 全文 | **PRD v0.2 新增的 M10 阅读模块（M10-01/02/03/05 均 P1）在技术方案里完全不存在**。PRD 自称 M10 是"v0.1 最大的漏洞"，方案一句未提。 | 全文检索 `M10`/`高亮`/`批注`/`阅读进度`/`沉浸` 均 0 次命中。 | 至少写清"M10 属 P1，MVP 不做，接口/表结构预留位置"。否则 PRD 与方案的范围不一致。另：方案 §12.3 承认抖音无可读正文，这直接削弱 M10 的价值，两者应交叉说明。 |
| 27 | 🟡中 | 正确性 | §13 Q2 | **磁盘容量算术不成立**。原文"3.9MB/条，一天 10 条 ≈ 1.2GB/月，256GB 撑不到一年"。按同句数字：1.2GB × 12 = 14.4GB，256GB 可用约 17 年。 | 方案自身数字自相矛盾。 | "按需存"的结论可以保留（理由应改为"备份体积/传输时间"），但算错的数量级必须改。AGENTS.md 要求"数值附口径与单位"，这条正好违反。 |
| 28 | 🟡中 | 正确性 | §6.1 全表 | **时区未交代**。`saved_at`/`published_at` 用 `timestamptz` 是对的，但 PRD"这周存的"（M5-06）、日期视图（M7-12，P0）都依赖"本地日"边界；方案未说明按哪个时区切日。 | 全文无 `timezone`/`Asia/Shanghai`/`时区` 相关表述。 | 明确"存储 UTC，展示与分桶按 `Asia/Shanghai`"，并在查询层给出转换方式。 |
| 29 | 🟡中 | 安全 | §8.1 | 上传与查询无上限：`POST /api/items/upload` 无大小/MIME 限制，`GET /api/items` 无 `limit` 上限，`GET /api/export` 全量 zip 未说明流式输出。8GB 内存平板上任一都可能 OOM。 | §8.1 端点表无参数说明；§8.3 只给了 `/api/items` 的示例。 | 补：上传单文件上限 + MIME 白名单、分页 `limit<=100`、导出用 `StreamingResponse` 逐条写 zip。 |
| 30 | 🟡中 | 可运维性 | 全文 | **没有告警/通知机制**。用户非技术背景，服务挂了只能看到"打不开"。`/api/health` 只回 `dbOk`，不检查 Meilisearch 与 worker。 | 全文 `告警`/`推送`/`通知` 相关 0 次实质设计；§10 G0 判据只有 `{"ok":true,"dbOk":true}`。 | health 增加 `meiliOk`、`workerHeartbeatAt`；增加"连续 N 分钟不健康 → 发一封邮件/一个 Bark 推送"的最小告警。否则 PRD M9-07"用量与花费可见"也无从落地。 |
| 31 | 🟡中 | 可维护性 | §8.2 | **Meilisearch 索引与 Postgres 的一致性无修复机制**：索引写入失败（Meili 挂/重启）时 items 已入库但搜不到，方案无对账/重建脚本。 | §8.2 只有三级降级查询，无索引重建。 | 补：`rebuild_index` 命令（从 Postgres 全量重灌）+ 每日对账（count 比对）+ Meili 不可用时写入失败进 `jobs` 重试。 |

### 🟢 低

| # | 严重度 | 类别 | 位置 | 问题描述 | 建议 |
|---|--------|------|------|---------|------|
| 32 | 🟢低 | 自相矛盾 | §7.3 第 1 行 vs 第 2 行 | 同一节里第 1 行 `apt install python3.12`，第 2 行说"改用 3.11 更稳"，第 3 段又说"终版统一为 3.11"。三句话三个状态。 | 与 #1/#2 一并重写。 |
| 33 | 🟢低 | 术语 | 全文 | `proot`、`manylinux 轮子`、`CDP`、`_ROUTER_DATA`、`Charabia`、`restic`、`zod/Drizzle/tRPC`、`runit` 等首次出现均无一句人话解释。AGENTS.md 第五节要求"术语首次出现配一句人话解释"，读者是非技术背景的验收方。 | 补术语表；至少让验收方能读懂 §2、§10。 |
| 34 | 🟢低 | 措辞 | §11.2 | "扫描 `extract/` 出现 `db.` 即失败"用纯文本扫描会误报（注释、字符串里出现即命中）。 | 明确用 AST（`ast.parse` 遍历 Import/Call）而非 grep。 |
| 35 | 🟢低 | 措辞 | §4.7 | 把 `yt-dlp` 列入"直接依赖"，但 §3.3 已"不采用"它抓抖音。实际它是 parsehub 的传递依赖。 | 区分"我们直接依赖"与"上游传递依赖"，并注明 yt-dlp 仅用于 B 站兜底（§5）。 |

---

## 🏗️ 架构影响评估

### 方案真正做对的地方（不因上述问题而抹掉）

| 项 | 评价 |
|---|---|
| 复用调研的深度 | §3.1 八个同类产品逐个给星数/许可/否决理由，§3.3 三条给了实测报错原文。这是本项目历版最扎实的一次调研，`doc_checks` 也通过。 |
| P0 覆盖 | 附录 A 的 41 条与 PRD 的 41 条 P0 **完全一一对应，无遗漏、无虚列**（已用脚本比对）。统计口径 7 复用/1 视 G3/33 自建也准确。 |
| 关卡的退路设计 | G0–G6 每关都写了"不过怎么办"，且 G2c 失败有已实测可用的 §4.9 退路。这是好设计。 |
| 反向测试意识 | §11.3 "验证检查本身有效"（如"用不存在的中文词搜，断言 0 条"）抓住了"永远返回通过"的假成功问题。 |
| 数据模型的核心约束下沉 | 类型 CHECK、任务幂等部分唯一索引、软删除——把约束放 DDL 而不是靠应用层自觉，符合 AGENTS.md 第八节。 |

### 架构层面的结构性判断

1. **"全栈跑在平板上"这个前提没有被验证，却承担了方案的全部重量**。§12.2 把 A5（保活）标为"高"，但 A2（四个服务共存的内存）也标"中"——8GB 平板里 Android 系统占 2–3GB，再跑 Postgres + Meilisearch + ython api + worker，方案从头到尾没做过一次内存预算。这不是"待验"，是**方案应该先算一遍的数**。
2. **"不注册域名"与"自签证书"被绑在一起，导致手机入口这条主线有两个独立失败点**（证书信任 + share_target 可用性）。而 Tailscale 本身就能签受信任的 `ts.net` 证书，方案没利用——这是把可以消掉的复杂度留下来了，与 C3"组件越少越好"相悖。
3. **抓取层的耦合度被低估**。parsehub 是一个 206 星、由单人维护的库，方案把"整个产品能否成立"（C7：抖音抓不通则产品不成立）压在它身上，同时它又锁死了 Python 版本。这构成了"单点依赖 + 版本锁定"的双重脆弱性，而 §4.8 的风险表只写了"停更→fork 自维护"，没写"它升级要求更高 Python 怎么办"。

---

## 🧪 测试与验收评估

| 项 | 现状 | 问题 |
|---|---|---|
| 功能测试 | T1–T12 共 12 条 | 全是单点功能测试，**没有一条覆盖"跨步骤串起来"**：存一条抖音 → fetch 成功 → summarize 成功 → tag 落库 → 可被搜到。这才是主链路。 |
| 外部依赖测试 | T7/T8 直接打真实抖音 | 抖音改版会让测试失败但产品没坏。方案未说明这类测试应标 `skip`/仅在 main 跑（项目已有 `slow` marker，可用而未用）。 |
| 隐藏依赖 | T9 依赖 `ffprobe` | §4.7/§7.3 都未列 ffmpeg（见 #6）。 |
| 并发 | T3 只测"连点两次" | 未定义是顺序还是并发；§11.3 要求"并发插两条 pending"，但未说明如何真并发（需两连接 + 事务隔离）。 |
| 缺失类别 | — | 无：升级迁移、worker 崩溃恢复（job 卡 `running`）、磁盘满、时区、大文件上传、Meilisearch 索引重建。 |
| 验收可测性 | G3 判据"排序合理" | "合理"无客观判据，无法判定通过或失败。V3"500 条按下就出结果"无毫秒阈值（PRD NF4 称这是"唯一必须量化的"）。 |
| 里程碑 | — | 无 M1 判定测试（见 #14）。 |

---

## ✅ 行动清单（按优先级排序）

| # | 行动 | 负责角色 | 紧急度 | 预期完成 |
|---|------|---------|--------|---------|
| 1 | 在平板上实跑 G0'：装 proot Debian，验证 **Python 3.12 来源**（trixie 升版 / 自编译 / pyenv）+ `pip install parsehub` + Postgres 启动参数（`shared_memory_type=mmap`）+ 四服务共存内存占用。**用真实输出决定 §7 技术栈**。 | 用户 + AI | P0 | 改文档前 |
| 2 | 二选一写死 Python 版本：若 G0' 证明 3.12 可用 → 全案改 3.12；若不可用 → 放弃 parsehub 走 §4.9。删除"若 G0 发现再升"这类悬置表述。 | Archi | P0 | 同上 |
| 3 | 补 `GET /api/media/{key}`（带鉴权）与 `POST /api/auth/bootstrap`；补回收站列表、`domain_type_rules` 的 GET/DELETE、"30 天清理"定时任务。 | Cody | P0 | 同批 |
| 4 | 证书方案改 `tailscale cert`（`ts.net` + Let's Encrypt），或明确写 `network_security_config` + CA 打包。同步改 §2.2 的 `0.0.0.0`。 | Rex | P0 | 同批 |
| 5 | 备份加一条异地副本（rclone/网盘/电脑拉取），改 §3.2 与 §6.3。 | Rex | P0 | 同批 |
| 6 | 补「AI 层」一节（模型/prompt/结构化校验/额度/用量表），补目录结构与模块划分。 | Archi + Cody | P1 | 第二轮 |
| 7 | 重做依赖核查：区分"直接/传递"、"实测/静态"，补 `user_agent`、`pycryptodome`、`ffmpeg`；修正 `signerpy` 的"需编译"结论。 | Cody | P1 | 第二轮 |
| 8 | 修 `tags` 触发器 UPDATE 漏洞 + 标签名归一化索引；补 `item_tags`/`settings` 的 `created_at`。 | Cody | P1 | 第二轮 |
| 9 | 增加「里程碑 → 关卡 → 验收」映射表；给 V3 定毫秒阈值、给 G3 换可判定判据；补 M1 判定测试与主链路端到端测试。 | Tessa | P1 | 第二轮 |
| 10 | 统一全部数字（10 表 / 22 端点 / 10 复用+2 参考 / 8 平台）、拆"已验证"三档、修 §13 Q2 容量算术。 | Docu | P2 | 定稿前 |

---

## ⚠️ 待完善 / 已知局限

- **本次是文档审计，不是实跑验证**。除外部事实（PyPI/Debian/GitHub API/Caddy 文档）为实测外，方案内部的工程结论（proot 性能、保活、内存占用）我无法在无设备条件下验证——这正是行动项 1 要做的。
- 未审的部分：前端设计（方案本身没有前端章节，无内容可审）、APK 侧 Kotlin 分享桥的实现细节（方案仅一句"写 Kotlin 分享桥"）。
- 关于 `coalesce(parent_id, 0)`：成员之间曾有分歧，我已裁定为**不构成缺陷**（`bigserial` 从 1 起，0 永不与真实 id 冲突），但标签名大小写未归一是真问题（#24）。
- 关于 Meilisearch 版本：方案 §12.1 R7 写 v1.58.0、334.9MB，实测 GitHub release 确有 `v1.58.0`（2026-11-23 发布），**此项准确**，不计入问题。
- 严重度分级是判断而非事实。若用户认为"平板坏了也无所谓"，则 #7 备份项可降级；但按 PRD M4"防丢失"的表述，我保留 🔴/🟠 判定。

---

## 📚 数据来源 & 成员产出索引

### 外部事实（本次实测，可复核）

| 事实 | 来源 | 结果 |
|---|---|---|
| `parsehub` 要求 Python | PyPI JSON API | `requires_python: >=3.12.0`，仅 `3.12` classifier |
| `parsehub` 依赖清单 | PyPI JSON API | 23 项，含 `signerpy>=0.12.0`、`yt-dlp[default]`、`opencv-python-headless>=5.0.0.93` |
| `signerpy` 是否需编译 | 下载 sdist 解包 | 纯 Python，无 C/Cython 源文件，`python_requires>=3.6` |
| `signerpy` 隐藏依赖 | sdist `setup.py` | `user_agent`、`requests`、`pycryptodome`（前两者未在方案清单） |
| Debian 12 postgresql 版本 | packages.debian.org | `15+248+deb12u2` |
| Debian 13 postgresql 版本 | packages.debian.org | `17+278+deb13u1`（**也无 18**） |
| Debian 12 python3.12 | packages.debian.org 搜索 | bookworm 与 bookworm-backports 均 "no results"；仅 trixie 有 |
| Debian 12 python3.11 | sources.debian.org | `3.11.2-6+deb12u8` |
| aarch64 轮子 | PyPI JSON API | pydantic-core 19 个、psycopg-binary 12 个、argon2-cffi-bindings 8 个 |
| Caddy 限流是否内置 | caddyserver.com 模块文档 | 原文："This module does not come with Caddy." |
| Meilisearch v1.58.0 | GitHub releases API | 存在，2026-11-23 发布 |

### 成员原始产出

| 成员 | 负责维度 | 主要贡献 |
|---|---|---|
| Archi（架构师） | 架构自洽、部署栈、数量一致性 | 发现 proot 下 Postgres/守护进程未论证、6+1 歧义、模块划分缺失 |
| Cody（代码审查师） | DDL、接口、鉴权、安全 | 发现鉴权死锁、媒体读取缺口、触发器 UPDATE 漏洞、端点缺口清单 |
| Rex（SRE） | 部署、保活、备份、可运维 | 发现备份同盘、内存未预算、health 不覆盖 Meili、0.0.0.0 矛盾 |
| Tessa（测试专家） | 测试完备性、验收可测性 | 发现无主链路端到端测试、G3 判据不可判定、缺里程碑映射 |
| Docu（文档师） | 可施工性、术语、一致性 | 发现无目录结构/配置/启动脚本、AI 层缺失、措辞夸大 |

---

> 本报告由工程保障团队 AI 协作生成，关键决策请由人类工程负责人复核。
> 审计未修改 `docs/tech/2026-11-26-技术方案.md` 任何内容。
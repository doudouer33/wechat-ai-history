# Phase 1：现有项目验证与全量解密基准

验证日期：2026-11-01，Asia/Tokyo（UTC+09:00）。代码基线：`2f2bd12`，分支：`feat/incremental-sync`。

## 结论与验收状态

已执行 Phase 1 的环境检查、真实密钥提取、全量解密、查询验证和基准保存。当前账号的 26 个数据库全部解密成功，消息、联系人、会话等 23 个数据库的完整 `PRAGMA integrity_check` 返回 `ok`；用户指定群的历史记录和长文本验证通过。

**不能宣称全部数据库完整性验收通过。** 另外 3 个全文索引库缺少微信专用 `MMFtsTokenizer`，标准 SQLite 无法完成全库检查。它们的普通表/影子表部分检查全部通过，但不等同于全文索引逻辑完整性通过。该限制需要在后续验证中保留。

| 验收项 | 实测结果 |
|---|---|
| 现有自动化测试 | 281 passed，42.74 秒，无失败或跳过 |
| 真实环境密钥提取 | 成功，候选通过现有提取器校验；后续解密逐页 HMAC 验证通过 |
| 全量解密 | 当前账号 26/26 成功，0 个库解密失败；另一账号因密钥不匹配被排除 |
| 完整性检查 | 23 个库 `ok`；3 个全文索引库检查受限，不能记为通过 |
| 既有端到端测试 | UTF-8 环境下 13/13 PASS |
| 指定群历史读取 | 唯一匹配，返回 560 条；CLI JSON 与 Python 接口结果一致 |
| 文本、发送者、时间戳 | 502 条普通文本全部核对通过 |
| 长文本 | 135 条超过 500 字的普通文本全部完整，最长 1296 字 |
| 后续对比基准 | 已保存本地明文快照、逐库及逐消息表摘要、表结构和性能记录 |

本阶段没有实现 Phase 2 增量同步，也未重构现有提取、解密或查询代码。

## 1. 环境与代码检查

| 项目 | 实测值 |
|---|---|
| 操作系统 | Windows 11 x64，系统版本 `10.0.26300` |
| 微信进程 / 文件版本 | `Weixin.exe` / `4.1.15.56` |
| Python | 3.12.4，64 位；满足 `pyproject.toml` 的 `>=3.10` 要求 |
| Python 环境 | 仓库内新建 `.venv`，按 `requirements-dev.txt` 安装 |
| Node.js | v24.22.1，用于执行提取器内嵌 JavaScript 的合成测试 |
| SQLite | Python 内置 SQLite 3.45.3 |
| 依赖版本 | pytest 9.2.0；frida 17.25.0；pycryptodome 4.2.0；mcp 1.30.0；zstandard 0.25.0 |
| 依赖一致性 | `python -m pip check`：No broken requirements found |
| 执行权限 | 当前终端不是管理员；本次对同一用户微信进程的提取实际成功，无需 UAC |
| 数据目录 | 非默认位置，已从本机配置定位；运行时显式设置 `WECHAT_DATA_ROOT` |
| 账号范围 | 找到 2 个账号目录，使用密钥验证选择可解密账号 |

已阅读 README、中英文使用说明、架构与设计决策、Windows 提取/数据模型/WAL 文档，以及提取、解密、WAL、数据库访问、消息解码、查询和测试源码。当前实现关系如下：

- `extract_raw_key.py`：在用户从桌面重启微信时挂钩 SHA-512 入口，验证并保存账号 raw key，不回显密钥。
- `decrypt_all.py`：通过密钥匹配账号，经 `wal_snapshot.read_snapshot()` 合并已提交 WAL 页面，逐页验证 HMAC 后解密，原子替换明文输出。
- `wal_snapshot.py`：验证 WAL header、generation salts、滚动校验和与提交边界；读取期间文件元数据变化时重试，持续不稳定则失败。
- `decrypt_stream.py`：已有逐页流式实现，但与批量解密的错误处理、一致性和输出逻辑不同；本次正式基准使用 `decrypt_all.py`，未将流式版本视为已验证的增量实现。
- `db.py` / `query.py` / `message.py`：Windows 使用明文镜像，数据库连接 `mode=ro` 并启用 `query_only`；按分片解析 `Name2Id`，解码 zstd 正文并保留普通文本与换行。

## 2. 自动化与真实环境执行

执行命令（仓库根目录，数据根目录使用本机实际值）：

```powershell
$Python = '.\.venv\Scripts\python.exe'
$env:WECHAT_DATA_ROOT = '<本机 xwechat_files 目录>'
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
& $Python -m pytest -q
& $Python -m pip check
& $Python -m compileall -q appmsg.py config.py contacts.py crypto.py db.py message.py server.py xmlmsg.py scripts/common scripts/windows
& $Python -c "import asyncio, server; assert len(asyncio.run(server.mcp.list_tools())) == 7"
& $Python scripts/windows/extract_raw_key.py 300
# 提取器提示后，用户从桌面手动启动微信并完成登录。
& $Python scripts/windows/decrypt_all.py
& $Python e2e/test_e2e.py
& $Python scripts/common/doctor.py --json
```

本次全量解密由本地计时包装器直接调用现有 `decrypt_all.main([])`，没有通过命令行传递 raw key。计时包含账号匹配、读取、WAL 快照处理、验证、解密和输出，不含安装依赖、人工登录和提取器等待时间。

源码编译检查、`setup.ps1` 的 PowerShell Parser 语法检查均通过。MCP 注册工具数为 7；仅验证了服务加载与工具枚举，没有将其等同于客户端连接验收。

首次运行端到端测试时仅设置 `PYTHONIOENCODING=utf-8`，子进程输出为 UTF-8，父进程 `subprocess(..., text=True)` 仍按本机 GBK 解码，导致 8 项失败并出现 `NoneType.strip` 后续错误。补充 `PYTHONUTF8=1` 后原测试代码 13/13 通过。两次日志均保留，本次没有修改测试掩盖失败。

解密后的 `doctor.py` 返回 `ok=true`，密钥、数据库和运行依赖检查通过。Skill 安装、Codex/WorkBuddy MCP 注册以及可选语音模型/后端仍为 warning；本阶段直接使用仓库接口，没有修改客户端注册或下载语音模型。

## 3. 解密和完整性基准

全量解密耗时 **14.322 秒**。输出 **26 个数据库，共 253,267,968 字节（约 241.54 MiB）**，共 **61,833 页**，页大小均为 **4096 字节**，SQLCipher 保留区域为 80 字节。每个输出文件大小均等于 `page_count × page_size`。

| 数据库（相对 db_storage） | 字节数 | 页数 | 完整 integrity_check |
|---|---:|---:|---|
| `bizchat/bizchat.db` | 28,672 | 7 | ok |
| `chatbot/chatbot_message.db` | 86,016 | 21 | ok |
| `contact/contact.db` | 5,382,144 | 1,314 | ok |
| `contact/contact_fts.db` | 1,998,848 | 488 | 受限 |
| `emoticon/emoticon.db` | 106,496 | 26 | ok |
| `favorite/favorite.db` | 159,744 | 39 | ok |
| `favorite/favorite_fts.db` | 32,768 | 8 | 受限 |
| `general/general.db` | 2,060,288 | 503 | ok |
| `hardlink/hardlink.db` | 692,224 | 169 | ok |
| `head_image/head_image.db` | 3,665,920 | 895 | ok |
| `message/biz_message_0.db` | 50,851,840 | 12,415 | ok |
| `message/biz_message_1.db` | 409,600 | 100 | ok |
| `message/biz_message_2.db` | 143,360 | 35 | ok |
| `message/media_0.db` | 8,417,280 | 2,055 | ok |
| `message/media_1.db` | 1,499,136 | 366 | ok |
| `message/message_0.db` | 34,811,904 | 8,499 | ok |
| `message/message_1.db` | 19,234,816 | 4,696 | ok |
| `message/message_2.db` | 4,464,640 | 1,090 | ok |
| `message/message_3.db` | 22,183,936 | 5,416 | ok |
| `message/message_fts.db` | 74,780,672 | 18,257 | 受限 |
| `message/message_resource.db` | 6,615,040 | 1,615 | ok |
| `message/weclaw.db` | 4,096 | 1 | ok（空结构） |
| `session/session.db` | 692,224 | 169 | ok |
| `sns/sns.db` | 14,929,920 | 3,645 | ok |
| `solitaire/solitaire.db` | 4,096 | 1 | ok（空结构） |
| `third_app_icon/third_app_icon.db` | 12,288 | 3 | ok |

完整性检查尝试合计约 0.853 秒。3 个受限库均报 `no such tokenizer: MMFtsTokenizer`。另外使用 `PRAGMA integrity_check('<普通表名>')` 做部分表检查：联系人索引 31/31、收藏索引 6/6、消息索引 52/52 返回 `ok`；分别有 6、1、8 张虚拟表未获完整验证。部分检查不能证明虚拟索引逻辑、跨表关系及全库 freelist 的完整性。

上述结果未发现普通库损坏，缺少 tokenizer 也不能被解释为数据库损坏。本阶段不注册替代 tokenizer、不重建原始索引，不修改微信源数据库。

## 4. 消息表和唯一标识

全部数据库共有 315 张 `Msg_*` 表、95,816 条消息表记录；其中现有查询接口使用的 `message_0.db` 至 `message_3.db` 共 4 个分片、247 张消息表、84,023 条记录，占 80,695,296 字节。前一总数包含 `biz_message_*` 中的消息表，不应与普通聊天查询的覆盖范围混淆。

315 张消息表的列结构一致：

```text
local_id INTEGER PRIMARY KEY
server_id INTEGER
local_type INTEGER
sort_seq INTEGER
real_sender_id INTEGER
create_time INTEGER
status INTEGER
upload_status INTEGER
download_status INTEGER
server_seq INTEGER
origin_source INTEGER
source TEXT
message_content TEXT
compress_content TEXT
packed_info_data BLOB
WCDB_CT_message_content INTEGER
WCDB_CT_source INTEGER
```

`Name2Id` 包含 `user_name TEXT PRIMARY KEY` 和 `is_session INTEGER`，发送者映射通过该分片的 `Name2Id.rowid = real_sender_id` 获取。`Msg_*` 表名由会话用户名的 MD5 生成。

`local_id` 的唯一性限于单表；基准定位键使用 **数据库相对路径 + 表名 + local_id**，同时保留 `server_id` 等字段供后续比较。普通聊天分片中有 **12 条 `server_id` 为 0 或 NULL**，因此不能只用它识别所有消息；各单表内未发现非零 `server_id` 重复组。本次没有据此断言非零 `server_id` 跨分片或跨会话全局唯一。

## 5. 指定群与长文本验证

目标为用户明确指定的微信群，报告使用匿名指代，未写入群名、群 ID、成员名称或正文。通过既有会话列表与联系人查找接口得到唯一匹配，再用其确切 ID 调用 `read_chat(limit=2000, days=36500)`，读取该本地快照中全部 560 条目标群记录。该结果仅代表当前电脑已保存的数据，不代表微信服务器中的完整历史。

会话列表共 1002 项，含 45 项群聊。针对目标群，将查询结果与明文快照的直接只读 SQL 结果比较：

- 普通文本 502 条，逐条时间戳和正文一致，文本结果多重集一致，保留重复值的计数。
- 502 条均可解析发送者映射；独立解压后的群消息发送者前缀与同分片 `Name2Id` 映射 502/502 一致，再核对现有接口的“我/对方”方向，全部一致。现有 `read_chat` 格式化输出只暴露发送方向，没有单独返回发送者姓名/ID。
- 其中 484 条使用 zstd 压缩，使用 `zstandard` 独立解压并移除发送者封套，与查询正文比较。
- 135 条普通文本超过 500 个字符，全部逐字一致；最长 1296 个字符。486 条含换行，保留完整换行。
- `query.py list --json` 与 `read --json` 均正常返回可解析 JSON；CLI 的 560 条读取结果与 Python 接口完全一致。

## 6. 查询性能

以下均为本次机器上的单次观测，不是平均值、P95 或冷磁盘性能；完整性扫描已预热部分操作系统文件缓存。“首次”指该验证进程首次调用相应接口，群读取前已执行会话查找。

| 操作 | 耗时 |
|---|---:|
| 全量解密（26 库） | 14.322 秒 |
| 会话列表，进程内首次调用 | 64.39 毫秒 |
| 会话列表，再次调用 | 15.71 毫秒 |
| 按指定名称查找群 | 14.03 毫秒 |
| 群历史读取，首次调用，返回 560 条 | 57.16 毫秒 |
| 群历史读取，再次调用 | 58.21 毫秒 |
| CLI 会话列表，含 Python 启动与 JSON 解析 | 373.81 毫秒 |
| CLI 群历史读取，含 Python 启动与 JSON 解析 | 385.81 毫秒 |

这些数据是静态明文快照读取基准，不能用于宣称增量同步延迟、并发正确性或长期稳定性已验证。

## 7. 本地基准产物与 Git

敏感原始数据保留在本机，公开报告只含汇总、通用表结构和相对数据库文件名：

| 本地产物 | 用途 |
|---|---|
| `key_windows.txt` | 提取后的本地密钥，已被 Git 忽略 |
| `decrypted/` | 现有查询接口使用的明文镜像，已被 Git 忽略 |
| `.local/phase1/snapshot/` | 独立保留的本次明文基准，后续重解密镜像不会覆盖它 |
| `.local/phase1/inventory.json` | 每库大小、页状态、SHA-256；每消息表按 local_id 排序后全部字段的带长度前缀序列摘要与记录数 |
| `.local/phase1/schemas.json` | 消息列结构及其覆盖表数 |
| `.local/phase1/*summary.json` | 环境、解密、查询、身份字段及发送者前缀验证统计 |
| `.local/phase1/target-metadata.json` | 本地目标群定位信息，不提交 |
| `.local/phase1/fts-partial.json` | 全文索引普通表部分检查结果 |
| `.local/phase1/*.log` | 自动化、端到端和解密日志，仅本地保留；端到端原脚本会输出部分会话标识 |
| `.local/phase1/environment.ps1` | 本次复查使用的本地数据根目录与 UTF-8 环境配置 |

保留快照可供后续逐消息比较，不能只靠消息数量或整表摘要判断差异原因。密钥、数据库、日志或聊天记录仅保留在本机；Git 交付内容只包含脱敏文档与忽略规则。

本次完善 `.gitignore`，补充 `.local/`、数据库 WAL/SHM/journal、SQLite 文件和临时解密产物；已有密钥与 `decrypted/` 忽略规则继续生效。检查时未发现数据库或密钥产物已被 Git 跟踪。

## 8. 已发现问题与后续边界

1. **全文索引完整性未完成验收**：缺少微信专用 tokenizer，3 库仍有明确验证缺口；普通消息、联系人和会话库均通过全库检查，当前 Python 搜索接口不依赖这些虚拟索引。
2. **Windows 端到端脚本受编码配置影响**：当前脚本使用隐式子进程文本编码。本次通过显式 UTF-8 环境复测通过；可在后续维护时单独修正，未为此大改代码。
3. **文档与当前代码有漂移**：部分旧文档声称必须全模块挂钩、管理员权限或使用不同提取入口，且部分 README 的 MCP 工具数量/CI 位置与当前源码不一致。本次实际由 `extract_raw_key.py` 在非管理员终端挂钩 `Weixin.dll` 的两个入口成功，MCP 为 7 工具，CI 位于 `.github/workflows/tests.yml`；不能将这一次权限成功推广到所有机器。
4. **现有流式脚本有已复现缺陷，不能直接充当增量模块**：`decrypt_stream.py` 在没有 WAL 提交时，循环前和第一页分支各写一次 SQLite 文件头。使用现有测试的合成加密页复现：输入 8192 字节，脚本返回成功却输出 8208 字节，前 32 字节是重复文件头，输出不再按 4096 字节对齐。正式基准使用 `decrypt_all.py`，不受此缺陷影响。复现统计保存在 `.local/phase1/stream-repro-summary.json`；按计划先报告原有问题，本阶段未修改其逻辑。流式版本独立解析 WAL，校验失败处理、源文件变化检测及临时文件发布也需要后续审查。
5. **未覆盖的真实动态场景**：未主动发消息，未测试持续同步、Checkpoint/WAL Reset、断点恢复、休眠唤醒或长期资源占用。这些属于 Phase 2/3；现有合成 WAL 测试通过不等于真实动态场景验收。

Phase 1 调查与报告交付完成；核心聊天读取链路已得到可复核基准，3 个全文索引库的全量完整性检查保留未通过验收状态。进入 Phase 2 时仍须先做专项路线分析，并按计划经用户确认后开始增量实现。

交付前已追加运行账号可移植性与公开文档隐私回归测试，14 passed（7.52 秒）；另检查本次交付文本未包含本地密钥、账号或目标群标识，`git diff --check` 通过。

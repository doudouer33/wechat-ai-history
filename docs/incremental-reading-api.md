# local_id 增量读取 API

日期：2026-11-02（Asia/Tokyo）。适用后端：Windows 的 `sqlcipher_native` 在线只读后端。保留原有七个 MCP 工具和查询 API，新增工具后共八个。离线 `sqlite3` 镜像和 macOS SQLCipher CLI 后端暂不支持本增量 API，会明确报错。

## 前提与可靠性边界

这个 API 查询当前数据库中 **local_id 大于已确认高水位的新增行**。每个数据库、每张消息表的 ID 空间独立，不能用时间戳或 server_id 替代。

Phase 1 确认消息表的 `local_id` 是 `INTEGER PRIMARY KEY`，这证明单表唯一性，**不证明微信在所有重建、删除和回填场景中始终追加更大的 ID**。没有 AUTOINCREMENT 或生产者协议就无法由读者保证永不复用 ID。因此：

- 只对每张表新增行持续使用更大正整数 local_id 的情况提供无跳过分页；接口校验单一 INTEGER 主键和正 ID，不支持重复 ID 的自定义表。
- 不捕获旧行的更新、删除，或事后插入到高水位以下的记录。它不是完整变更日志或事件审计服务。
- 可检测 salt 变化、已知分片/表消失，以及当前最大 ID 低于已确认高水位，遇到这些情况报错，不默默重置。
- 若库/表用相同 salt 重建、ID 已追平高水位，或者一次查询间隔内删除后又复用 ID，单靠 salt + local_id 可能无法检测。要覆盖这些场景，需要额外的可靠来源代次或生产者变更日志；本实现不声称已解决，也不会改用时间戳冒充解决。

## Python API 与初始化

```python
# 入口模块位于 scripts/common/query.py
from query import read_new_messages

baseline = read_new_messages(cursor=None, limit=200)
assert baseline['messages'] == []
cursor = baseline['next_cursor']
```

初始化扫描本次发现的 `message_N.db` 和 `Msg_*` 表，读取每张表的最大 local_id（空表为 0），不读取/格式化历史正文。返回 `count=0`、`has_more=False` 和初始 Cursor。

每个分片在一个短只读事务内读取 schema、ID 边界和数据。分片之间没有共同原子快照：在已扫描分片的新提交由下次调用读取，初始化期间尚未扫描分片的新提交可能被纳入该分片的基线。初始化建立的是逐分片基线，不是一个全局墙钟时刻。

## 增量查询与分页

```python
batch = read_new_messages(cursor=cursor, limit=200)
process_successfully(batch['messages'])
# 只有处理整批成功后，调用方才持久化 next_cursor。
save_in_callers_own_storage(batch['next_cursor'])
cursor = batch['next_cursor']
# has_more=True 时立即继续；False 时由调用方决定何时再调用。
```

`limit` 必须是 1～2000 的整数，默认 200。每张表先读取 `local_id > watermark ORDER BY local_id ASC LIMIT limit+1`，然后按 `(local_id, source_db, source_table)` 选出全局最多 limit 条。每表只会选取其待读记录的连续前缀；选中后才按 `(create_time, source_db, source_table, local_id)` 升序排列本批输出。

每张表的水位只推进到本次实际返回的最大 ID。预读但没有返回的行不推进水位。跨表 local_id 仅用于确定可重复的选择次序，不代表消息的全局时间次序。

`has_more` 表示本次分片快照中存在超出 limit 的候选，不承诺返回后没有新的提交。不同分页之间不保证全局时间顺序；时间戳回退、同秒多条消息不影响 ID 游标。持续写入时不提供跨会话的公平性或延迟上界。

新发现的表和分片从水位 0 开始，读取其中已存在的正 ID 行；未选中时保留 0，避免丢掉新会话的首批消息。

返回格式：

```text
{
  messages: [
    { ...原 _fmt_msg 字段,
      contact: 联系人显示名称,
      source_db: "message/message_0.db",
      source_table: "Msg_<会话哈希>",
      local_id: 整数,
      server_id: 整数或 null }
  ],
  count: 本批条数,
  has_more: 布尔值,
  next_cursor: "不带填充的 Base64 URL-safe JSON"
}
```

复用原正文/zstd 解码、发送方向识别、联系人名称及富媒体/系统事件格式化；同分片的 Name2Id 用于发送者映射。`server_id=0/null` 原样保留，不参与推进。消息定位应结合账号、数据库 generation、source_db、source_table、local_id 使用；跨账号或库重建后不能只凭 local_id 去重。

## Cursor 格式与恢复

Cursor v1 的 JSON 包含 `version`、`account`、`databases`、`checksum`。账号指纹是账号目录名的带域 SHA-256，分片 generation 是数据库 salt 的带域 SHA-256。数据库索引使用 `message/message_N.db` 相对路径，每个分片保存 `generation` 和 `tables: {表名: high_water_mark}`。

没有 raw key、派生密钥、正文、账号明文、原始 salt 或绝对路径。Cursor 是可解码的元数据，不是加密或授权令牌；SHA-256 校验和检测意外损坏，**不提供防伪签名**。应把 Cursor 当作不透明字符串保存，不自行编辑水位。格式严格校验，拒绝重复字段、未知字段、异常路径、非法水位、非规范编码和不兼容版本；当前最大编码长度 4 MiB。

进程退出后，将调用方保存的字符串原样传回即可继续，不依赖旧连接或内存缓存。账号目录标识和数据库 salt 不变时，移动数据根目录不影响 Cursor；换账号目录或库 generation 会明确报错。

格式/身份/代次/回退错误抛出 `incremental_cursor.CursorError`（ValueError 子类），源不可用或查询错误也会失败，没有部分成功返回或自动持久化。重试短暂失败时使用最后已成功处理的 Cursor。恢复时不能悄悄省略 Cursor：显式重新初始化会跳过当时全部现存历史。需要保留缺口时，应先由调用方对离线备份/历史 API 对账，再决定是否建立新基线。

交付语义为 **至少一次**：处理成功但还没有持久化 Cursor 就崩溃，重试可能返回同一消息；若源仍保留这些行，原 Cursor 可重放。调用方负责幂等处理和持久化，不承诺跨进程恰好一次。已被源删除且 WAL 已回收的数据无法由该 API 恢复。

## CLI 与 MCP

```powershell
python scripts/common/query.py changes --json
python scripts/common/query.py changes --cursor '<上一批 next_cursor>' --limit 200 --json
```

不带 `--json` 也返回可读 JSON。CLI 不写游标文件；Cursor 较大超过操作系统命令行长度限制时使用 Python 或 MCP 接口。

MCP 工具：`wechat_read_new_messages(cursor=None, limit=200)`，返回上述结构化对象。原有七个工具的参数与文本输出保持兼容。

## 资源与测试范围

没有后台线程、轮询、队列、同步服务或常驻连接。每次调用重新发现分片和表，候选缓存按 limit 有界；成本随表数和每表待读量增长。只读连接保持原有 `mode=ro/query_only` 约束，不执行 Checkpoint、不修改全量解密。SQLite 正常读 WAL 仍可能参与 SHM 协调。

`tests/test_incremental_messages.py` 全部使用人工 SQLCipher 数据和临时目录，覆盖初始化、乱序/同秒、多表/多位分片、分页/重放、新源、NULL/0 server_id、格式及代次错误、回退、格式化异常、读连接清理、源字节不变、解码复用、CLI 进程重启和 MCP 实际调用。现有 `e2e/test_e2e.py` 也在合成账号下运行，子进程通过临时 sitecustomize 指向合成密钥/数据目录，不查询真实聊天记录。真实微信对 local_id 单调性的长时观察、实际源重建及延迟测量不在本次合成验证中。

## 本次验证结果

执行环境：Windows 11 x64、Python 3.12.4、sqlcipher3 0.6.4。分支 `feat/incremental-sync`，开始时工作区干净。

| 检查 | 结果 |
|---|---|
| 新增 `tests/test_incremental_messages.py` | **36 passed in 47.76s** |
| 全部 `python -m pytest -q` | **330 passed in 85.52s**，无失败或跳过 |
| 原 `e2e/test_e2e.py` | 合成账号上 **13/13 PASS**，由新增回归测试启动原脚本及其 CLI 子进程 |
| CLI `changes --help` | 包含 `--cursor`、`--limit`、`--json`；合成库上的初始化/恢复由独立进程验证 |
| MCP 注册及调用 | 8 个工具，原七个名称不变；新增工具实际调用成功并返回消息定位字段 |
| 源只读、句柄清理 | 合成主库/WAL 查询前后字节一致；SQL trace 无 Checkpoint；查询返回后所有追踪连接已关闭 |
| 语法编译、`git diff --check` | 通过 |
| 变更内容检查 | 仅代码、合成测试、文档与 CI 配置，无本地密钥或数据库产物 |

改动文件：`incremental_cursor.py`、`scripts/common/query.py`、`server.py`、`tests/test_incremental_messages.py`、`e2e/test_e2e.py`、`.github/workflows/tests.yml`、本文及 `docs/README.md`。原全量解密流程及数据库直读后端未修改。本次没有自动提交或推送。

# Phase 2.2：SQLCipher 只读后端

日期：2026-11-02（Asia/Tokyo）。开发分支：`feat/incremental-sync`，开始时工作区干净。

## 使用方式

Windows 默认后端改为 `sqlcipher_native`，安装固定的 `sqlcipher3==0.6.4`：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-windows.txt
$env:WECHAT_DATA_ROOT = '<xwechat_files 根目录>'
# 可选：多个账号时明确选择账号目录名或 wxid
$env:WECHAT_ACCOUNT = '<账号>'
.\.venv\Scripts\python.exe scripts/common/query.py list --json
```

密钥读取现有 `key_windows.txt`，不通过命令行传递。首次读取按账号密钥与每库 salt 派生；最多缓存 128 个派生结果，只在进程内保存，不调用原有磁盘派生缓存。

全量解密入口 `scripts/windows/decrypt_all.py` 保持原样。需要查询旧的明文镜像时显式选择：

```powershell
$env:WECHAT_DB_BACKEND = 'sqlite3'
.\.venv\Scripts\python.exe scripts/common/query.py list --json
# 返回在线查询
$env:WECHAT_DB_BACKEND = 'sqlcipher_native'
```

离线模式不保证最新。在线失败不会自动退回旧镜像或按目录时间猜测账号；不存在或无法唯一验证的账号明确报错。macOS 原 SQLCipher CLI 后端保持原样。

## 实现

- `native_reader.py`：懒加载驱动、验证 codec、SQLCipher v4/raw key 初始化、`mode=ro`、`query_only=ON`、`temp_store=MEMORY`，HMAC 保持默认开启。
- `query/query_raw/iter_query` 保持参数和返回结构，每次调用打开独立只读连接，结束或异常时关闭。流式查询使用单游标与批量 fetch；调用方提前停止时应关闭生成器，搜索入口已显式清理。
- 锁等待由引擎提供，单次最长约 1 秒 busy timeout；超时明确抛出驱动异常。没有在输出部分行后重试，避免重复或混合快照；也没有额外的自动重试/恢复循环。
- 每次消息库发现重新扫描 `message_N.db`，支持多位数字分片、新增与删除；验证所有已发现分片，某分片损坏时不返回部分成功。Name2Id 和 schema 沿用现有实时 SQL。
- 查询入口复用现有跨分片合并、排序、全局 limit、发送者映射及正文解码；搜索的压缩正文回退也使用原生流式游标。
- 联系人只在单次查询请求内缓存，使用 ContextVar 隔离并发请求；请求结束即丢弃，下次重新读取。直接调用联系人解析也会重新读取。没有常驻源连接，从而避免 Windows 文件句柄阻止源文件替换；合成测试覆盖该行为。
- 原始主库/WAL 不执行写入、Checkpoint 或 journal mode 修改；正常 WAL 读取仍可能创建/更新 SHM 协调文件。没有使用 `immutable` 或 `nolock`。

每条 SELECT/流式游标具有单库快照；已开始的游标不会混入之后提交的数据，下一次查询建立新快照。多个分片及一次业务请求内的多条 SQL **不构成跨库/跨语句的原子快照**。联系人在请求中首次读取后固定到请求结束。

## 自动化覆盖

`tests/test_native_reader.py` 使用真正 SQLCipher 引擎和人工生成的加密库，Windows 缺依赖会失败；非 Windows 环境没有可用绑定时跳过这组测试。

1. 多页事务、未提交不可见、提交后主库字节未变但 WAL 新行可见、回滚。
2. 活跃流式游标快照稳定，下一次查询可见更新；BLOB/NULL/原始输出兼容。
3. PASSIVE / RESTART / TRUNCATE、等长 WAL reset 且 salts 改变。
4. 提前关闭释放读锁、Checkpoint 恢复、并行线程查询、SQL 错误后恢复。
5. 独立写者进程连续 10 次多页事务，逐次验证提交前后完整字段；进程退出后重读。
6. 写者关闭/重开、读连接重建、同路径不同 salt 的库替换。
7. 缺失/截断/损坏、错误密钥、锁超时、缺失驱动/codec、禁止写入。
8. 新增/删除多位数字分片、账号歧义与显式选择、已选账号不可用时不切换。
9. 现有 list/read/search/recent/summary 的合并/排序/limit，联系人提交刷新、压缩正文与最新消息可见，坏分片明确失败。
10. 联系人请求缓存、文件替换不被句柄阻止、异常退出请求后缓存释放。
11. 同一合成主库/WAL 与保留的 `decrypt_all.decrypt_db()` 全量解密结果逐字段一致，明文输出 integrity_check 为 ok。

## 边界

本阶段只交付直读后端与查询适配，没有新增后台轮询、持久化同步进度、消息消费游标、事件订阅或明文持续镜像。进程重启直接重新发现并验证当前源，不需要恢复物理 WAL offset。

`temp_store=MEMORY` 避免排序临时明文落盘，但大搜索仍有内存峰值风险，未完成压力测试。当前依赖的实际执行环境是 Windows x64 / Python 3.12.4；其他 Python/平台组合需由 CI 验证，不能由本机结果推定通过。

真实微信连续新增消息、自然 Checkpoint/Reset、退出重登、休眠与耐久测试，以及提交到可见的延迟统计留待 Phase 3。本次真实环境只做已有数据的只读冒烟，不主动发送消息或修改源。

## 本次执行结果

环境：Windows 11 x64、Python 3.12.4、sqlcipher3 0.6.4（复用 Phase 2.1 固定 wheel，安装到现有 `.venv`）。

| 检查 | 实际结果 |
|---|---|
| 新增后端测试 | 12 passed，14.91 秒；随后包含在全量回归中再次通过 |
| 全量 `python -m pytest -q` | **294 passed in 48.29s**，无失败、无跳过 |
| 真实环境 `e2e/test_e2e.py` | **13/13 PASS**，日志仅保留于忽略的 `.local/phase2_2/e2e.log` |
| 正式后端真实冒烟 | 成功直读 4 个消息分片；列出 1002 个会话，两次结果相同 |
| 正式后端列会话单次计时 | 首次 1.629 秒，再次 0.070 秒；系统/派生缓存可能已预热，不是冷磁盘或提交可见延迟基准 |
| `python -m pip check` | No broken requirements found |
| 语法编译、`git diff --check` | 通过 |
| MCP 加载与工具枚举 | 7 个工具；不等同于外部 MCP 客户端连接验收 |

首轮全量回归发现新增合成账号名不符合仓库的夹具命名白名单，已改为 `wxid_test...` 命名并通过最终回归。最初的常驻联系人观察连接在 Windows 替换测试中暴露文件占用，最终实现已移除该连接，改为请求内快照；替换和清理测试通过。没有通过放宽原有校验或跳过 Windows 测试掩盖问题。

全量解密模块及 WAL 快照模块无修改。Git 变更仅包含代码、合成测试、依赖与脱敏说明，未提交数据库、密钥或真实聊天正文；未创建提交或上游 PR。

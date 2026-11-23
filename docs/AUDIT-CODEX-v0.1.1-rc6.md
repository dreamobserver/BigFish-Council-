# BigFish Council v0.1.1-rc6 审计报告

审计对象为 [v0.1.1-rc6 Release](https://github.com/dreamobserver/BigFish-Council-/releases/tag/v0.1.1-rc6)，审计日期为 2026-11-23。

本报告基于 GitHub tag 的源码、发布元数据和仓库内交付/安全文档进行静态审计。没有在本机重新安装依赖、运行 265 项测试、运行真实 Qt 窗口或重建 exe。仓库文档中的测试结果按作者自述记录，并与源码声明逐项对照。

## 一、结论

### 1. 发布判断

结论为 **可以继续开发和做隔离验收，不建议直接作为生产版发布**。

仓库自身把 rc6 定义为 release candidate，并明确列出两个未完成门槛。

- Windows 真实窗口和恢复流程的人眼验收没有完成。
- 跨设备 MCP 闭环没有在 rc6 上重跑。

源码层面还存在一个 Windows 恢复互斥的竞态。这个问题比“测试是否全绿”更基础，因为它影响数据库恢复时的数据一致性。

### 2. 桌面端判断

**已有桌面端代码和 exe 打包入口，尚未形成可交付 exe。**

已存在以下内容。

- PySide6 GUI，GUI 内嵌 API 线程和看门狗。
- build.ps1，使用 PyInstaller onedir 生成 dist\BigFishCouncil\BigFishCouncil.exe。
- Windows 启动脚本和计划任务脚本。

rc6 Release 的资产是源码 zip、patch、handoff 和审计文本，没有 BigFishCouncil.exe 或安装包。build.ps1 在 rc6 交付说明中也标为“未执行”。因此当前状态是“具备打包路径”，不是“已交付桌面安装包”。

### 3. 安卓端判断

**有接口和端口预留，有设计草案，没有 APK 初步实现。**

已存在以下预留。

- PORT_RESERVED 中登记 3099、3100、3101。
- docs/ARCHITECTURE-FUTURE.md 中定义配对码、REST、SSE、版本协商和 Android 技术选型。
- 文档明确写出 mobile-android 目录不存在，移动端端点全部未实现。

当前仓库没有 Android Gradle 工程、Kotlin/Flutter 源码、移动端路由、配对码鉴权或 SSE 实现。应判定为“接口设计预留完成，初步开发未开始”。

## 二、问题总表

| 编号 | 等级 | 类型 | 判断 |
|---|---|---|---|
| BFC-01 | 高 | 并发/数据一致性 | Windows 恢复流程与新写者之间存在 TOCTOU 竞态 |
| BFC-02 | 高 | 资源耗尽 | MCP 自由文本没有字段级 maxLength |
| BFC-03 | 高 | 安全边界 | create_app() 可被第三方绕过官方入口后绑定公网 |
| BFC-04 | 中 | 安全语义 | 审计 manifest 仍写 contains_plaintext_credentials=false，与 pattern-based 扫描不一致 |
| BFC-05 | 中 | 生命周期 | CLI 的非 restore 分支不显式关闭数据库，嵌入式调用可能遗留连接和租约 |
| BFC-06 | 中 | 文档/实现 | allowed-ports 只打印已用端口，没有打印文档承诺的预留端口 |
| BFC-07 | 中 | 发布工程 | 依赖未锁定、无可见 CI、无 exe/apk 产物和签名验证流程 |
| BFC-08 | 中 | 版本维护 | tag 指向无父提交且未签名的根提交，历史可追溯性较弱 |

## 三、详细问题与修改片段

### BFC-01 Windows 恢复锁存在 TOCTOU 竞态

#### 证据

Windows 写者租约使用每进程文件，例如 council.db.access.<pid>.lock。恢复时先遍历这些文件并尝试锁定，随后再锁定另一把 council.db.access.lock。这两把锁不是同一把 Windows 内核锁。写者没有在恢复锁上登记共享状态。

相关源码位置。

- [bigfish/locks.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/locks.py#L38-L157)
- [bigfish/db.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/db.py#L238-L274)
- [bigfish/backup.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/backup.py#L475-L505)

核心竞态可抽象为：

~~~
alive = probe_all_pid_lock_files(db_path)
if alive:
    raise LeaseError("还有写者")
with exclusive_access_file(db_path):
    replace_database()
~~~

在 probe_all_pid_lock_files 返回后，新的 Database 仍可以创建自己的 PID 锁文件。恢复端随后继续替换数据库。

#### 影响

- 旧写者已被扫描为空时，新写者可能在替换前打开数据库。
- Windows 上可能表现为替换失败，也可能表现为恢复后新旧连接分别写入不同文件状态。
- 当前单元测试覆盖了扫描时已有写者和没有写者的静态场景，没有覆盖扫描与替换之间启动新写者的竞争窗口。

#### 修改方向

让写者和恢复者使用同一个操作系统级协调对象。优先方案是使用同一锁文件的 Windows LockFileEx。

- 普通 Database 持有共享锁。
- restore 持有排他锁。
- 所有写者都必须先取得共享锁，再打开 SQLite。
- restore 取得排他锁后再进入维护模式和替换文件。

~~~
class AccessLease:
    def acquire_shared(self) -> None:
        ...

    def acquire_exclusive(self) -> None:
        ...

    def release(self) -> None:
        ...

self._lease = AccessLease(self.path)
self._lease.acquire_shared()
self._conn = sqlite3.connect(self.path)

with AccessLease(target).exclusive():
    enter_maintenance()
    replace_database()
~~~

如果继续使用 msvcrt，至少要增加一个恢复闸门协议。写者打开前先检查并持有恢复闸门，恢复端拿闸门后再次确认写者集合，并阻止新写者进入。单纯先扫描 PID 文件，再锁另一文件不能解决竞态。

应新增一个真实多进程 Windows 测试。

1. 进程 A 在恢复扫描和替换之间暂停。
2. 进程 B 尝试打开 Database。
3. 断言 B 被阻止或恢复端被阻止。
4. 断言恢复后的数据库只有一个可继续写入的连接世代。

### BFC-02 MCP 自由文本没有字段级长度限制

#### 证据

REST 的 Pydantic 模型为 log、diff、tests、note 和 detail 设置了 max_length。MCP 的 tool_specs() 只声明这些字段为字符串，validate_args 只校验必填、未知字段、类型和枚举。

相关源码位置。

- [bigfish/server.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/server.py#L38-L75)
- [bigfish/server.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/server.py#L356-L385)
- [bigfish/server.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/server.py#L416-L465)

最小修复片段。

~~~
def validate_args(spec: dict, args: dict) -> str | None:
    for name, value in args.items():
        prop = props.get(name) or {}
        if prop.get("type") == "string":
            limit = prop.get("maxLength")
            if limit is not None and len(value) > limit:
                return f"参数 {name} 超过长度上限 {limit}"
~~~

并在 MCP schema 中补齐。

~~~
"log": {"type": "string", "maxLength": MAX_TEXT_FIELD},
"diff": {"type": "string", "maxLength": MAX_TEXT_FIELD},
"tests": {"type": "string", "maxLength": MAX_TEXT_FIELD},
"note": {"type": "string", "maxLength": MAX_TEXT_FIELD},
"detail": {"type": "string", "maxLength": MAX_TEXT_FIELD},
~~~

当前 MCP 请求仍受 8 MiB 总体限制，但一个字段可以独占接近整个请求体，并在 JSON 解析和业务处理前进入内存。

### BFC-03 create_app() 仍可绕过监听地址闸门

#### 证据

官方 serve() 会调用 check_bind_host()，但 create_app() 返回未绑定的 ASGI app。第三方可以自行调用 Uvicorn。

~~~
app = create_app(council)
uvicorn.run(app, host="0.0.0.0", port=3096)
~~~

仓库文档已经承认这条绕过路径。

- [bigfish/server.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/server.py#L161-L171)
- [bigfish/server.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/server.py#L535-L552)
- [SECURITY-REVIEW-v0.1.1-rc6.md](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/docs/SECURITY-REVIEW-v0.1.1-rc6.md#L160-L185)

#### 修改方向

保留测试需要的无绑定工厂，同时增加生产工厂并强制校验。

~~~
def create_production_app(council: Council, host: str,
                          watchdog_interval: float | None = None) -> FastAPI:
    check_bind_host(host)
    return _build_app(council, watchdog_interval)
~~~

同时把 serve()、GUI 和 CLI 全部切换到 create_production_app()。文档仍应保留 create_app() 的测试用途说明。

### BFC-04 审计 manifest 的布尔声明与扫描能力不一致

#### 证据

审计包明确说明扫描是 pattern-based，并要求人工复核，同时仍写入：

~~~
"contains_plaintext_credentials": False
~~~

相关位置。

- [bigfish/backup.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/backup.py#L190-L229)
- [bigfish/security.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/security.py#L32-L108)

下游脚本若只读取这个布尔值，会把“未命中 11 类已知模式”误读为“证明没有明文凭据”。换行拆分、Base64、私有格式和新型 token 仍可能漏检。

#### 修改方向

删除保证性布尔值，改为枚举状态。

~~~
"credential_status": "pattern_scan_only",
"manual_review_required": True,
"credential_scan": {
    "mode": "pattern-based",
    "patterns": list(SCRUB_KINDS),
    "hits": hits,
}
~~~

若必须兼容旧字段，应把旧字段改为 None，并增加 schema 版本，让旧消费者不能静默当作安全证明。

### BFC-05 CLI 非 restore 分支不显式关闭数据库

#### 证据

cli.py 只在 restore 分支的 finally 中关闭 db。其他分支依赖进程退出释放连接。

- [bigfish/cli.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/cli.py#L124-L142)
- [bigfish/cli.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/cli.py#L215-L232)

作为独立进程运行时通常能释放资源，但被测试、计划任务包装器或其他 Python 进程调用 main() 时，数据库连接和租约会持续到垃圾回收或进程结束。Windows 文件占用会放大这个问题。

#### 修改方向

把命令分支包进统一的 try/finally。

~~~
db = None
try:
    settings, db, council = open_for_command(...)
    return dispatch_command(settings, db, council, args)
finally:
    if db is not None:
        db.close()
~~~

### BFC-06 allowed-ports 没有显示预留端口

#### 证据

配置定义了 PORT_RESERVED，其中包含移动端 3099、3100、3101，但 CLI 的 allowed-ports 只导出 PORT_ROLES。

- [bigfish/config.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/config.py#L22-L48)
- [bigfish/cli.py](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/bigfish/cli.py#L155-L171)
- [ARCHITECTURE-FUTURE.md](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/docs/ARCHITECTURE-FUTURE.md#L786-L796)

文档阶段验收要求输出 3 个已用端口加 3 个预留端口，但当前实现只输出已用端口。

#### 修改片段

~~~
from .config import PORT_RESERVED, PORT_ROLES

dump({
    "active": {
        k: {"port": v[0], "role": v[1]}
        for k, v in PORT_ROLES.items()
    },
    "reserved": {
        str(port): {"role": role, "description": desc}
        for port, (role, desc) in PORT_RESERVED.items()
        if port in (3099, 3100, 3101)
    },
})
~~~

同时补一个断言，确保预留端口不会和已用端口重复。

## 四、结构性缺陷

### 1. 发布工程不闭环

requirements.txt 只使用版本范围，没有 lockfile 或 hash。build.ps1 每次从网络重新解析 PySide6、FastAPI、Uvicorn 和 MCP 依赖。当前没有看到 .github/workflows、构建产物校验或安装包签名流程。

建议加入 lockfile、固定 Windows runner、CI artifact、exe SHA256、代码签名状态和构建日志。Release 只从成功 CI artifact 发布 exe。

### 2. 数据层和 API 层耦合较重

core.py 同时承载状态机、事务、授权、看门狗和审计写入。server.py 直接把同步 Council 调用放入线程池。移动端未来还要增加配对码、SSE、版本协商和人工可读数据接口。

建议拆为四层。

~~~
Transport
  REST / MCP / Mobile SSE
        |
Application Service
  claim / heartbeat / submit / review
        |
Domain
  task state machine / ticket / approval
        |
Persistence
  SQLite / migration / backup / lease
~~~

迁移顺序建议如下。

1. 先抽 ClientTransport 和 HttpTransport，保持现有 ExecutorClient 兼容导出。
2. 再抽 ApplicationService，让 REST 和 MCP 共用参数校验、权限和错误映射。
3. 最后增加移动端路由和 SSE，不直接把移动端逻辑塞入 server.py。
4. 所有移动端路由由 mobile_enabled=False 默认关闭。

### 3. 移动端权限模型尚未落地

设计文档提到 role=mobile，但当前 roles.py 没有移动端角色，现有 HTTP 面只有执行器权限。不能把手机端直接复用 EXECUTOR_DSH，否则手机 token 可能获得领取任务和提交结果权限。

建议新增最小角色。

~~~
MOBILE_VIEWER = "mobile_viewer"

ROLE_PERMISSIONS[MOBILE_VIEWER] = {
    "task.read",
    "event.read",
    "review.create",
}
~~~

服务端根据配对码产生短期设备 token，token 只返回一次。approve 默认关闭，若未来开启，必须在服务端叠加短时 PIN 或设备级二次确认。

## 五、exe 与 APK 的实现路线

### 阶段 0

- 修复 BFC-01、BFC-02、BFC-03、BFC-04。
- 保持 3099、3100、3101 只作为配置预留。
- 解决 3099 与自检端口的所有文档和脚本冲突。
- 为 allowed-ports、移动端路由关闭状态和版本字段补测试。

### 阶段 1

- 完成 bigfish/client/transport.py。
- 将现有 ExecutorClient 迁移为 HttpTransport，保留兼容导出。
- GUI 和测试使用 InProcessTransport。
- 让 HTTP、MCP 共用一套应用服务和错误语义。

### 阶段 2

- 在 Windows runner 上执行 build.ps1。
- 运行 GUI 真窗口验收。
- 运行 exe 首次启动、升级、恢复、计划任务和卸载流程。
- 生成 exe、压缩包、SHA256 和签名结果。

### 阶段 3

- 新增 bigfish/mobile/auth.py、routes.py、sse.py。
- 以 mobile_enabled=False 默认关闭。
- 配对码 300 秒过期，失败次数限制和设备吊销必须在服务端实现。
- SSE 需要 since 游标和断线续传，不允许每次从 0 重放历史。

### 阶段 4

- 新增 mobile-android/ Kotlin 工程。
- 第一版只做配对、总览、任务列表、任务详情和事件流。
- 不在 APK 保存长期管理员凭据。
- 锁屏断线后先拉任务快照，再按游标补增量。
- 旧 APK 连接新服务端时返回明确的 426 能力版本错误。

## 六、建议验收门槛

| 门槛 | 通过标准 |
|---|---|
| Windows 恢复并发 | 多进程竞争时新写者不能进入恢复临界区 |
| MCP 输入 | 每个自由文本字段超过上限返回 422 或 JSON-RPC 参数错误 |
| 监听边界 | 官方入口和生产 app 工厂都拒绝非回环地址 |
| 审计包 | 不再用布尔值暗示 DLP 完整性，人工复核字段必须可见 |
| exe | Release 有可运行 exe、SHA256、签名状态和构建日志 |
| APK | 仓库有可构建 Android 工程，至少完成配对和只读总览 |
| 跨设备 | 在 rc6 代码上重新完成 claim → heartbeat → submit 闭环 |
| GUI | 真窗口完成恢复、托盘、DPI、焦点和停服失败路径验收 |

## 七、审计依据

- [Release v0.1.1-rc6](https://github.com/dreamobserver/BigFish-Council-/releases/tag/v0.1.1-rc6)
- [README.md at v0.1.1-rc6](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/README.md)
- [DELIVERY-v0.1.1-rc6.md](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/docs/DELIVERY-v0.1.1-rc6.md)
- [SECURITY-REVIEW-v0.1.1-rc6.md](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/docs/SECURITY-REVIEW-v0.1.1-rc6.md)
- [ARCHITECTURE-FUTURE.md](https://github.com/dreamobserver/BigFish-Council-/blob/v0.1.1-rc6/docs/ARCHITECTURE-FUTURE.md)
- [Release tag commit](https://github.com/dreamobserver/BigFish-Council-/commit/a2e9275751ffdc9e03a25df163d31275a340fb7)



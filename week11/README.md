# V10 MBSE CLI 与 MCP

V10 将 AI 草稿、本地规格检查、人工确认、SysML/Modelica 生成与验证、版本和证据接在同一个 CLI 中。新增代码在 `week11`；通过结构化参数适配复用 `week9`、`week10`，不修改旧源码、模板和检查阈值。

首个 CLI 建模、查询、连续两次修改和重验闭环已完成真实机制测试。本机保存的测试记录为62项通过、零跳过；正式公开在线批次20/24未达22/24门槛。独立补充批次保留两次传输失败，续跑后按固定分母为9/12，未达11/12门槛。用户已实际完成RC首次建模、两次电压修改（5→6→1 V）、版本比较和电容原始声明查询；真人RLC、双房间和截止频率修改验收尚未完成，不宣称所有验收均通过。

本目录按前版惯例发布源码、配置、测试和使用说明；总结、工作日志、运行结果、项目数据、密钥和虚拟环境只留在本地。发布不包含小车建模或独立聊天界面。

## 本机运行

在 PowerShell 中进入 `D:\mbse`：

```powershell
week11/.venv/Scripts/python.exe -B -m week11.src.cli
```

新项目与独立会话可使用：

```powershell
week11/.venv/Scripts/python.exe -B -m week11.src.cli --db week11/data/my-projects.sqlite --session my-session
```

退出后用相同命令重开，显示该会话所选项目、当前有效版本、阅读版本、待确认方案及最近任务。`/proposals` 或 `/status` 可再次查找方案；列表只包含此会话在当前项目仍有效的方案，查看不会批准或执行。数据库路径必须位于 `week11`。CLI 通过真正的 stdio MCP 子进程调用业务工具。

```text
/new RC试用
设计一个 RC 低通滤波器，截止频率 1kHz，电阻 1kΩ，电容根据截止频率计算，输入电压 5V 阶跃信号
```

先核对显示的原文来源、规范参数、派生公式、刺激条件和假设，再按提示确认具体方案。确认入口只在本地 CLI，未列入 MCP 工具。模型返回 `approved: true` 不会获得执行权限。

查询使用 `/evidence 这个电容来自哪里，请展示原始声明`；`/history` 列出版本，`/compare v000001 v000002` 比较两个已保存版本。自然语言修改最低范围是 RC 输入电压，以及固定电阻和阶跃输入下的截止频率与派生电容。每次修改都展示基线和差异，并单独确认。修改方案还展示经过工件摘要和规格一致性校验的两份原始声明、显式关联检查及需重跑检查；这些基线证据参与确认摘要，不能替代修改后的真实重验。

`/help` 显示完整命令。`--debug-offline` 关闭运行模型，`/source`、`/proposal` 是人工构造规格的调试入口；这种模式不能作为在线理解能力证明。

## 依赖与运行模型

当前本机环境为 Python 3.13.3、OpenModelica 1.25.7、Modelica标准库4.0.0、sysmlpy 0.34.1、ANTLR runtime 4.13.2。MCP 使用 v1 维护线 1.30.0。记录见 [dependency_snapshot.json](configs/dependency_snapshot.json)，固定 Python 包见 [requirements.txt](requirements.txt)。

Windows 任务取消另使用 psutil 7.2.2 核对并清理当前服务实际拥有的父子进程。执行无法证明已清理时会保留 `uncertain` 并阻止新任务；不会把断线当作成功或自动重跑。

### 中断恢复

重开时先用 `/status` 查看最后任务，`/recovery` 检查整个数据库的执行占用；其他项目的阻塞也会显示项目与任务 ID。使用 `/open 项目ID` 后再 `/task 任务ID` 查看详情。

- `queued`、`running`、`cancel_requested`：如果原服务仍在工作，等待其结果或在所属项目请求 `/cancel 任务ID`。返回状态才是清理结论；不要按进程名称批量终止 Python 或 OpenModelica。
- `failed`、`cancelled`、`interrupted`：保留日志与最近有效版本。修正原因后重新描述需求、核对新方案并确认；重复旧执行请求只返回旧任务，不作为重试。
- `uncertain`：停止本数据库的新执行，保存数据库和对应 `week11/projects/<项目ID>/task-logs/<任务ID>.log` 日志供诊断。主 PID 不存在不能证明后代已退出。新任务在启动前保存本机身份摘要和 Windows 启动 GUID；在同一台机器实际重新启动后，可用 `/recover 任务ID` 检查资格，并输入 `RECOVER 任务ID` 将任务标为已中断，保留原诊断、版本与恢复审计。此命令不会重启电脑、终止进程、批准或重跑模型。

旧任务没有启动记录、仍处于同一次系统启动、数据库来自不同机器或无法读取操作系统证据时，恢复会拒绝。不要直接修改 SQLite 状态或换数据库绕过阻塞。没有启动记录的旧任务仍需独立核实完整进程树及清理证据，当前入口不处理这类迁移。

`/recovery` 为本地只读诊断；`/recover` 是本地受限状态恢复，不在 MCP 工具列表中，不授予模型进程控制或批准权限。启动周期检测使用 Windows 原生 BootIdentifier，不以可能受系统校时影响的启动时间或可复用的 PID 判断。

本机身份读取已实际通过，重启前后状态迁移以模拟启动标识测试覆盖，尚未实际重启本机验收。复制/克隆操作系统或虚拟机快照恢复不在该证明范围内。

运行需要保留同一仓库的 `week9`、`week10` 代码与配置，不能只复制 `week11`。OpenModelica须能通过 `PATH` 或 `OPENMODELICAHOME` 找到，并能加载上述Modelica标准库。

与旧版相同，本次是源码发布，不是可直接部署的独立安装包。真实生成还依赖本地 `week9/configs/template_certifications.json` 及其认证所需资料；该文件未包含在已有Git提交中。本目录仅记录它的摘要，不复制或修改旧认证。只有克隆源码、没有这些认证资料时，技术检查会拒绝发布模型，不能通过跳过检查解决。

已有 `.venv` 使用 `--system-site-packages` 继承本机已安装的解析器等依赖；这不是另一台机器的干净安装证明。缺失环境可按以下方式建立独立环境，再确保 OpenModelica 与旧版认证依赖可用：

```powershell
python -B -m venv week11/.venv
week11/.venv/Scripts/python.exe -B -m pip install -r week11/requirements.txt
```

运行模型是 DeepSeek `deepseek-flash`。程序按需读取环境变量 `DEEPSEEK_API_KEY`，未设置时读取仓库 `.env` 中该键；不把密钥写入日志或模型消息。工具发现不需要密钥；自然语言模式需要。

[runtime.json](configs/runtime.json) 固定官方 HTTPS 接口、非思考模式、温度 0、120 秒请求超时、每次最多 4096 输出 token、零自动重试。开发预算为最多 52 次请求、1,100,000 token、USD 1.00，共用 `week11/results/runtime-ledger.sqlite`。发送前持久预留，未知用量保留预留，连续两次传输错误或身份/用量异常会冻结。费用按保守峰时费率计算，是本地估算而非账单。

验收用固定公开输入和门槛在 [online_acceptance.json](configs/online_acceptance.json)。不要删除账本、换路径或提高限额来绕过一次开发批次的停止规则。

## 源码与工件

| 文件 | 职责 |
|---|---|
| `src/spec.py` | 规范规格、数值单位和来源校验、明确公式派生 |
| `src/generation.py` | 将已检查规格交给旧结构化生成入口，保存 V10 绑定 |
| `src/store.py` / `src/service.py` | 项目、确认、任务、不可变版本和证据查询 |
| `src/worker.py` | 真实生成子进程、进度、超时、取消和版本发布 |
| `src/mcp_server.py` / `src/cli.py` | MCP 工具与终端对话、可信本地确认 |
| `src/runtime.py` | 在线请求、身份检查、持久预算和重复请求保护 |

项目产物保存在 `week11/projects/<project_id>/versions/<version_id>`。每个已发布版本含规格、确认记录、原文、两份模型、RunResult、生成绑定、追溯证据和文件摘要。技术验证与证据索引分别记录，全部就绪才更新当前有效版本。失败任务和日志保留作诊断，不替代有效版本。

底层 RunResult 继续使用旧 `v8` schema；V10 身份由外层规格和绑定记录。旧认证不自动证明 V10 新输入适配已获认证。SysML 检查范围是 ANTLR 语法和最小结构，不宣称完整语义验证。

首版实现 RC、RLC、双房间新建，并已分别完成公开案例真实机制测试；修改限上述 RC 两类。反相运放、单房间、未知拓扑、新公式及通用因果推理未开放。证据系统基于保存的模型与显式图关系，没有关系时不能推断“没有影响”。

单独启动 MCP：

```powershell
week11/.venv/Scripts/python.exe -B -m week11.src.mcp_server --db week11/data/projects.sqlite --session default
```

它使用 stdio 协议，由 MCP 客户端连接；不是交互 Shell。工具只接收标识和结构化参数，不提供任意路径读取、Shell 执行或模型自行确认能力。

完整本地测试命令：

```powershell
week11/.venv/Scripts/python.exe -B -m unittest discover -s week11/tests -q
```

62项通过是开发机上包含公开案例仿真记录的历史结果，不是仅克隆源码后的测试承诺。`test_audit_version.py` 依赖本地 `week11/results/wp2-rc-integration-final-20260914`；`test_service.py` 中的模拟worker及部分CLI测试依赖 `week10/results/trace-stage2-20260909/{baseline,changed}/run-result.json`。这些运行产物未上传，缺失时完整测试会失败，不能将缺失记录计为通过。另一台机器完整部署和测试资料准备留待后续交付验证。

`scripts/cli_smoke.py`、`cli_mechanism.py`、`online_acceptance.py` 和 `clarification_regression.py` 是开发验收脚本，部分会使用真实模型API并消费预算；机制/正式批次脚本需自行准备对应公开冒烟结果。脚本确认只用于标记明确的测试项目，不能作为真人确认的证明。绑定历史项目编号或恢复授权的一次性脚本不随源码发布。

测试中的模拟 worker 和脚本输入会明确记录，不代表真实仿真或真人完成了产品确认。

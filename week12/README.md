# V11：有限支持的自动建模 Agent

## 源码发布范围

GitHub仅发布源码、配置、组件模型、测试源码与交付说明；不包含测试或仿真结果、run账本、项目快照、用户原件及本地环境。下文历史证据链接和旧run查询命令仅在保留这些数据的原工作区可用；部分历史复核测试与脚本也依赖本地证据。本次上传是源码归档，未验证新电脑完整安装复现。

更新：2026-10-15。当前交付沿用V10的CLI/MCP、需求来源、确认、版本及工具链，补充V11领域输入、双源码生成、真实执行和证据交付。产品在线模型提出方案、生成及有限纠正模型；Agent承接上下文、关键确认、执行、检查和证据。用户无需写JSON或手工模型。

按[当前结项计划](任务规划/任务规划-V11.md)的有限范围，第1–4项已有证据，第5项说明和记录已收口；主窗已审查，V11按该有限支持范围结项。车辆物理仍为 **UNKNOWN**，执行快照未发布；不证明任意新需求、20级整车、全部碰撞/能量或实车能力。证据和限制见[交付验收总结](V11-交付验收总结.md)，当前入口见[短状态](V11-CURRENT-STATE.md)。

## 在现有环境启动

从 `D:\mbse` 启动，使用已安装的 `week11/.venv`，不因V11目录名另建环境或升级依赖。在线任务需要程序可取得 `DEEPSEEK_API_KEY`（环境变量，或由现有程序按需读取根目录 `.env`）；不把密钥输入Agent对话或写入结果。当前产品配置为 `deepseek-flash`，与开发窗口所用模型分开。

**新任务显式选择 `interactive-20261013`，并换成自己的新run名。** 名称只含字母、数字、下划线和短横线，最长64字符。`robot-task-01` 仅为命令示例，不是已授权或已验收任务。

PowerShell：

~~~powershell
Set-Location D:\mbse
.\week11\.venv\Scripts\python.exe -X utf8 -B -m week12.src.car_domain.cli_adapter --backend model-artifact --run robot-task-01 --runtime-profile interactive-20261013
~~~

Windows CMD：

~~~bat
cd /d D:\mbse
.\week11\.venv\Scripts\python.exe -X utf8 -B -m week12.src.car_domain.cli_adapter --backend model-artifact --run robot-task-01 --runtime-profile interactive-20261013
~~~

只准备新run、显示额度而不激活、不调用模型：在同一命令末尾加 `--prepare`。离线演练使用另一个新run名并加 `--offline`；无在线模型或执行权，同run不能随后切为LIVE。

| 新run配置 | 实际限制 |
|---|---|
| `interactive-20261013` | thinking enabled；最多12 HTTP、累计1000万tokens、按配置计费≤$2；单次输入≤128000、输出含思考≤65536tokens |
| 执行额度 | 每run最多1个实际模型任务；Worker上限300秒、内部仿真上限120秒 |
| HTTP等待 | 读取等待600秒、其余操作60秒；自动传输重试0。读取等待不是完整请求硬墙钟上限 |
| 旧默认 `daily` | 2 HTTP、16000tokens、按配置≤$0.02，单次输出2048tokens，基础配置thinking disabled；不是建议的完整方案/生成任务入口 |

任一额度先到即停，1000万tokens不是保证可消费量，费用会先限制。以上是现有配置上限，不代表本页授权费用、保证模型响应或账户实付。**profile仅创建新run时生效；已有run保持原额度和思考模式，不能靠启动参数改额。** 不删除、改名或新建run来重置旧失败、关闭域或退役余额。新独立任务才使用新run，并重新给出该任务有限授权。

## 从需求到自动结果

1. 首次启动显示额度，确实同意时输入 `ALLOW robot-task-01`。它只批准有限费用/执行额度，尚未批准具体方案。
2. 在 `artifact>` 直接用中文描述任务，给出载荷、地形/楼梯尺寸与级数、运行方向/目标及硬约束。轮数/构型可明确指定，也可明确委派模型建议。简单动力学任务给质量、作用、初态、仿真时长和目标。缺关键用户事实时集中澄清，无需填写内部协议。
3. 阅读中文方案、需求来源、参数、假设、成功判据和剩余额度。仅有效 `READY` 方案可输入“确认”或“批准”；“拒绝”结束该方案。具体方案确认后连续生成SysML＋Modelica、启动Worker、执行OMC并读回结果，无需再逐步输入生成或源码确认。实质改动须重新展示并确认，Agent不能自批新结构或任务。
4. 查看同任务的工具、链路、观测判据、交付状态和路径。任务额度用过后重开同run自动只读。显式只读命令无需模型额度；退出用单独一行 `/exit`。

任务输入示例：“一维小车总质量10kg，恒驱力20N，线性阻力系数5N·s/m，初始位置和速度为零，仿真3秒，检查末位移至少2m、末速度至少2m/s；必要简化请列出并让我确认。”这是写法示例，本轮未运行该示例，不代表新增验收。

## 任务内对话与查询

| 输入 | 用途 |
|---|---|
| 直接需求，或 `/require 需求内容` | 新增/修改需求，保留旧来源，受影响确认失效 |
| `/ask 当前方案为什么这样设计？` | 咨询当前方案/证据，消耗原有限模型额度，不新增事实、不改批准、不生成执行 |
| “确认”“批准” / “拒绝” | 决定当前已展示有效方案；“确认？”和“拒绝？”属于咨询 |
| “状态”“进度”“结果”，或 `/status` | 当前项目与任务；显式状态命令本地返回 |
| `/task task_ID` | 按真实task ID查状态 |
| `/history` | 当前项目执行快照列表 |
| `/compare v000001 v000002` | 比较同项目版本条件和结果，不合成为车辆能力PASS |
| `/evidence task:task_ID v000001` | 指定真实task及可选版本读证据；`/query`同义，task不可省略 |
| `/help` / `/exit` | 本地帮助 / 退出；`/exit`须单独输入，拼接文字会要求拆开 |

对话限于当前任务，不是通用闲聊。含修改又不确定的问句可能先澄清，用 `/ask` 与 `/require` 消歧。只读模式无模型runtime时使用显式命令，普通中文咨询不能保证无费用。

查询已有run（替换为实际已存在名字，无需再指定profile）：

~~~powershell
.\week11\.venv\Scripts\python.exe -X utf8 -B -m week12.src.car_domain.cli_adapter --backend model-artifact --run EXISTING_RUN --read-only
~~~

已有平台实例只读打开命令如下（run保存原session，无需手填）：

~~~powershell
.\week11\.venv\Scripts\python.exe -X utf8 -B -m week12.src.car_domain.cli_adapter --backend model-artifact --run acceptance-platform-20261014-01 --read-only
~~~

[平台只读证据](results/s5-frozen-acceptance-20261014-01/source-cache-success-readback.json)给出真实任务身份。进入后输入 `/task task_455c7acb82ee463aa7d7f038b43f3df3` 或 `/evidence task:task_455c7acb82ee463aa7d7f038b43f3df3 v000001`。

显式DB只读入口：

~~~powershell
.\week11\.venv\Scripts\python.exe -X utf8 -B -m week12.src.car_domain.cli_adapter --backend model-artifact --read-only --db PATH_TO_EXISTING_WEEK12_DB --session EXISTING_SESSION
~~~

只读MCP采用同样参数，将模块改为 `week12.src.car_domain.mcp_adapter`；工具为project_status、execution_status、evidence_query、versions_list、versions_compare。evidence_query示例参数：`{"question":"task:task_455c7acb82ee463aa7d7f038b43f3df3","version_id":"v000001"}`。

## 失败与恢复

保留原提示和run。退出旧CLI后用原命令重开；旧MCP仍占锁、配置漂移或分派不确定时停止写操作，只读核实，不启动第二Worker。

`/retry`不是通用重跑，只接续已记录且符合准入的同账请求或成功缓存。成功响应可本地重验；明确已知的方案/生成截断有一次有界阶段恢复；公共生成阶段当前仅支持将“缺少已确认字面参数声明及SysML镜像”的已知错误一次反馈产品模型，在原批准字段、参数、动力学/控制等锁内纠正；不支持任意源码、方程或编译错误自动修复。格式、源码、截断恢复不能任意叠加成循环。原失败和实际用量保持；再次失败、未知传输、未知用量/分派、已耗恢复、预算或锁定漂移则停止。

`SOURCE_REVIEW`表示来源不足，补充真实事实后再审，不能直接确认执行。编译、仿真、物理硬失败不自动改源重跑；不让用户手改JSON或删假设凑通过。已完成任务只查询，历史缺图或SysML不补造旧清单。

## 文件与结论

- `week12/runs/<run>/`：run.json、原runtime/context、累计SQLite账本、projects.sqlite；`cli-turns`保留请求/响应，`cli-continuations`保留方案和连续流程。未知预留不能当零费用。
- `week12/projects/<project>/versions/<version>/`：同任务冻结spec、`frozen-model.sysml`、`frozen-model.mo`、SysML/双源码核查、`artifact-manifest.json`、`tool/*_res.csv`、`curves/signals-*.png`和结果。以CLI实际路径为准，不凭目录存在判PASS。
- `week12/results/`：开发检查、只读分析和验收证据。曲线来自实际CSV映射，不代替原数据或独立物理审查。

工具PASS表示所列语言/编译/仿真阶段通过；链路PASS表示身份和证据串联成功；观测任务PASS/FAIL只针对已确认末值或有限采样轨迹判据。缺证据为UNKNOWN/NOT_VERIFIED，未执行为NOT_RUN；工具错误不能称车辆物理FAIL。车辆有独立结论，目前平台和一维例均UNKNOWN。快照 `published=false`，并非已发布认证车辆版本。

## 支持边界与依赖

支持内建方程及允许的Modelica标准库子域，产品模型按需求提出方案，不按轮数选择固定案例模板。SysML核查覆盖支持语法、共享数值字面参数、逐条需求/可信来源和Modelica模型名关联；不证明全部结构、单位、方程语义等价，也不证明声明参数被动力学实际调用。任意类型、参数表达式与任意MSL调用不保证支持。

轨迹协议是 `outputs: label→CSV变量`，实体x/z引用label；同实体位置区间严格分离，后区间x_min大于前x_max，宽度大于两倍位置容差。只检有限采样的正向顺序窗口；点/轮轴轨迹不等于车体外形碰撞，阶段名称不扩大判据。来源不明、非法/未知字段、外部函数、文件/资源/进程访问、MOS/shell等拒绝；当前不是任意源码安全沙箱。

小规模真实新输入已证明双源码→OMC→CSV/曲线→查询闭环，原失败与修复后复验保留。仍未证明任意输入/构型、通用机械设计、全车体无穿透、完整20级、能力扫描、供电/能量/续航或实车可信度。平台M3/M4名称不证明全车同时在顶面或全部越出口0.15m；前轮17.55秒超出模型地面x=5后的下降不能作正常平地巡航证据。详见[验收总结](V11-交付验收总结.md)。后置事项不自动成为V12范围；V12仍按原目标做有界自动改进，未自动启动。

依赖基线：Windows现有 `week11/.venv`、OpenModelica 1.25.7、MSL 4.0.0、sysmlpy 0.34.1，以及现有CLI/MCP、httpx和绘图依赖。本次未安装、升级、调用API或重跑仿真；跨电脑安装复现仍属后续交付范围。沿用来源：[OpenModelica](https://openmodelica.org/doc/OpenModelicaUsersGuide/latest/)、[MSL 4.0.0](https://doc.modelica.org/Modelica%204.0.0/Resources/helpOM/Modelica.html)、[sysmlpy](https://pypi.org/project/sysmlpy/)，不声称本轮重核最新版本。

旧A/B、用户原案和八轮路线的过程保留在[WORKLOG](WORKLOG.md)与原结果，不重跑、不恢复closed账本。收口前文档有[实际备份索引](results/s5-closeout-20261015-01/backup-index.json)；本次第5项核验见[check.json](results/s5-closeout-20261015-01/check.json)。

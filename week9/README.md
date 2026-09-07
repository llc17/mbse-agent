# V8：验证驱动的自适应 Agent

V8 已于 2026-09-01 结项：实现和正式 100/100 批次均已完成，结项状态为
`COMPLETED_WITH_FORMAL_FAILURE`，正式 verdict 为 `FORMAL_FAIL`。完整结项边界见
`V8结项报告.md`，DeepSeek 开发集故障分层与 V9 单一假设见
`V8失败复盘与V9启动建议.md`；机器可读状态见
`configs/v8_closure_record_v1.json`。这些新增文件只总结公开证据，不改变冻结协议、
正式报告或已消费授权。

面向学习和项目回顾的完整版本见 `V8总结.md`，其结构参考 V7 总结，集中说明 V8 的
设计、文件职责、阶段证据、正式实验、失败原因、能力边界和 V9 衔接。

当前完成了阶段 0 的离线合同和认证模板自然语言入口、阶段 1 的“模板认证 +
硬质量门”、阶段 2 面向已有认证产物的离线错误驱动修复闭环、阶段 3 的参数
规范化与受限离线检索机制验证，以及阶段 4 的自适应路由、预算账本、需求追踪
和离线机制回归。主 runner 现在能把五类认证拓扑的显式中文需求送入完整 pipeline；
默认入口使用可审计的本地规则，不声称具有通用自然语言理解能力。歧义、缺参数、
刺激类型冲突和未覆盖拓扑仍会明确阻断。受协议约束的 DeepSeek 投影入口和正式
四臂实验代码已经离线实现、mock 验证并按 SHA-256 冻结；开发集上的同期 V4/V6
DeepSeek Pilot 与 Kimi 稳定性批次已经完成。首次正式比较在开发集 9/100 处因基础
设施合同缺陷中止且没有访问留出集；修复后不复用旧记录，从零启动的替换批次又在
22/100 处因 V6 留出用例适配器超时而中止，其中已经执行 8 次留出运行。替换批次
没有生成正式报告，旧留出集现已锁定且不能再用于确认性推断。随后完成了离线恢复
协议、外层运行信封和开发集 adapter dry-run；独立来源提供的两个新案例通过非披露
校验并绑定公开 manifest。最终盲态协议 v2 在一次性授权下完成新的 100/100 四臂
批次，基础设施故障为 0，正式结论为 `FORMAL_FAIL`：V8 默认本地臂 25/25 成功，
但受控 DeepSeek 臂未达到相对 V6 的预登记开发集提升阈值。新留出和批次授权现均已
消费，不能通过修改实现后重跑来改变该结论。

角色边界已经固定：GPT/Codex 负责开发并只记作开发来源，不是实验运行臂；DeepSeek
负责 V4、V6 和 V8 的同生成器受控运行；V8 的实际默认入口仍是确定性本地生成；Kimi
只做盲化辅助审查，不参与代码开发、不生成候选，也不能覆盖确定性硬判定。因此不把
GPT/Codex 再拆成第二套 V8，而是在同一 V8 下保留“受控 DeepSeek”和“默认本地”双视图。

## 当前已经建立的边界

- `src/schemas.py`：规范参数、五态检查、产物、修复证据、哈希链路由、预算账本、
  需求追踪和统一 `RunResult`；自适应合同缺失或交叉字段不一致时拒绝序列化。
- `src/quality_gate.py`：所有必需检查只有全部为 `PASS` 才能发布。
- `experiments/external_evaluator.py` 与 `experiments/cross_version_validation.py`：不读取
  版本内部 success；对比较产物重新执行 SysML 解析、Modelica 编译/仿真、参数、
  物理行为和跨模型检查。三类冻结开发拓扑已通过真实 OpenModelica 版本外复核。
- `experiments/version_adapters.py`：V4、V6、V8 独立子进程接口和唯一运行目录；
  三个版本的最终结果都必须经过版本外重新验证后才能进入比较报告。
- `experiments/subprocess_control.py`：统一子进程执行和超时树回收；Windows 使用
  kill-on-close Job Object，其他平台使用独立进程组，失败会进入基础设施错误。
- `experiments/cases.py`：开发回归、留出和错误注入数据集及哈希。
- `configs/experiment_protocol.json`：公平性、预算冻结点和在线运行政策。
- `src/modelica_templates.py`：5 个只允许显式参数槽的认证模板；缺参数、错单位、
  未知参数和后续全文件改写均会失败。
- `src/modelica_tool.py`：每次使用唯一目录执行 `loadFile → checkModel → buildModel`
  和独立仿真可执行文件；CSV 缺失、非有限值、冲突重复时间、时间倒退、
  时间窗覆盖不足或缺信号均不算成功；超时后清理完整进程树并记录清理证据。
- `src/diagnostics.py`：把真实 Modelica 诊断保守分类为固定子类；无法稳定识别时
  返回 `unknown_diagnostic`，不会用宽泛检索掩盖未知错误。
- `src/parameter_normalization.py`：模板作用域内的精确别名和显式单位换算；缺失值、
  未知别名、非法单位、非有限值和冲突重复值全部阻断，不补默认值、不从散文猜值。
- `src/generation.py`：五类认证拓扑的零网络、零模型 token 自然语言准备入口；只提取
  与已知标签绑定的显式数值，RC/RLC 仅在需求明确要求时使用冻结公式推导电容，
  同时生成逐字跨度追踪输入。它不是任意需求生成器。
- `src/online_generation.py`：显式选择后才会构造的单次 DeepSeek 投影入口；冻结
  `temperature=0.3`、`max_tokens=8192` 和严格 JSON schema，保存 prompt、请求、
  响应、正文及本地 verifier 哈希。模型投影只有与本地确定性结果一致时才能进入
  pipeline，无法绕过认证模板、参数规范化、硬门和追溯。
- `src/retrieval.py`：完全离线、先硬过滤后稳定排序的词法检索；严格绑定模板、语言、
  修复层、错误类型、诊断子类、库版本、作用域、源文件哈希和认证状态。
- `configs/retrieval_corpus.json`、`.sha256` 与 `retrieval_policy.json`：冻结 5 个原子
  开发片段及 corpus 哈希；默认关闭，`top_k=3`，单片段最多 160、总计最多 480
  个估算 token，每次运行最多一次检索，片段不能截断。
- `src/hard_gate.py`：ANTLR SysML 语法、参数/单位、模板完整性、认证锚点、
  执行证据溯源、编译、仿真、物理行为，以及限定在 `part def System` 作用域的
  参数、组件和关键连接一致性硬检查。
- `src/repair_loop.py`：内容修改、基础设施重试和已提交快照三套独立账本；支持
  no-op、重复错误、回归回滚、内容预算和基础设施预算等确定性停止条件。
- `src/routing.py`：只有规范输入、认证模板和首次完整十项硬门通过才能走 fast path；
  其余分支显式记录修复、重建、重试、检索或前置阻断原因，并生成不可变预算事件。
- `src/traceability.py`：使用显式绑定把需求 ID/原文跨度连接到规范参数或拓扑约束、
  精确 SysML/Modelica 选择器和硬检查证据；关键需求 `NOT_IMPLEMENTED` 会阻断发布。
- `src/pipeline.py`：对候选源码重新编译、仿真并执行十项硬检查；Modelica 只允许
  从认证骨架和已声明参数槽重新渲染，不允许自由全文改写；最终结果同时绑定初始
  输入快照、路由终态、预算用量、最终产物和追踪报告。
- `src/runner.py`：冻结 case、原始需求和 case 哈希一致后，执行自然语言准备与完整
  认证 pipeline；未知 case、留出集未授权、解释器异常或合同漂移都写出失败合同。
- `experiments/repair_regression.py`：四类真实反例的离线对照，定向修复 4/4，
  原样重试 0/4；每个成功案例恰好发生 1 次真实且被接受的内容修改。
- `configs/stage4_adaptive_regression_suite.json`、
  `experiments/run_adaptive_regression.py` 与 `experiments/report_adaptive_regression.py`：
  冻结 12 个开发用例并执行纯离线机制回归；报告只能给出
  `MECHANISM_REGRESSION_PASS/FAIL`，不能产生正式发布或成本优势结论。
- `configs/stage0_natural_language_regression_suite.json` 与
  `experiments/run_natural_language_regression.py`：冻结 3 个开发需求的端到端离线
  预期，使用独立子进程运行真实 OpenModelica，并校验报告原始字节 SHA。
- `experiments/run_contemporaneous_pilot.py`：冻结开发集上的 V4/V6 三次成对、交错、
  顺序平衡 Pilot；只接受相同生成模型配置和版本外复核结果。协议未授权时拒绝启动。
- `experiments/run_judge_stability.py`：从通过的 Pilot 固定至少 3 份产物，每份至少
  重评 3 次；送入 Kimi 前删除版本、Agent 和路径标记，并按标准差决定单次分数、
  重复中位数或仅定性评论的辅助证据策略。
- `experiments/propose_release_thresholds.py`：只根据通过的 V4/V6 Pilot 生成阈值提案，
  不读取 V8 正式结果、不访问 holdout，也不会自动改写冻结协议。
- `experiments/run_formal_comparison.py`：在 Pilot、Judge 稳定性、阈值、授权和 holdout
  全部冻结后，按四臂轮换运行开发集与留出集。`v4_default_deepseek`、
  `v6_default_deepseek`、`v8_controlled_deepseek` 构成同生成器受控视图；
  `v4_default_deepseek`、`v6_default_deepseek`、`v8_default_local` 构成默认配置视图。
  只有前三臂可作同生成器公平性/质量归因；V8 本地默认臂的 token、耗时和修复预算
  只作系统级证据，并且必须单独满足开发改进和留出非劣质量底线后才能支持成本结论。
  正式推荐的 5 次重复共计划 5 个用例 × 4 臂 × 5 次 = 100 次运行。
- `experiments/run_formal_judge.py`：从正式报告固定选择每个“用例 × 实验臂”的第 1 次
  产物，按已测 Judge 波动策略执行单次、重复中位数或仅定性盲评；规范化盲化正文
  内容哈希相同时只评分一次，再把同一分数映射回全部来源臂，以避免重复计费。它明确
  禁止修改正式硬判定。
- `experiments/experiment_readiness.py`：只读检查协议、开发回归、Pilot、Judge、阈值、
  正式实现哈希、在线授权、留出集、正式比较和正式 Judge 状态；报告除了 SHA 还必须
  通过 schema、verdict、范围和计数语义检查，不读取密钥值、不访问留出内容、不发起
  网络请求。
- `experiments/formal_recovery.py` 与 `formal_recovery_harness.py`：为未来新实验增加
  版本外层尝试信封；预登记时限且进程树清理成功记作已完成的实验臂失败，启动、
  清理、harness 或统一 evaluator 异常才记作批次基础设施失败。有效候选合同仍以
  统一外部 evaluator 为唯一成功口径，不能由旧版本内部退出码覆盖。
- `experiments/formal_recovery_adapters.py`：在不改旧冻结 adapter 的前提下建立四臂
  命令接口；当前只允许开发集 `v8_default_local` 真正执行，三个付费臂和任何
  holdout-shaped case 都在进程启动前阻断。
- `configs/formal_recovery_protocol_v2.json`：绑定恢复策略、11 项故障注入预检和
  3/3 开发集 adapter dry-run；在线运行、正式 Judge、旧记录复用、旧留出访问以及
  尚未提供的新留出集继续保持关闭。
- `experiments/validate_independent_holdout_seal.py` 与
  `configs/independent_holdout_binding_v1.json`：仅把私有新留出作为不透明字节保存在
  项目外；校验公开 manifest、文件哈希、结构、范围、公式、本地语法与 oracle 一致性，
  报告不输出案例 ID、类型、需求、参数或答案。新增绑定层不修改恢复协议 v2，也不
  解锁付费运行。
- `configs/template_certifications.json`：OpenModelica 1.25.7 / MSL 4.0.0 下的
  正式发布摘要：5/5 正确样本通过、8/8 冻结错误注入被阻断，并锚定模板、
  门禁、合同、修复闭环、检索、路由、追踪、Stage 4 回归实现与冻结配置的
  27 份代码和配置哈希。

V4/V6 的 `success`、`cross_validate_passed` 和 `physics_passed` 仅作为
`legacy.*` 观察项保存，不能决定 V8 的 `overall_success`。

## 离线验证

从项目根目录运行：

```powershell
python -B -m unittest discover -s week9/tests -v
python -B -m week9.experiments.freeze_protocol
python -B -m week9.experiments.certify_templates
python -B -m week9.experiments.repair_regression
python -B -m week9.experiments.run_retrieval_ablation --repeats 3
python -B -m week9.experiments.run_natural_language_regression
python -B -m week9.experiments.run_adaptive_regression
python -B -m week9.experiments.certify_subprocess_cleanup
python -B -m week9.experiments.experiment_readiness
python -B -m week9.experiments.freeze_formal_recovery_protocol
python -B -m week9.experiments.freeze_independent_holdout_binding
python -B -m week9.experiments.validate_v8_closure
```

开发期 V4/V6 Pilot 与 Kimi 稳定性批次已经各执行一次。当前在线开关已重新关闭，
下列命令只用于说明可复现入口；没有新的、限定范围的明确授权时不要重跑：

```powershell
python -B -m week9.experiments.run_contemporaneous_pilot `
  --execute-online --confirmation-id <协议中的确认ID> --repetitions 3
python -B -m week9.experiments.run_judge_stability `
  --pilot-report <通过的pilot报告> --execute-online --confirmation-id <协议中的确认ID>
```

阈值提案已经离线生成、人工写入协议并在任何 V8 正式结果和留出访问之前冻结：

```powershell
python -B -m week9.experiments.propose_release_thresholds `
  --pilot-report <通过的pilot报告>
```

以下正式入口已经实现，但这里只保留为历史复现接口说明，当前协议会拒绝执行。
原正式比较计划包含 100 次运行；旧留出集已经被替换批次访问，不能靠一次新的付费
授权重新启用。新的确认性实验必须先取得由独立来源生成并密封的新留出集，重新冻结
实现、配置和协议哈希，再取得与新批次绑定的明确授权：

```powershell
python -B -m week9.experiments.run_formal_comparison `
  --pilot-report <通过的pilot报告> --judge-report <通过的Judge稳定性报告> `
  --execute-online --confirmation-id <新协议中的确认ID> --repetitions 5
python -B -m week9.experiments.run_formal_judge `
  --formal-report <新批次的完整正式比较报告> --stability-report <通过的Judge稳定性报告> `
  --execute-online --confirmation-id <新协议中的确认ID>
```

只有在有意重新认证正式发布摘要时，才运行：

```powershell
python -B -m week9.experiments.certify_templates `
  --release-summary-out week9/configs/template_certifications.json
```

发布摘要不是手工整理：认证程序只会把 `stage1_gate_passed=true` 的完整候选结果
转换成硬门消费者使用的固定 schema；默认发布模式还会反算其中的模板和错误注入
计数，任何实现哈希、环境版本、逐项证据或汇总漂移都会要求重新认证。

单用例 CLI 合同：

```powershell
cd week9
python src/runner.py --case rc_lowpass --out <result.json>
```

对冻结开发 case，该命令会先运行本地自然语言准备，再执行完整认证 pipeline；成功
返回 0，任一必需检查失败则写出完整失败合同并返回非零退出码。已有明确
`template_id` 和结构化参数的调用方仍可直接使用 `CertifiedTemplateRepairPipeline`。

阶段 1 的 SysML 硬门固定使用 `sysmlpy.antlr_parser.parse`，并用一正一反哨兵
做启动自检。当前只声明“ANTLR 语法 + 最小结构”能力，不声称已完成完整 SysML
语义验证；`sysmlpy.analyze()` 在当前版本对部分合法项目写法仍有误报，后续必须
先校准再升级为硬门，不能用正则 fallback 伪造 `PASS`。

认证模板在本机的行为断言分别覆盖 RC 时间常数、RLC 正弦频率与谐振增益、
反相运放增益/电源轨限幅、单房间解析热响应、双房间被动温度边界与共同稳态。
动态仿真窗口另以 1 Hz RC 合法参数验证；RLC 激励被替换为可编译的阶跃源时，
物理门仍会拒绝。
正式运行证据写入唯一的 `results/template-certification-*` 目录；修复对照和
Stage 4 机制回归证据分别写入唯一的 `results/repair-regression-*` 与
`results/stage4-adaptive-regression-offline-*` 目录（均已忽略提交）。

## 阶段 0 自然语言入口的当前结论

最新离线回归在三个冻结开发需求上全部通过：RC、RLC 和双房间热均从原始中文需求
进入 fast path，命中正确认证模板，完成一次真实十项硬门验证与追踪检查；模型 token
为 0，未触发检索、修复、在线 API 或留出集。报告原始字节 SHA-256 与伴随文件一致。

这证明的是认证拓扑显式需求的低成本本地入口，不证明任意自然语言、未知组件或新
拓扑的泛化。未覆盖需求仍会失败闭合，不能把 3/3 写成通用生成能力。证据：

- `results/stage0-natural-language-offline-20260831T032814Z/natural-language-regression-report.json`
- `results/stage0-natural-language-offline-20260831T032814Z/natural-language-regression-summary.md`
- `results/experiment-readiness-20260831T154517Z/experiment-readiness-report.json`

## 阶段 3 的当前结论

阶段 3 只做开发期机制探针，没有访问正式留出集，也没有使用在线 API。消融在
同一份不可变参数观察快照和相同最大修复预算上重复 3 次：关闭检索时 0/3 在必需
参数规范化处被明确阻断；开启检索时仅使用冻结片段
`parameter.rlc.source-amplitude.001`，每次计入 63 个估算 token，并在真实
OpenModelica 下通过全部 10 项硬检查，结果为 3/3，且没有 hard regression。

这只证明受限检索能补齐一个合成开发知识缺口，不证明端到端泛化，也不满足默认
启用门槛。因此安装策略和正式协议继续保持检索关闭，决策为 `KEEP_DISABLED`。
本次可复核证据：

- `results/template-certification-20260831T032407Z/certification.json`
- `results/repair-regression-20260830T050601Z/repair-regression.json`
- `results/retrieval-ablation-20260830T050659Z/retrieval-ablation.json`

## 阶段 4 的当前结论

冻结的开发套件共 12 个用例，最新真实离线运行与逐项预期 12/12 匹配：6 个走
fast path，6 个走升级、修复或显式阻断路径，非法 fast release 为 0。这里的
“12/12”表示路由、追踪、预算和失败闭合机制均符合冻结预期，不表示 12 个用例
都发布成功。报告 JSON 的原始字节 SHA-256 已与伴随 `.sha256` 文件复核一致。

本次可复核证据：

- `results/stage4-adaptive-regression-offline-20260831T032903Z/adaptive-regression-report.json`
- `results/stage4-adaptive-regression-offline-20260831T032903Z/adaptive-regression-summary.md`
- `results/template-certification-20260831T032407Z/certification.json`
- `results/subprocess-cleanup-certification-20260831T023937Z/subprocess-cleanup-certification.json`

当前全量离线测试为 287/287 通过（包括 2 项新增结项记录防篡改测试），最新模板再认证为
5/5 通过且 8/8 冻结错误注入
被阻断；进程树清理连续 3/3 次认证通过，协议中的
`process_tree_cleanup_ready=true` 已绑定认证报告和实现哈希。Stage 4 的离线机制结论
仍只到 `MECHANISM_REGRESSION_PASS`，不能替代正式四臂实验。

## 已完成的付费开发期证据

同期 DeepSeek Pilot 在 3 个冻结开发用例上完成 V4/V6 各 9 次，共 18/18 次运行，
调度、运行 ID、版本外评估器、基础设施和成对生成器公平性检查全部通过，结论为
`PILOT_PASS`。这个 verdict 表示批次有效，不表示内容质量已经达标：版本外硬判定中
V4 成功 1/9、V6 成功 2/9；双房间热和 RLC 两个版本均为 0/3，RC 为 V4 1/3、
V6 2/3。批次合计使用 487,056 个 prompt token 和 124,400 个 completion token；
V4/V6 全部用例的总 token 中位数分别为 10,311/37,178，耗时中位数分别为
47.9/122.3 秒。它只建立同期基线，不能推出 V8 优越性。

第一次启动在完成 1/18 后发现运行期间 `week8/V7总结.md` 被外部修改，因冻结源哈希
漂移而失败闭合；该目录保留 `INCOMPLETE.md`，不计入阈值或结论。有效批次改用其
启动前的只读字节快照，SHA-256 为
`851004de5abd8308dff453fa5d33fa258d541654c7f1d0b11f2e844f1bb9fd28`，没有覆盖历史文件。

Kimi 随后对 3 份固定且盲化的开发产物各复评 3 次，9/9 响应有效并得到
`JUDGE_STABILITY_PASS`。三组总分标准差分别为 2.52、0.58 和 6.35；最大波动使正式
策略冻结为 `QUALITATIVE_AUXILIARY_ONLY`。Kimi 只能提供定性辅助评论，其数值分数
不能解释小版本差异，也不能覆盖确定性硬判定。

发布阈值已经依据有效 Pilot 在任何 V8 正式结果和留出访问之前冻结：开发集成功率
至少比 V6 高 1/9，留出非劣容差为 0，简单用例 token/耗时预算分别为 33,198 和
75.1 秒，每次运行最多 1 次内容修复。阈值证据、Pilot 和 Judge 报告均由协议绑定
SHA-256。

首次正式四臂尝试在开发集完成 9/100 后中止：已产生 7 次 DeepSeek 调用和 2 次
本地运行，没有执行任何留出用例，也没有生成正式报告。监控发现 V8 本地产物在版本外
重建结果时被错误标为 `adapter_execution_error`；根因是 evaluator 复制了修复计数，
却遗漏对应的修复/重试审计记录。修复只透传审计证据，不改变候选产物或硬检查逻辑。
两个现场失败产物均已离线重放为外部硬门通过、计数一致且无基础设施错误，新回归测试
也已加入。修复后的 11 个正式实现文件已重新绑定哈希；中止目录只用于成本和审计，
不能并入正式结论。

正式重启前另用真实 V8 本地适配器对 RC、RLC 和双房间三个开发用例做了不访问网络、
不访问留出集的 3/3 预检，版本外复核全部通过且基础设施错误为 0。随后按新确认记录
从零启动完整替换批次，没有复用首次中止的 9 条记录。替换批次在 22/100 处停止：
已执行 16 次 DeepSeek 调用和 6 次确定性本地运行，其中开发集 14 次、留出集 8 次。
第 18 项 V6 三房间留出运行达到冻结的 900 秒适配器时限，记录至少 1 项基础设施
错误；Windows Job Object 进程树清理成功。批次随即停止，没有生成正式比较报告，
也没有调用正式 Kimi Judge。

由于替换批次已经访问留出集，不能再根据这次超时或留出表现修改适配器、超时时限、
实现、阈值或判定规则后重跑同一留出集；22 条记录也不能拼接进将来的新批次。当前
只能将该次实验记为“基础设施导致的正式实验不完整”，而不能记作 `FORMAL_PASS` 或
`FORMAL_FAIL`。旧留出集对确认性推断已经消耗。

最新就绪审计中，协议、自然语言开发回归、Pilot 和 Judge 均为 `PASS`，阈值为
`FROZEN`，11 个正式实现文件为 `READY`；在线执行为 `DISABLED`，留出集为
`LOCKED_AFTER_ACCESS`，正式结果为 `INCOMPLETE`，正式 Judge 为 `NOT_RUN`，所以
总体为 `BLOCKED`。目前不能声称 Stage 4 正式实验完成，也不能声称 V8 比 V4/V6
更快、更便宜或更优。

本次可复核证据：

- `results/contemporaneous-pilot-20260831T082240Z/contemporaneous-pilot-report.json`
- `results/judge-stability-20260831T090435Z/judge-stability-report.json`
- `results/release-threshold-proposal-20260831T091033Z/release-threshold-proposal.json`
- `results/formal-local-preflight-20260831T145342Z/formal-local-preflight-report.json`
- `results/experiment-readiness-20260831T154517Z/experiment-readiness-report.json`
- `configs/source_snapshots/V7总结-pre-pilot-20260831T081903Z.md`
- `results/contemporaneous-pilot-20260831T081641Z/INCOMPLETE.md`（中止批次，仅作审计）
- `results/formal-comparison-20260831T141602Z/INCOMPLETE.md`（9/100 中止，无留出访问）
- `results/formal-comparison-20260831T150136Z/INCOMPLETE.md`（22/100 中止，已访问留出）

## 正式实验恢复阶段的当前结论

恢复工作完全是新增的离线层，没有修改原 V8、旧正式协议或历史结果。新策略统一应用
于四个实验臂：达到预登记的 900 秒时限且进程树清理成功时，记录为
`valid_arm_failure`、计入已完成运行但 `overall_success=false`；清理失败、进程无法
启动、外层 harness 异常或统一 evaluator 自身报错才是 `invalid_infrastructure`。
候选合同有效时，统一外部 evaluator 的 PASS/FAIL 仍是唯一质量口径，即使旧版本
内部 gate 返回非零码也不能覆盖外部硬判定。此规则只适用于未来使用全新密封留出集
的实验，明确禁止重新解释原来的 9 条或 22 条记录。

纯离线故障注入预检 11/11 符合预期，覆盖正常成功、外部硬失败、有效合同非零退出、
合同缺失/非法、预登记超时、清理失败、启动失败、evaluator 错误和 harness 错误；
真实 0.5 秒超时在 Windows Job Object 下完成进程树清理。随后四臂命令均完成只构造
不执行测试，三个付费臂保持启动前阻断；真实 V8 本地 adapter 在 RC、RLC、双房间
三个开发用例上从 0 dry-run，3/3 经版本外 evaluator 通过，基础设施错误为 0，
网络、模型、凭据转发和留出访问均为 0。

恢复协议 v2 继续以原摘要保持
`offline_recovery_complete_new_holdout_required`，没有被追改。独立来源提交的两个
新案例现已在项目外冻结；公开 manifest、sidecar、私有文件字节数和 SHA-256 一致，
非披露验证的 18/18 项检查通过。新增
`independent_holdout_binding_v1` 已把 `independent_holdout_manifest_bound` 置为真，
同时保持私有正文不可见、凭据未读、在线运行和正式 Judge 均关闭。

盲态正式执行协议公开调度只使用 `sealed_h01` 和
`sealed_h02`，固定为 5 个案例、4 个实验臂、5 次重复，共 100 次运行；真实留出正文
只允许在新增授权通过后的私有 worker 中加载，OS 命令行、项目内日志和公开报告均不
携带案例正文、原始 ID、参数或 oracle。v1 在授权前发现 `deepseek-chat` 已被官方
停用，因此以“未授权、未付费调用、未由正式执行加载留出正文”状态被 v2 取代。v2
固定 `deepseek-v4-flash`、官方 API 主机和 `thinking=disabled`，三个在线臂在请求
发送前执行相同配置检查；没有改动 V4/V6 历史代码。新版合成执行预检 9/9 通过，确认
没有读取真实新留出、旧留出、凭据或网络，也没有调用 DeepSeek/Kimi。最终协议摘要为
`69c2b0de0530b524429729da0707bc0c0651ded2d2df65c760a2acd9b265b7f8`。用户随后为该
精确摘要批准一次且仅一次的 100 次付费批次；授权收据 SHA-256 为
`4fb046e3ea22f11f8fff3f28807494b83f880bd9237311f40eb91bcd629c1d53`，已在首次模型
调用前消费，不能再次使用。

v2 正式批次 100/100 完整结束，25/25 配对组生成配置一致，统一外部 evaluator、
离线本地臂、进程清理及隐私条件全部通过，基础设施故障为 0。冻结结论为
`FORMAL_FAIL`：V4、V6 和 V8 受控 DeepSeek 臂分别为 0/25、0/25、0/25；V8 默认
本地臂为 25/25。七项发布条件中六项通过，唯一失败项是受控 V8 在开发集上的成功率
没有比 V6 提高冻结的 1/9；留出非劣、默认本地视图的开发提升与留出非劣、简单案例
token/耗时预算及修复预算均通过。因此可以报告“V8 默认本地机制在本批范围内通过”，
但总体预登记结论仍是正式失败，不能声称受控 DeepSeek 视图优于 V6。公开正式报告
SHA-256 为 `83e645d2c1c164b7931ae0cd300f2c008e6f0fe1570f08f9582f36695c44abcb`。

本阶段可复核证据：

- `configs/formal_recovery_protocol_v2.json`
- `configs/independent_holdout_binding_v1.json`
- `configs/blinded_formal_protocol_v1.json`（授权前被替代的历史摘要）
- `configs/blinded_formal_protocol_v2.json`（当前最终协议）
- `results/formal-recovery-preflight-20260901T013304429050Z/formal-recovery-preflight-report.json`
- `results/frd-20260901T013410Z/formal-recovery-adapter-dry-run-report.json`
- `results/independent-holdout-intake-20260901T022430Z/independent-holdout-validation-report.json`
- `results/blinded-execution-preflight-20260901T063231544140Z/blinded-execution-preflight-report.json`
- `results/blinded-formal-20260901T064757Z/blinded-formal-report.json`
- `results/formal-authorization-consumption/4fb046e3ea22f11f8fff3f28807494b83f880bd9237311f40eb91bcd629c1d53.consumed.json`

## 当前禁止的操作

- 不修改 `week5`、`week7`、`week8` 或历史结果。
- 未取得新的限定范围授权时，不重跑付费 smoke、Pilot 或 Kimi 稳定性批次。
- 不继续或重启 v2 正式四臂实验；该批授权已经消费，100 条记录不可重新生成或拼接。
- 不再把旧留出集用于调参、调超时、修改适配器、重新判定或确认性重跑。
- 不把本次新留出集用于根据正式结果调参、修改适配器或再次确认性运行。
- 不把两次中止批次的 9 条或 22 条记录拼接到新的正式批次。
- 不运行正式 Judge。
- 不人工读取、复制、打印或调试输出新留出集正文；仅允许冻结的非披露验证器按协议
  读取并输出布尔检查与哈希。
- 不把恢复协议、留出绑定、盲态协议或已消费的授权收据当作新的在线授权。
- 不把 RAG 或 Kimi Judge 当作硬成功条件。

在线 adapter 同时受显式调用参数、冻结协议、一次性用户确认记录和进程树清理能力
约束。v2 批次已经完成并给出 `FORMAL_FAIL`，其新留出和授权均已消耗；不得通过查看
正式结果后修改实现并重跑来改变结论。Kimi 正式 Judge 仍未运行，若以后单独授权，
也只提供定性辅助评论，不能改变本次确定性正式失败。若要开展另一项确认性实验，必须
重新准备开发者未见的独立留出、重新冻结协议并取得新的批次授权。

`experiments/quality_gate_practice.py` 已提供带注释的用户练习骨架，核心 TODO 仍留给
用户手写；该文件没有接入正式 pipeline，也没有预填完整答案。

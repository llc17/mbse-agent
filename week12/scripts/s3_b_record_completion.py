"""Update current documentation from accepted B evidence, without running models."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RESULT = ROOT / 'results/s3-b-auto-repair-20260923-02/final-result.json'


def replace_once(text, old, new):
    if text.count(old) != 1:
        raise ValueError('Expected unique documentation anchor: ' + old[:100])
    return text.replace(old, new, 1)


def main():
    result = json.loads(RESULT.read_text(encoding='utf-8'))
    if result['status'] != 'PASS_S3_B_FROZEN_COMPONENT_FIXTURES':
        raise ValueError('B is not accepted')
    if result['budget']['cumulative_scene_attempts'] != 26 or result['budget']['cumulative_local'] != 25:
        raise ValueError('Unexpected budget')
    updates = {}
    updates[ROOT/'V11-CURRENT-STATE.md'] = '''# V11 当前状态

更新：2026-09-23；窗口 `V11-W13-S3-基础物理组件-20260922`。

## 当前结论

- **S3的A、B已完成；C–E未进入，S3/V11未完成。** B六组冻结小场景的基准、加严物理校核与数值对照全部通过；结论只适用于已验证开发夹具。
- 最新[验收结果](results/s3-b-auto-repair-20260923-02/final-result.json)、[证据摘要清单](results/s3-b-auto-repair-20260923-02/accepted-evidence-manifest.json)、[本轮记录](results/s3-b-auto-repair-20260923-02/round-01/batch-result.json)。B01依赖未变，复用原接受证据，未重跑。
- GREEN：当前范围、源码和证据已核对，可在本窗接续；实际客户端上下文占用未知，无需因B完成换窗。
- Git HEAD `ab8f0e3f2e35b661f89d62415ed61773be91b145`；已跟踪文件无改动，用户未提交内容保留，未提交/上传。

## 修复与执行规则

- 用户要求小错误自动修复，并进一步要求“改修不合理的地方，继续完成自动修复”。根AGENTS及[B执行补充](任务规划/B-自动修复执行补充.md)已落实：有限轮次内自动定位、修复和验证，不逐错等“继续”；执行预算不阻塞代码准备和静态审查，预算按完整受影响场景集预留。
- 公共接触改用参数化方程，避免旧算法循环生成事件关系时混用缓存编号；增加构建后、仿真前的生成C结构检查。有符号面距离改用SI.Position。未手改生成C、屏蔽事件、放宽判据或调整物理方案。
- B02–B06共10次真实运行，构建、事件代码检查、仿真、独立物理校核全部PASS，5组成对数值比较PASS；4项定向本地检查PASS。B06已完成高台支撑→离地→低台落地，旧穿地失败保留。
- OMC1.25.7、MSL4.0.0本机维护修订与原依赖不变；组件版本及源码内容以本轮source-manifest/descriptor绑定。冻结[B协议](任务规划/B-基础物理组件与验证协议.md)SHA-256为 `cc9875ad68b44aa1d614e54fa16226165d527b63f8b052de9ef2b0efb8f13de8`；正文初始状态属历史，当前进度以本页与结果为准。

## 实际用量

- 最新自动包使用1/3轮、场景10/30、本地4/12，执行耗时247.31秒/45分钟；已成功收尾，余量不转给其他包。
- B历史累计：场景尝试26、本地检查25次、真实仿真启动22/完成21、旧CSV重处理1、只读工具探测2；产品在线模型调用/token/费用均0。历史失败未改写。

## 边界与下一步

- 本次B工作已完成，当前没有待修复阻塞。后续候选为C的两种结构集成，但本次未授权进入；后续须明确C范围、具体方案和有限预算，不能直接启动整车运行。
- A物理后端仍UNSUPPORTED，B夹具不等于通用A→物理生成或产品CLI/MCP已打通。未重跑A/R7，未进入C–E、整车调参、能力扫描或V12。
- V11正式出口仍含20kg设备载荷、普通轮胎、连续20级上下行、车头朝下/前轮先下的正向下楼、完整出入口后继续巡航；六个组件小场景不证明这些整车能力。
- 用户原件 `models/user-4w2n/source-v1/`、原样运行及全部失败保留；成果基于用户模型适配，不冒充AI独立设计。
- 日常入口：[README](README.md)、[S3启动](任务规划/S3-启动.md)、[WORKLOG](WORKLOG.md)；规则见[需求与设计规则](任务规划/专用小车系统-需求与设计规则.md)，复用依据见[官方索引](任务规划/B-官方资料与示例索引.md)。
'''
    path = ROOT/'README.md'
    old = path.read_text(encoding='utf-8')
    end = old.index('产品规则集中于')
    text = '''# V11 小车建模与能力验证（开发中）

当前主线为S3组件化建模，A、B完成。B01–B06六组冻结小场景的基准/加严物理与数值对照均PASS；此前B06穿地问题已修复并完成全套受影响场景重验。C–E未进入，S3/V11未完成，详见[验收结果](results/s3-b-auto-repair-20260923-02/final-result.json)。

用户已授权小错误自动修复接续，不再逐轮要求确认。[执行补充](任务规划/B-自动修复执行补充.md)覆盖旧协议的逐次确认要求，冻结物理规格不变。已修正“执行预算不足也停止代码准备”及“修复后不足一整轮重验”的流程：最新包上限3轮×10场景、12项本地、累计执行45分钟，实际使用1轮/10场景/4本地/247.31秒即通过。有限预算、源码绑定与物理方案确认边界继续有效。

B历史累计本地25次通过、场景尝试26次、仿真启动22次/完成21次、CSV重处理1次、只读探测2次，产品在线0。旧[失败与诊断](results/s3-b-auto-repair-20260923-01/checkpoint.json)保留。公共接触改为参数化方程并新增生成C关系缓存检查，保持接触规律、参数和门槛；B02–B06完整10次重验通过，B01所需类/库未变而复用已有结果。最终源码与依赖见[manifest](results/s3-b-auto-repair-20260923-02/round-01/source-manifest.json)。

上下文按根AGENTS第7节管理；同一目标默认连续工作，不因B完成换窗。当前W13为GREEN，实际上下文占用未知。B已收尾，C需另行明确范围、方案与有限预算。

''' + old[end:]
    text = replace_once(text, '已完成B01–B05真实CSV验收；B06仿真未完成，不能验收。', '已完成B01–B06真实CSV与成对数值验收。')
    text = replace_once(text, '有限超时和首错停止；测试回执不能启动Modelica。', '有限超时和每批首错暂停；已授权的小错误经修复、重绑及必要验证后自动接续。构建后检查生成C关系缓存槽是否混用于不同标量条件，通过后才仿真；这不是完整编译器语义证明。测试回执不能启动Modelica。')
    anchor = text.index('```powershell', text.index('## S3-B'))
    text = text[:anchor] + '''六组已验证参数详见冻结协议第5节，全部为开发假设，未作参数范围扫描：

| 场景 | 已验证内容 | 基准/加严/数值 |
|---|---|---|
| B01 | 被动关节与固定传力，2kg杆长0.5m及1kg端体，小角释放 | 全部PASS，复用有效证据 |
| B02 | 轮对落地与静载 | 全部PASS |
| B03 | 驱动、滚动、制动与供电功率账本 | 全部PASS |
| B04 | 0.3m立面撞击、冲量及分离 | 全部PASS |
| B05 | 0.3m凸角径向撞击、独立几何及分离 | 全部PASS |
| B06 | 0.08m高台滚出、抛落及低台落地，初速2m/s | 全部PASS |

B02–B06每物理轮m=1kg、J=0.005kg·m²、R=0.1m、k=50000N/m、c=100N·s/m，左右轮对一次聚合；摩擦、初态和外力按各场景协议。B06加严最大压入4.621mm（限20mm）、能量闭合残差0.0003104J（限0.06002J），基准/加严落地时刻差0.000594ms（限0.2ms）。不据此证明多轮组合、20kg设备整车、20级楼梯或电池持续能力。

当前接触局部回归由 `modelica_equation_harness.py`读取实际方程，旧算法解释器用于冻结旧源码对照。此次4项定向检查与真实Modelica运行已通过，未宣称全测试集均已重跑。下列旧命令已执行，不能为刷新报告重跑；最终结果采用本页顶部证据。

### 历史执行与修复记录（以下状态仅对应当时批次）

''' + text[anchor:]
    text = text.replace('最新B06阻塞及自动包用量见本页顶部。', '最新B06修复结果及自动包用量见本页顶部。')
    text = replace_once(text, '## 既有证据与下一包', '## 既有证据与后续边界')
    start = text.index('A 之后只进入另行声明的 B：')
    text = text[:start] + 'A、B已完成；后续C两结构物理集成、D冻结后新输入、E产品接入均未进入。本次不启动整车参数调优、20级批次或能力扫描；后续工作需先明确范围、具体方案和有限预算。原窗可接续，不自动进入V12。\n'
    updates[path] = text
    path = ROOT/'任务规划/S3-启动.md'
    old = path.read_text(encoding='utf-8')
    text = '''# S3 启动：组件化建模（A、B完成，C–E未进入）

当前窗口：`V11-W13-S3-基础物理组件-20260922`。默认连续工作，不因工作包完成换窗；当前GREEN，实际上下文占用未知。

当前检查点：[B验收结果](../results/s3-b-auto-repair-20260923-02/final-result.json)。B01–B06基准/加严物理与数值全部PASS；最新公共接触方程修复后，B02–B06完整10次重验通过，B01依赖未变而复用有效结果。历史B06穿地失败及生成事件索引诊断保留于前一自动包，不能覆写。

最新执行规则见[B自动修复补充](B-自动修复执行补充.md)：小错误在有限轮次内自动修复，不逐错等“继续”；执行预算不阻塞代码准备，按完整受影响场景集预留额度。最新包上限3轮/30场景/12本地/累计执行45分钟，实际1轮/10场景/4本地/247.31秒完成；B累计本地25、场景26、仿真启动22/完成21、CSV重处理1、工具探测2、产品在线0。已收尾，剩余额度不授权C。

[B冻结协议](B-基础物理组件与验证协议.md)包含复用/缺口、版本、A映射及6组独立校核/容差；其最初待批准状态与逐错确认段落属于历史，最新用户授权由执行补充接续，原协议哈希保持。日常以[短状态](../V11-CURRENT-STATE.md)为准，历史看WORKLOG。

''' + old[old.index('## 新增需求与设计规则'):]
    start = text.index('B基础物理的准备审查已在W13完成：')
    end = text.index('\n\n不进入C', start)
    text = text[:start] + 'B已在W13完成实体/连杆、被动关节、含轮速车轮、共用离散台阶接触及驱动功率接口的冻结小场景验收。A仍无可执行物理后端；B成功不等于通用整车生成已实现。当前B无待修复阻塞，后续C须另行明确范围、具体方案和有限预算，本次不启动。' + text[end:]
    text = text.replace('下文A接口/预算保留为已执行包的设计依据，B–E尚未验收。', '下文A接口/预算保留为已执行包的设计依据；A、B已验收，C–E未进入。')
    text = text.replace('A已在W12完成；下一工作包为B，可在当前窗口明确范围/预算并在授权充分时接续。', 'A已在W12完成、B已在W13完成；后续工作包为C，须先明确范围/方案/预算。')
    text = text.replace('### 预算与失败退出提案', '### 历史预算与失败退出提案（B执行以最新补充为准）')
    updates[path] = text
    path = ROOT/'WORKLOG.md'
    text = path.read_text(encoding='utf-8')
    entry = '''## W13 修正自动修复流程，B六组小场景全部验收（2026-09-23）

- 用户进一步要求“改修不合理的地方，继续完成自动修复”。上一包余6次不足完整10次重验，把代码修复也停下不合理；根AGENTS与B执行补充已修正：准备/静态审查不受仿真余量阻塞，自动修复预算按完整受影响集合×有限轮次预留。新包从场景16/本地21接续，新增最多30场景/12本地/3轮、累计执行45分钟；旧未用额度退役不叠加，旧政策/检查点不覆写。
- 新根 `results/s3-b-auto-repair-20260923-02/`保存before原字节、政策、实际授权来源、源码/依赖/复用摘要及全局预占。冻结物理协议SHA仍为cc9875ad68b44aa1d614e54fa16226165d527b63f8b052de9ef2b0efb8f13de8，OMC/MSL及原工具链不变，在线产品调用0。
- 按约定通知并委派GPT-5.6 Sol / High、fork none处理公共接触方程化；子代理写出部分实现后因额度异常退出，未交付完整验收。主窗口已告知用户并接回代码，修正无效的0起始Modelica数组，完成方程局部解释器、定向回归和生成事件C缓存一致性门禁，未冒称子代理完成复核。
- 仅公共PolylineWheelContact从算法改为参数for-equation及无环前缀选择方程，面/内部角点、单边力、耗散、摩擦及特征选择保持冻结语义；有符号距离改SI.Position。其他Modelica类逐字不变。未硬编码三个面、改生成C、noEvent屏蔽、放松物理断言或调整工况。官方依据为Modelica3.6第8.3.2/8.5节，链接见执行补充。
- 本轮4项本地PASS：低台面旧失败状态应有正接触力及非负压入；凸/凹角、共线端点与离地对照旧源码；其他类/事件结构不变；生成C检查器拒绝旧缓存编号混用并接受独立编号。窄解释器不替代MSL/DAE积分；未跑任意全测试集。
- B02–B06基准/加严共10次加载/checkModel/构建、事件代码检查、真实仿真、独立物理均PASS，5组数值比较PASS，not_run为空。B01局部依赖闭包未变，沿用此前真实CSV物理和数值结果，无重跑。B六组冻结场景验收完成；S3/V11未完成。
- B06加严出现高台支撑→连续离地0.14580385s→低台落地，最大压入0.00462067m（限0.02m）、能量闭合最大残差0.000310359J（限0.0600192J）；基准/加严落地时刻差5.93967e-7s（限2e-4s）。生成C检查90次关系调用/44个不同缓存槽，无混用；此结构门禁不是完整编译器语义证明。旧穿地失败保留，不改记PASS。
- 最新包实际使用1/3轮、场景10/30、本地4/12、执行247.310846秒/2700秒。B累计场景26、本地25次全部通过、仿真启动22/完成21、CSV重处理1、只读工具探测2、产品在线请求/token/费用0。剩余额度不转给其他包。
- 收尾脚本s3_b_events_close仅核对既有源码/依赖、逐项报告与预算，保存accepted-evidence-manifest.json及final-result.json，未新增运行。结果PASS_S3_B_FROZEN_COMPONENT_FIXTURES；源码版本和实际内容绑定保留。短状态、README及S3入口已同步，当前无B修复阻塞。
- 未重跑A/R7，未进入C–E/整车调参/扫描/V12。A后端仍UNSUPPORTED，20kg整车/20级/产品CLI/MCP未验收；成果基于用户模型适配，原件及全部历史失败保留。上下文GREEN，实际占用未知；B完成不要求换窗。后续C先明确范围、具体方案与有限预算。

'''
    updates[path] = replace_once(text, '# V11 工作记录\n\n', '# V11 工作记录\n\n' + entry)
    for path, text in updates.items():
        path.write_text(text, encoding='utf-8')
        print(path.relative_to(ROOT))


if __name__ == '__main__':
    main()

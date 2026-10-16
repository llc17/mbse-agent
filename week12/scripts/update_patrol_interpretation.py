"""Record the user's continuous-patrol clarification without changing simulation evidence."""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'results/user-4w2n/original-20260922-01'
NOTE = '''## 最新用户更正：持续巡航不要求末端停车

用户明确小车要持续巡航，不要求每次上下楼后停稳。此前把“没有停车”作为全过程任务失败的理由不适用于该任务，撤回这一判定；旧运行数据与当时记录保留。后续连续巡航工况以完整通过入口、连续台阶和末轮离开出口后继续行驶为目标，不强制末端零速。

本次全过程模型在 150×280 mm、上/下各 20 级条件下完成一次上楼—平台—正向下楼；鼻轮于 32.45 s、后主轮于 34.05 s 越过下楼地形终点，之后持续行驶至 120 s，末速约 0.592 m/s。因此，本机原模型的一次上下楼并继续巡航运行成功。没有仿真重复上下楼循环、平台转向或完整巡检路线，不能称已验证循环巡航；缺少原始 MAT/CSV，原结果逐点一致性仍未验证。此更正仅改变任务解释，没有修改模型、补跑仿真或改写原始结果。

'''

def add_note(path):
    text = path.read_text(encoding='utf-8')
    heading, rest = text.split('\n', 1)
    path.write_text(heading + '\n\n' + NOTE + rest.lstrip('\n'), encoding='utf-8')

for path in [ROOT/'V11-CURRENT-STATE.md', ROOT/'WORKLOG.md', OUT/'README.md', OUT/'reproduction-summary.md']:
    add_note(path)

p = ROOT/'V11-CURRENT-STATE.md'
s = p.read_text(encoding='utf-8')
s = s.replace('全过程越过 20 级终点但无停车', '全过程完成一次上/下各20级并继续巡航（停车不是该任务的成功条件）')
s = s.replace('部分截图特征一致，出口/停车仍未完成', '部分截图特征一致，全过程一次上下楼并继续行驶成功，重复循环未验证')
s = s.replace('下楼出口、全过程停车、20 kg 载荷', '独立100级下楼出口、20 kg 载荷')
s = s.replace('当前阻塞是下楼没有达到原代码出口且全过程没有停车判据', '当前限制是独立下楼没有达到原代码的100级出口，且缺少原始逐点结果与重复循环证据')
p.write_text(s, encoding='utf-8')

p = OUT/'reproduction-summary.md'
s = p.read_text(encoding='utf-8').replace('60 s 后鼻轮仅到 15.9646 m', '60 s 后车身参考点 x=15.9646 m')
s = s.replace('因此“运行结束”不等于“完成上下楼并停稳”。', '这符合用户最新说明的持续行驶目标，不再以未停车判为失败。')
s = s.replace('下楼出口未完成；全过程无停车；', '独立100级下楼出口未完成；全过程一次上下楼并继续巡航成功；')
p.write_text(s, encoding='utf-8')

# Clarify current planning precedence while preserving historical acceptance/results.
clarification = '''## 2026-09-21 用户修订：持续巡航工况

用户最新明确不要求小车完成一次上下楼后停车，小车应继续巡航。本条优先于本文后续旧版“末端停稳/连续停稳时间窗”的巡航验收要求。仍要求完整入口、连续台阶、末轮离开出口及继续行驶；未停车不再作为持续巡航工况的失败理由。历史批次按当时协议的记录保留，不能倒改旧成绩。一次上下楼后继续平地行驶不等于重复循环巡检已经验证。

'''
for path in [ROOT/'任务规划/任务规划-V11.md', ROOT.parent/'任务规划/任务规划-V11.md', ROOT/'任务规划/S3-启动.md']:
    if path.is_file():
        s = path.read_text(encoding='utf-8')
        heading, rest = s.split('\n',1)
        path.write_text(heading+'\n\n'+clarification+rest.lstrip('\n'),encoding='utf-8')
print('Updated interpretation and current planning. No simulation evidence modified.')

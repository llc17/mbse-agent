"""Record actual user-approved package in living docs; no execution or approval creation."""
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
receipt=ROOT/'results/s3-c-passive-bogie-authorization-20261006.json'
r=json.loads(receipt.read_text(encoding='utf-8'))
assert r['user_text']=='批准' and r['specification_digest']=='3048bd7ac34cb8814114743fc84b8636e6ece72b18d55033624763be1333d030'
p=ROOT/'V11-CURRENT-STATE.md';s=p.read_text(encoding='utf-8')
s=s.replace('**当前：S3-C被动摆臂单候选准备完成，等待具体参数＋有限验证预算的一次集中确认。** 用户“批准”已授权结构/联合质量布局准备；不再询问同一结构取舍。A/B完成，C/S3/V11未完成；没有启动新仿真。','**当前：S3-C被动摆臂具体方案及有限预算已获用户批准，正在完成隔离执行接入和预检。** [真实授权](results/s3-c-passive-bogie-authorization-20261006.json)绑定3048bd7a…3d030及18场景/9000秒预算；不再索要同一确认。A/B完成，C/S3/V11未完成。')
s=s.replace('**这是待确认预算，当前消耗0，不可启动。**','**本预算已获最新“批准”；消耗以新执行账本为准。**')
p.write_text(s,encoding='utf-8')
p=ROOT/'任务规划/S3-启动.md';s=p.read_text(encoding='utf-8')
s=s.replace('**当前：被动摆臂单候选静态准备完成，具体物理参数与执行预算尚待集中确认。** 用户最新“批准”是结构和联合质量布局准备授权；已完成这一准备，不再重复询问结构是否允许。','**当前：被动摆臂具体参数和18场景/9000秒执行包已获批准，正在隔离接入及预检。** 最新[真实授权](../results/s3-c-passive-bogie-authorization-20261006.json)绑定3048bd7a…3d030；不再询问同一参数、结构或预算。')
s=s.replace('下一步仅一次确认[完整参数]','用户已批准[完整参数]').replace('确认后覆盖接入','本次批准覆盖接入')
p.write_text(s,encoding='utf-8')
p=ROOT/'README.md';s=p.read_text(encoding='utf-8')
s=s.replace('具体参数/新执行预算待一次确认。','具体参数/新执行预算已获最新“批准”，见[授权](results/s3-c-passive-bogie-authorization-20261006.json)，正在隔离接入。')
s=s.replace('新参数与执行预算待确认。','新参数与执行预算已批准，消耗以新账本为准。')
p.write_text(s,encoding='utf-8')
p=ROOT/'WORKLOG.md';s=p.read_text(encoding='utf-8');h='# V11 工作记录\n\n'
entry='''## W13 被动摆臂具体参数与验证包获批，开始执行接入（2026-10-06）

- 用户最新“批准”紧接完整参数及18场景/3轮/9000执行秒预算请求，真实回执`results/s3-c-passive-bogie-authorization-20261006.json`绑定3048bd7ac34cb8814114743fc84b8636e6ece72b18d55033624763be1333d030与审查/合同SHA。此次是具体方案和执行批准，先前结构准备授权另保留；不修改冻结准备文件中的历史待批状态。
- 按根约定委派真实GPT-6.1 Sol/High两个独立工作包：隔离执行适配/CLI/12前检，与弹簧及铰接独立校核器。主窗口接回源审、预算和实际执行，子代理不启动任何测试/编译/仿真。原fixed-only后端/B/C2、历史失败和用户原件保持。
- 按探针两档→平地两档→三级上楼两档顺序，最多2轮小实现修复，物理/任务/数值失败停止受影响执行并只读定位。没有新下楼/20级/D/E/V12授权。Git HEAD ab8f0e3，跟踪差异空；未清理用户文件、未提交上传。

'''
assert s.startswith(h);p.write_text(h+entry+s[len(h):],encoding='utf-8')
print('Actual approval linked; four living documents updated. Frozen preparation remains unchanged.')

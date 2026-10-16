"""Small conservative task router; ambiguous input never becomes a source."""
import re


def task_intent(text, *, awaiting_facts=False):
    plain=text.strip().rstrip('。！!').strip()
    local=plain.rstrip('？?').strip()
    if plain in {'确认','批准','同意','CONFIRM'}:return 'confirm'
    if plain in {'拒绝','取消','不同意'}:return 'decline'
    if plain in {'退出','结束对话','exit','quit'}:return 'exit'
    if local in {'帮助','怎么使用','这里可以对话吗','这里能对话吗'}:return 'help'
    if local in {'状态','进度','结果','查看结果','现在是什么情况','现在什么情况','目前是什么情况','当前进度','现在进展如何'}:return 'status'
    lead=re.split(r'[，,；;。\n]',plain,maxsplit=1)[0]
    hypothetical=bool(re.match(r'^(?:如果|假如|假设|倘若)',lead))
    question=bool(re.search(r'[?？]\s*$|吗[。！？!?]*$|么[。！？!?]*$',text) or re.search(r'为什么|为何|如何|怎样|什么|能否|可否|是否|解释|说明一下|对话|交互',lead))
    change=bool(re.search(r'^(?:请)?(?:把|将|修改|更改|改为|改成|增加|删除|补充|要求|需求是|需求：|新需求)|(?:质量|载荷|速度|力|轮径|高度|深度|初速度|运行时间).*(?:改为|改成|改到|修改为)',plain))
    new=bool(re.search(r'^(?:请)?(?:帮我|我要|我想要|我需要|创建|建立|设计|建模|模拟|仿真|做一个|做一辆|验证)|(?:小车|模型|任务|需求).*[:：]',plain))
    if hypothetical:return 'consult'
    if (change or new) and question:
        # A complete request may end with a polite invitation to organize it.
        if new and re.search(r'请先整理(?:需求|方案).*可以吗',plain):return 'require'
        return 'clarify_intent'
    if change or new:return 'require'
    if question:return 'consult'
    if awaiting_facts:return 'require'
    # Numerical task facts are an ordinary supported requirement entry.
    if re.search(r'(?:质量|载荷|初速度|初始|静止起步|恒力|阻力|运行|末值|台阶|楼梯|秒|kg|N\b|m/s)',plain):return 'require'
    return 'clarify_intent'

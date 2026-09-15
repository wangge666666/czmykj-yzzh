"""Stable source-person/color contracts shared by short and long video workflows."""
import re

from workflow_core import WorkflowError
from video_proxy_rules import BARE_PROXY_BODY, REMOVE_OVERLAY_TEXT

PALETTE = (("red", "红", "#D92D20"), ("white", "白", "#FFFFFF"), ("yellow", "黄", "#FFD43B"), ("blue", "蓝", "#246BFD"))
COLOR_NAMES = {key: name for key, name, _ in PALETTE}
PAIR_RULE = "双人默认：一男一女时原片男性为红模、女性为白模；两人性别一致或无法可靠区分时，以首次两人清晰同框的画面左侧人物为红模、右侧为白模。"
STABILITY_RULE = "颜色只分配一次并绑定原片身份；交叉走位、转身、遮挡、出入画和切镜后保持原色，不按新位置重新分配，不互换动作、台词或服装。背景路人及未指定的人物保持原样。"


def normalize_plan(value):
    if not isinstance(value, list) or not 1 <= len(value) <= 4:
        raise WorkflowError("请选择 1–4 位需要处理的人物；每人分别绑定人物和服装。")
    result = []
    for i, item in enumerate(value):
        if not isinstance(item, dict):
            raise WorkflowError("人物分配格式无效。")
        color = item.get("color", "white" if len(value) == 1 else PALETTE[i][0])
        source = str(item.get("source", "")).strip()
        if color not in COLOR_NAMES or len(source) > 100 or "@" in source:
            raise WorkflowError("请选择有效颜色；原片人物描述最多 100 字，不含 @ 引用。")
        result.append({"id": f"p{i+1}", "color": color, "source": source})
    if len({r['color'] for r in result}) != len(result):
        raise WorkflowError("每位人物必须使用不同颜色。")
    return result


def source_anchor(role, count):
    if role.get("source"):
        return role['source']
    if count == 1:
        return "原片唯一主要人物"
    if count == 2:
        return "按双人默认规则确定的" + COLOR_NAMES[role['color']] + "模人物"
    # Saved masters can use an older palette. Their slot still identifies the
    # same source person even when the default order for new masters changes.
    slot = re.fullmatch(r'p([1-4])', str(role.get('id', '')))
    index = int(slot[1]) if slot else [key for key, _, _ in PALETTE].index(role['color']) + 1
    return f"首次可辨认的同框画面从左到右第 {index} 位主要人物（尚未入画者按首次出现顺序补位）"


def color_contract(plan, *, proxy=False):
    sequence = '、'.join(COLOR_NAMES[r['color']] for r in plan)
    rule = PAIR_RULE if len(plan) == 2 else f"多人默认按首次可辨认同框画面从左到右依次分配{sequence}；未同框者按首次出现顺序补位。"
    if len(plan) == 1:
        rule = "单人使用所选颜色。"
    stability = STABILITY_RULE.replace('不互换动作、台词或服装', '不互换动作或台词') if proxy else STABILITY_RULE
    return rule + "人工填写的原片人物描述优先于默认规则。" + "；".join(
        f"{r['id']}={COLOR_NAMES[r['color']]}模，对应{source_anchor(r,len(plan))}" for r in plan
    ) + "。" + stability


def color_model_prompt(plan, *, green=False):
    background = ("背景环境转换为纯绿色 #00FF00；绿色仅用于背景，不得用于人物。保留手持交互道具原样及原有遮挡边界。"
                  if green else "场景、背景、动态视差、道具、配角及非文字特效保持原样；不把背景变绿或变为静态图片。")
    return ("参考@视频1（全部人脸已打码），生成按人物分色的 CG 动作代理母版。打码不代表需要转换，只有下列指定人物转为颜色模型。"
            + BARE_PROXY_BODY + color_contract(plan, proxy=True) + "所有指定人物均为连续完整、无图案的哑光工业动画模型，头部是无五官的光滑椭圆体；"
            "不保留脸部、皮肤、头发、胡须、帽子、首饰或原服装外观；各自分配的颜色覆盖全身。"
            "保留头部转向与抽象说话节奏、手部关节、身体比例和每个人的动作时序。" + background +
            "逐帧复刻原时长、镜头顺序、裁切、焦段、景别、机位、运镜、屏幕占位、虚实、透视、接触点和遮挡。"
            "人物局部入画或被遮挡也保留原位置和尺度；不加人、不漏人、不合并身体、不重新构图、不居中或缩放人物。"
            "模型表面的光照、反射和阴影匹配场景；不新增文字、字幕、水印或色卡。" + REMOVE_OVERLAY_TEXT)


def validate_mentions(prompt, image_count):
    prompt = re.sub(r'@\s*(图片|视频)\s*(\d+)', lambda m: f'@{m[1]}{m[2]}', prompt)
    if '@' in re.sub(r'@(图片|视频)\d+', '', prompt):
        raise WorkflowError("提示词含失效引用，请使用已绑定的素材标签。")
    for kind, num in re.findall(r'@(图片|视频)(\d+)', prompt):
        if not 1 <= int(num) <= (1 if kind == '视频' else image_count):
            raise WorkflowError(f"@{kind}{num}没有对应素材。")
    return prompt


def long_color_plans(job, host):
    """Resolve colors once per stable source identity, never per new camera position.

    Gender is only taken from existing textual evidence, with left-first fallback.
    A multi-shot job must have reviewed continuity data before using this contract.
    """
    assignments=host.cast_continuity_assignment_map(job)
    people={int(r['character_id']): str(r.get('description','')) for r in job.cast_continuity.get('characters',[])}
    slots={}
    for shot in job.shots:
        count=max(host.long_shot_stable_detected_people_count(shot),int(shot.get('suggested_actor_count') or 0),
                  max((int(p.get('actor_slot') or 0) for p in (shot.get('performance') or {}).get('performance', [])),default=0),
                  max((int(p.get('slot') or 0) for p in shot.get('actor_mappings',[])),default=0))
        if count>4:
            raise WorkflowError('分色母版每镜最多 4 位人物。')
        for slot in range(1,count+1):
            key=(int(shot['index']),slot)
            if key not in assignments and len(job.shots)>1 and (count>1 or len(people)>1):
                raise WorkflowError('多人分色前请先完成跨镜人物分析，确认每镜角色槽位，避免颜色在切镜后换人。')
            character=int(assignments.get(key,{}).get('character_id') or slot)
            anchor=host.long_shot_performance_slot_anchor(shot,slot)
            people.setdefault(character,anchor)
            slots[key]=(character,anchor)
    if len(people)>4:
        raise WorkflowError('全片分色人物最多 4 位，请减少本次处理人物。')
    order=sorted(people,key=lambda c:min((k for k,(cid,_) in slots.items() if cid==c),default=(999999,c)))
    # Semantic P slots may be ordered by speaker rather than horizontal position.
    # Prefer the first shared frame's explicit left/right evidence over slot number.
    for shot in job.shots:
        together=[(slot,cid,anchor) for (index,slot),(cid,anchor) in slots.items() if index==int(shot['index'])]
        if len(together)==len(order) and len(order)>1:
            def horizontal(item):
                slot,_,anchor=item
                if re.search(r'左侧|左边|画面左|\bleft\b',anchor,re.I): return (0,slot)
                if re.search(r'右侧|右边|画面右|\bright\b',anchor,re.I): return (2,slot)
                return (1,slot)
            order=[cid for _,cid,_ in sorted(together,key=horizontal)]
            break
    if len(order)==2:
        def gender(text):
            male=bool(re.search(r'男性|男人|男子|男孩|男生|男士|\bman\b|\bmale\b',text,re.I))
            female=bool(re.search(r'女性|女人|女子|女孩|女生|女士|\bwoman\b|\bfemale\b',text,re.I))
            return 'male' if male and not female else 'female' if female and not male else ''
        if {gender(people[c]) for c in order}=={'male','female'}:
            order.sort(key=lambda c:gender(people[c])!='male')
    palette={c:('white' if len(order)==1 else PALETTE[i][0]) for i,c in enumerate(order)}
    return {int(shot['index']):[{'id':f'p{slot}','color':palette[cid],'source':f'表演槽位P{slot}，原片稳定身份C{cid}，{anchor}'[:100]}
                              for (index,slot),(cid,anchor) in slots.items() if index==int(shot['index'])]
            for shot in job.shots}

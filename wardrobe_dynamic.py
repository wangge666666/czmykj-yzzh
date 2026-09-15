"""Selective foreground replacement for wardrobe's moving-camera mode."""
from __future__ import annotations

import re

from workflow_core import WorkflowError
from video_proxy_rules import BARE_PROXY_BODY, REMOVE_OVERLAY_TEXT, PROXY_DIALOGUE, FINAL_DIALOGUE


DYNAMIC_WHITE_PROMPT = (
    "参考@视频1，将整段视频转换为仅指定主要人物为纯白CG白模、其余元素保留原片的动作代理视频。"
    + BARE_PROXY_BODY +
    "原视频中检测到的人脸均已统一打码，打码不是主角标记，不得把所有打码人物都转为白模。"
    "根据原片的叙事主体、持续动作和人物关系识别核心主要人物，只将主要人物转为白模；"
    "次要人物、群演、背景路人保持参考视频中的可见外观、服装和动作，不转换为白模。"
    "主角头部为无五官、无身份特征的光滑椭圆体，不保留原脸部特征、头发、发型、胡须、帽子、眼镜、首饰、服装或穿戴物；"
    "头部动作通过整体转向和俯仰表达，下颌只用轻微抽象几何形变对应说话节奏，不生成可辨认的脸。"
    "主角全身采用连续完整、无图案、无身份特征的哑光白色工业动画模型外观；结构简洁，手部、关节和身体轮廓稳定清楚，"
    "不得生成写实人体表面细节。白模的光影明暗、环境反光和投影阴影匹配原场景光源，自然融入画面。"
    "逐帧保留主角的屏幕占位、尺度、姿态、动作、前后景和遮挡；保留所有次要人物、场景环境、道具陈设和装饰，"
    "不简化、不换形、不删除；交互道具保持原外观与质感，不转为白色几何模型。"
    "背景完整保留@视频1的动态画面、透视、视差、色调和明暗，不替换为绿幕或静态场景图。"
    "严格保持总时长、剪辑节奏、转场效果、单镜时长、机位、焦段、景别、裁切、构图、对焦与运镜轨迹。"
    "保留原片的非文字特效，不新增人物。"
    "禁止白模漂移、人物换位、肢体增减、闪烁、融化、重影及边缘合成痕迹。" + REMOVE_OVERLAY_TEXT
)

DYNAMIC_FINAL_PROMPT = (
    "参考@视频1、@图片1和@图片2完成动态场景更换人物。最高优先级白模母版规则：@视频1是主要人物屏幕占位、前后景、"
    "局部入画、虚实、遮挡、动作时序、机位、焦段、景别、裁切、透视、构图、对焦和运镜的唯一逐帧依据。"
    "逐帧只将@视频1中指定的白模人体替换为@图片1的角色；即使只露出模糊头肩、背影或身体局部，也必须按原位置、"
    "大小、虚实和遮挡保留，绝不能删除。次要人物、群演和背景路人保持@视频1原貌，不替换、不重绘其身份或服装。"
    "禁止重新构图、自动居中、人物缩放、推近拉远或改变运镜；逐帧保持白模边缘位置、人物尺度与背景视差。"
    "@图片1是火山角色库人物参考，只提供身份、五官、发型和体型；@图片2是唯一服装、鞋履和配饰依据。"
    "不得继承图片参考的姿势、排版、背景、光线、机位、景别或构图。"
    "场景、背景、道具、光照、反射、投影、动态视差与非文字特效全部以@视频1为准，不使用场景图片，不重建或静态替换背景。"
    "保持所有交互道具的原有外观、材质、位置和遮挡；新人物自然融入原有光影。"
    "逐帧复刻白模人物的动作、表情节奏、视线、站位、尺度、遮挡、互动和动作时点；完整保留镜头顺序、切点与转场。"
    "跨镜保持新人物身份、五官、发型、体型和服装一致；禁止串脸、换位、漏人、加人、服装互换、场景漂移或重影。"
    "彻底替换主角的白模材质、光头和无身份脸部，不把白模外观带入成片。输出正常彩色视频，不出现参考图拼贴、"
    "分屏、素材板边框或人物库标记。原片非文字特效与场景内自然文字保留原样；"
    "不得新增台词字幕、翻译字幕、标题、姓名条、说明文字、角标或水印。" + REMOVE_OVERLAY_TEXT
)


DYNAMIC_OBJECT_WHITE_PROMPT = (
    "参考@视频1，仅将指定的目标物品转换为无图案、无标识的哑光白色CG物品白模。"
    "目标由补充的原片位置、外观与交互描述确定，不替换其他同类物品。保留目标的原有几何轮廓、尺寸、"
    "部件连接、形变、转动、运动轨迹和出入画时点；物品不是人体，不得转换为人形白模。"
    "逐帧保持手指握持位置、接触点、支撑关系、前后遮挡、运动模糊、反射和落地阴影。"
    "人物身份、脸部、头发、服装、表演、手部和非目标物品完整保留原片；场景、灯光和动态背景保持原样。"
    "不移除遮挡目标的手指，不补出本来不可见的目标部分；目标离开画面时不得凭空出现。"
    "原片是时长、动作、切点、机位、构图、焦段、景深、运镜、透视和视差的唯一依据。"
    "保留原片非文字特效，不新增字幕、标识、水印、拼贴或分屏；禁止物品漂移、穿模、复制和闪烁。" + REMOVE_OVERLAY_TEXT
)
DYNAMIC_OBJECT_FINAL_PROMPT = (
    "参考@视频1与@图片1，只将指定物品的白模替换为@图片1中的新物品。"
    "@视频1是逐帧动作、占位、尺度、旋转、形变、接触点、遮挡、虚实、剪辑、机位、焦段、构图和运镜的唯一依据。"
    "@图片1只提供新物品的形状细节、材质、色彩和表面设计，不继承图片的背景、摆放姿势、机位或光线。"
    "新物品以白模的占位和接触关系为约束适配尺寸，不得通过修改人物手势或运镜来迁就参考图。"
    "保持手指在物品前后的层次、握持、支撑、碰撞和互动时点，局部入画和遮挡部分按原样保留。"
    "跨镜保持同一物品，不串换其他物品，不复制、不漂浮、不穿模；匹配原场景的光影、反射、阴影和运动模糊。"
    "人物身份、脸部、发型、服装和动作，非目标物品及所有背景均保持@视频1原貌。"
    "彻底替换目标白模，输出正常彩色视频；保留原片非文字特效，不新增字幕、水印、分屏或参考图边框。" + REMOVE_OVERLAY_TEXT
)
DYNAMIC_SCENE_WHITE_PROMPT = (
    "参考@视频1（已将检测到的人脸全部打码的原片），生成绿底白模视频，用于后续还原原片人物并更换背景场景。"
    + BARE_PROXY_BODY +
    "仅将核心主要人物转换为无身份的纯白CG动画绑定模型。头部为无五官的光滑椭圆体，"
    "不保留脸部、头发、胡须、帽子、眼镜或首饰；全身为连续完整、无图案的哑光白色模型，不保留原服装外观。"
    "手部、关节、身体轮廓稳定，头部转向、俯仰和抽象说话节奏严格跟随原片。"
    "将原片场景环境、建筑、固定陈设和地面替换为均匀纯绿色背景（#00FF00），无纹理、无渐变、无绿幕褶皱；"
    "不要生成场景空间白模，不把人物染成绿色。手持及正在交互的可移动道具保留可见外观及运动，"
    "次要人物保留参考视频中的可见外观和动作，不新增、删除或改成另一人物。"
    "原片是逐帧动作、人物边缘位置、占位、尺度、站位、接触点、前后层次、局部入画和遮挡的唯一依据。"
    "原环境遮挡主体的区域保持原可见边界，不补出原本不可见的身体；记录为绿色遮挡区域供后续场景重建。"
    "严格保持原片机位、焦段、景别、裁切、透视、构图、对焦、运镜、运动模糊、总时长、切点和转场。"
    "禁止自动居中、人物缩放、固定机位、姿态改写、漂移、闪烁、重影和新增文字、水印。" + REMOVE_OVERLAY_TEXT
)
DYNAMIC_SCENE_FINAL_PROMPT = (
    "参考@视频1与@图片1，只将场景空间白模重绘为@图片1提供的新环境外观。"
    "@视频1是空间几何、机位轨迹、焦段、视角、透视、构图、地平线、景深、遮挡和动态视差的唯一依据。"
    "@图片1只提供环境风格、材质、色彩、建筑装饰与照明氛围；不得继承参考图的拍摄机位或平面构图，"
    "不得把场景图作为静态贴图粘在人物后方。沿白模空间结构重建可随镜头运动的连续场景，"
    "背景前后层次、近景掠过和被遮挡区域必须遵循原白模，禁止场景滑动、地面漂移、拉伸和边缘闪烁。"
    "人物身份、脸部、发型、服装、身体比例、动作、走位、尺度以及手持和交互物品保持@视频1原貌；"
    "只允许为融入环境匹配人物表面的光照与环境反光，不更换人物或服装，不改变肢体和姿势。"
    "保持脚部接触面、支撑、投影和遮挡关系，禁止悬浮、穿地或切换物品。"
    "完整复刻原时长、剪辑、转场和动作时序，输出正常彩色视频，不残留场景白模。"
    "保留原片非文字特效，不新增字幕、水印、参考图拼贴、分屏或素材板。" + REMOVE_OVERLAY_TEXT
)

DYNAMIC_WHITE_PROMPT += PROXY_DIALOGUE
DYNAMIC_SCENE_WHITE_PROMPT += PROXY_DIALOGUE
DYNAMIC_FINAL_PROMPT += FINAL_DIALOGUE

DYNAMIC_EDIT_PROFILES = {
    "dynamic": {"label": "动态场景更换人物", "white_prompt": DYNAMIC_WHITE_PROMPT, "final_prompt": DYNAMIC_FINAL_PROMPT},
    "dynamic_object": {"label": "动态物品替换", "target_label": "原片中要替换的物品", "target_default": "", "image_label": "物品参考图", "white_prompt": DYNAMIC_WHITE_PROMPT, "final_prompt": "根据人物、服装参考是否齐全，选择主角白模或原片视频入库流程。"},
    "dynamic_scene": {"label": "动态背景替换", "workflow_version": 2, "image_label": "场景参考图", "white_prompt": DYNAMIC_SCENE_WHITE_PROMPT, "final_prompt": "无人物服装参考时直接引用原片；有参考时使用绿底白模、人物、服装和场景图。"},
}
DYNAMIC_EDIT_MODES = set(DYNAMIC_EDIT_PROFILES)
# Retained for older helper imports. Current object/background workflows both
# mask faces before creating their character proxy; neither whites the environment.
DYNAMIC_ENVIRONMENT_MODES = set()


def scene_final_prompt(workflow: str, custom: str, has_scene_image: bool) -> str:
    if workflow not in {"references", "library"}:
        raise WorkflowError("请选择原片视频入库或有人物服装参考流程。")
    custom = re.sub(r"@\s*(图片|视频)\s*(\d+)", lambda m: f"@{m[1]}{m[2]}", str(custom).strip())
    if not custom or len(custom) > 1000:
        raise WorkflowError("请用 1–1000 字说明需要更换的背景场景。")
    if workflow == "references" and not has_scene_image:
        raise WorkflowError("绿底白模流程需要上传场景参考图，对应 @图片3。")
    image_count = (2 if workflow == "references" else 0) + int(has_scene_image)
    allowed = {"@视频1"} | {f"@图片{i}" for i in range(1, image_count + 1)}
    if "@" in re.sub(r"@(图片|视频)\d+", "", custom) or any(token not in allowed for token in re.findall(r"@(?:图片|视频)\d+", custom)):
        raise WorkflowError("提示词引用了未提交的素材，请使用当前显示的 @ 素材标签。")
    if workflow == "references":
        base = ("@视频1是绿底白模动作母版。逐帧将白模主角还原为@图片1提供的原片人物身份、五官、发型和体型，"
                "穿着@图片2提供的原片服装、鞋履和配饰；将绿色背景及绿色遮挡区域重建为@图片3提供的新场景。"
                "人物与服装图只提供身份和穿着，不继承图片的姿势、背景、光线、机位、构图或排版。"
                "彻底去除绿底、绿色溢色、白模材质及无身份头部；跨镜保持同一人物与服装。")
    else:
        base = ("@视频1是角色库中直接引用的原片视频。只更换下方指定的背景场景，"
                "保留原片人物身份、五官、发型、体型、服装、表情、视线、动作、站位和手持道具。"
                "不重建人物身份，不把人物或环境转换为白模；仅调整必要的环境反光及接触阴影使人物融入新场景。")
    base += ("@视频1是逐帧人物占位、边缘位置、尺度、前后景、虚实、局部入画、动作时点、接触与遮挡、"
             "机位、焦段、景别、裁切、透视、构图、对焦和运镜的唯一依据。"
             "禁止自动居中、人物缩放、重新构图或改变镜头轨迹。新场景需要随原镜头运动形成合理的透视与动态视差，"
             "不得把场景图作为静态贴图放在人物后方。保持脚部落点、支撑面和前景遮挡层次，"
             "保留人物手持及交互道具的外观、握持、运动和出入画时点；光照、反射、景深、阴影和运动模糊与新场景一致。"
             "保持原时长、剪辑、转场及动作节奏，不漏人、不加人、不串脸、不换衣、不漂浮、不穿模、不闪烁。"
             "输出正常彩色成片，不残留绿底、白模、拼贴、分屏或参考图边框，不新增字幕、说明文字或水印。")
    if has_scene_image:
        base += f"@图片{image_count}只提供场景环境外观、材质、色彩与光照氛围，不改变人物姿势、机位、景别、构图或裁切。"
    return base + REMOVE_OVERLAY_TEXT + FINAL_DIALOGUE + "\n背景替换要求：" + custom


def object_final_prompt(workflow: str, custom: str, has_object_image: bool) -> str:
    if workflow not in {"references", "library"}:
        raise WorkflowError("请选择有人物服装参考或原片视频入库流程。")
    custom = re.sub(r"@\s*(图片|视频)\s*(\d+)", lambda m: f"@{m[1]}{m[2]}", str(custom).strip())
    if not custom or len(custom) > 1000:
        raise WorkflowError("请用 1–1000 字说明原片中的哪个物品要换成什么。")
    image_count = (2 if workflow == "references" else 0) + int(has_object_image)
    allowed = {"@视频1"} | {f"@图片{i}" for i in range(1, image_count + 1)}
    if "@" in re.sub(r"@(图片|视频)\d+", "", custom) or any(token not in allowed for token in re.findall(r"@(?:图片|视频)\d+", custom)):
        raise WorkflowError("提示词引用了未提交的素材，请使用当前显示的 @ 素材标签。")
    if workflow == "references":
        base = ("@视频1是仅主要人物为白模的视频；先逐帧将白模主角还原为@图片1提供的原片人物身份、五官、发型和体型，"
                "并穿着@图片2提供的原片服装、鞋履和配饰。图片只提供人物和服装外观，不继承图片的姿势、背景、光线、机位或排版。"
                "彻底去除主角白模材质和无身份头部，跨镜保持同一人物与服装。")
    else:
        base = ("@视频1是角色库中直接引用的原片视频，完整保留原片人物身份、五官、发型、服装、表情与动作；"
                "无需重建人物，不将人物或物品转换为白模。")
    base += ("只根据下方要求替换指定物品，其他同类物品、人物和场景保持母版。"
             "@视频1是逐帧动作、屏幕占位、尺度、前后景、虚实、局部入画、遮挡、交互时点、机位、焦段、景别、裁切、透视、构图、对焦和运镜的唯一依据。"
             "严格保留手指握持、接触点、支撑关系和遮挡层次；新物品匹配原运动轨迹、尺寸约束、光照、反射、阴影和运动模糊，"
             "不得改变人物手势或运镜来迁就参考图。禁止自动居中、重新构图、人物缩放、物品漂移、穿模、复制、闪烁或背景漂移。"
             "保持总时长、剪辑、转场与节奏，输出正常彩色成片，不出现拼贴、分屏、素材板或白模残留。保留原片非文字特效，不新增字幕、文字或水印。")
    if has_object_image:
        base += f"@图片{image_count}只提供替换后物品的外观、结构、材质、色彩与设计细节，不继承背景、姿势或机位。"
    return base + REMOVE_OVERLAY_TEXT + FINAL_DIALOGUE + "\n物品替换要求：" + custom


def dynamic_target_description(mode: str, description: str) -> str:
    value = " ".join(str(description).split())
    if mode in DYNAMIC_ENVIRONMENT_MODES and not value:
        raise WorkflowError("请说明原片中要替换的物品或场景区域。")
    if len(value) > 160 or "@" in value:
        raise WorkflowError("替换目标描述最多 160 字，不要在描述里插入素材引用。")
    return value


def dynamic_prompt(base: str, description: str = "", custom: str = "", *, white: bool = False, mode: str = "dynamic") -> str:
    if mode not in DYNAMIC_EDIT_MODES:
        raise WorkflowError("无法识别动态替换模式。")
    description = dynamic_target_description(mode, description)
    description = " ".join(str(description).split())
    if len(description) > 160 or "@" in description:
        raise WorkflowError("主角补充描述最多 160 字，不要在描述里插入素材引用。")
    custom = str(custom).strip()
    prompt = base
    if description:
        prompt += (f"\n指定主角：{description}。主角身份跨镜保持不变。" if mode == "dynamic"
                   else f"\n指定替换目标：{description}。仅编辑该目标，其余区域保持母版。")
    if custom and custom != base:
        if len(custom) > 500:
            raise WorkflowError("补充提示词最多 500 字；固定规则由系统自动附加。")
        prompt += f"\n补充要求：{custom}"
    allowed = {"@视频1"} if white else ({"@视频1", "@图片1", "@图片2"} if mode == "dynamic" else {"@视频1", "@图片1"})
    prompt = re.sub(r"@\s*(图片|视频)\s*(\d+)", lambda m: f"@{m[1]}{m[2]}", prompt)
    normalized_custom = re.sub(r"@\s*(图片|视频)\s*(\d+)", lambda m: f"@{m[1]}{m[2]}", custom)
    if "@" in re.sub(r"@(图片|视频)\d+", "", normalized_custom):
        raise WorkflowError("只能引用当前步骤已绑定的素材标签。")
    for token in re.findall(r"@(图片|视频)(\d+)", prompt):
        if "@" + "".join(token) not in allowed:
            raise WorkflowError("提示词引用了未提交的素材，请使用当前步骤的素材标签。")
    if len(prompt) > 1850:
        raise WorkflowError("提示词过长，请精简补充要求。")
    return prompt

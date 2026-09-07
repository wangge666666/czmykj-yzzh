"""One explicit consent for one supported, immutable workbench POST.

This is not an account-wide permission. The bridge binds the plan to its own
operation ID, account, session, settings revision and private worker. Every
outgoing request still gets a frozen body/hash and its own send-once journal.
"""
import re


def workflow_plan(path, method, mode):
    if method != "POST" or mode != "platform":
        return None
    actions = {
        "/api/wardrobe-swap/white-model": ("生成白膜视频", ["video.create"]),
        "/api/wardrobe-swap/extract-references": ("提取人物、服装与场景", ["image.generate"]),
        "/api/wardrobe-swap/extract-person": ("提取人物参考图", ["image.generate"]),
        "/api/wardrobe-swap/prepare": ("准备白膜与参考素材", ["video.create", "image.generate"]),
        "/api/wardrobe-swap/generate": ("生成衣装智换成片", ["video.create"]),
        "/api/wardrobe-swap/extracted-person/character-library": ("将本次虚拟人物加入角色库", ["assets.CreateAssetGroup", "assets.CreateAsset"]),
        "/api/character-library/assets": ("上传本次虚拟人物", ["assets.CreateAsset"]),
        "/api/real-long-video/character-library/assets": ("上传本次虚拟人物", ["assets.CreateAsset"]),
    }
    for project, label in (("long-video", "虚拟人物"), ("real-long-video", "写实虚拟人像")):
        for step, title, commands in (
            ("performance", "分析本次分镜表演", ["analysis.create"]),
            ("white-model", "生成本次分镜白膜", ["video.create", "analysis.create"]),
            ("generate", "生成本次选定成片", ["video.create", "analysis.create", "image.generate"]),
        ):
            actions[f"/api/{project}/{step}"] = (label + " · " + title, commands)
    actions["/api/real-long-video/prepare-actors"] = ("将本次所选虚拟人像加入角色库", ["assets.CreateAsset"])
    if re.fullmatch(r"/api/real-long-video/actors/[0-9]+/character-asset", path):
        selected = ("将本次虚拟人像加入角色库", ["assets.CreateAsset"])
    else:
        selected = actions.get(path)
    if not selected:
        return None
    title, commands = selected
    paid = any(command in commands for command in ("video.create", "image.generate", "analysis.create"))
    labels = {"video.create": "视频生成", "image.generate": "图片生成", "analysis.create": "内容分析",
              "assets.CreateAssetGroup": "创建所需角色分组", "assets.CreateAsset": "虚拟人物入库与审核"}
    return {
        "version": 1, "title": title, "commands": ["media.upload", *commands],
        "steps": ["上传本次所选素材及必要的中间参考素材", *[labels[command] for command in commands]],
        "paid": paid, "asset_consent": "assets.CreateAsset" in commands,
        "cost": ("按本次页面选定的素材、分镜和生成参数执行，可能包含多次模型调用；费用按平台价格和实际用量从米哟账户扣除。"
                 if paid else "本次包含素材上传与角色库操作，不创建付费视频或图片生成任务。"),
    }

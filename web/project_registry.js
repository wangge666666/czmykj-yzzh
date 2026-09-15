(function () {
  const categories = [
    { id: "partial", number: "01", title: "局部替换与剧情改写", subtitle: "更换人物、服装、物品和场景，或按秒改写指定片段", projects: ["wardrobe"] },
    { id: "long", number: "02", title: "长视频重绘", subtitle: "分镜、表演、白膜与连续成片，可承担单人或多人完整复刻", projects: ["long-video", "real-long-video"] },
    { id: "motion", number: "03", title: "动作迁移", subtitle: "复用原片动作，用新人物或首尾画面参考制作新片", projects: ["motion-transfer"] },
  ];

  const projects = {
    "motion-transfer": {
      number: "04", category: "motion", path: "/projects/motion-transfer", title: "人物动作迁移", ownShell: true,
      summary: "原片打码并生成白膜后，用场景、多位角色库人物与 @图片 提示词制作新片；也可使用首尾画面参考与已有白膜。",
      steps: [{id:"prepare",label:"准备动作白膜"},{id:"studio",label:"视频制作"},{id:"results",label:"预览与下载"}],
      paidActions: {},
    },
    wardrobe: {
      number: "01", category: "partial", path: "/projects/wardrobe", title: "衣装智换",
      summary: "在同一工作台中更换人物、服装与场景；运动镜头支持人物、物品和场景分别替换。也可按原片秒数改写开头、中间或结尾的多个区间，其余部分保留。",
      ownShell: true,
      steps: [
        { id: "mode", label: "选择功能", hint: "人物 / 场景 / 服装" },
        { id: "source", label: "上传原片", hint: "替换流程最长 15 秒" },
        { id: "prepare", label: "原片与白模", hint: "Seedance 2.0 · 480p" },
        { id: "references", label: "人物与素材", hint: "角色库与参考图" },
        { id: "generate", label: "生成成片", hint: "Seedance 2.5" },
        { id: "result", label: "预览下载", hint: "打码 / 白膜 / 成片" },
      ],
      paidActions: {
        prepareBtn: { calls: "1 次 Seedance 2.5 + 按缺失素材调用 Seedream", kind: "打码、白膜与素材准备", fixedOutput: "白膜 480p" },
        generateBtn: { calls: "预计 1 次 Seedance 2.5", kind: "局部替换成片" },
      },
    },
    person: {
      number: "01", category: "partial", path: "/projects/person", title: "只更换人物",
      summary: "保留原片场景，提取干净场景后只替换人物身份与对应服装。",
      form: "#personWorkflowForm",
      steps: [
        { id: "source", label: "上传原片", hint: "选择待处理视频", selectors: ["#source"] },
        { id: "preprocess", label: "提取原场景", hint: "深度与干净场景", selectors: ["#prepare"] },
        { id: "references", label: "新人物与服装", hint: "替换素材", selectors: ["#replacement"] },
        { id: "generate", label: "生成设置", hint: "Seedream 与 Seedance", selectors: ["#generate"] },
        { id: "progress", label: "进度与结果", hint: "素材与成片预览", selectors: [".task-panel"], outsideForm: true },
      ],
      paidActions: {
        prepareBtn: { calls: "预计 1 次", kind: "提取干净场景", fixedOutput: "Seedream 5.0" },
        generateBtn: { calls: "预计 1 次 Seedance", kind: "人物替换成片" },
        fullBtn: { calls: "预计 1 次 Seedream + 1 次 Seedance", kind: "完整流程" },
      },
    },
    scene: {
      number: "02", category: "partial", path: "/projects/scene", title: "只更换场景",
      summary: "保留原片人物与服装，提取人物、服装参考后只替换背景场景。",
      form: "#sceneWorkflowForm",
      steps: [
        { id: "source", label: "上传原片", hint: "选择待处理视频", selectors: ["#source"] },
        { id: "preprocess", label: "提取人物服装", hint: "深度与三视图", selectors: ["#extract"] },
        { id: "references", label: "新场景", hint: "上传替换场景", selectors: ["#target"] },
        { id: "generate", label: "生成设置", hint: "Seedream 与 Seedance", selectors: ["#generate"] },
        { id: "progress", label: "进度与结果", hint: "素材与成片预览", selectors: [".task-panel"], outsideForm: true },
      ],
      paidActions: {
        prepareBtn: { calls: "预计 2 次", kind: "人物与服装提取", fixedOutput: "Seedream 5.0" },
        generateBtn: { calls: "预计 1 次 Seedance", kind: "场景替换成片" },
        fullBtn: { calls: "预计 2 次 Seedream + 1 次 Seedance", kind: "完整流程" },
      },
    },
    clothing: {
      number: "03", category: "partial", path: "/projects/clothing", title: "只更换服装",
      summary: "保留人物身份与原片场景，提取参考后只替换指定服装。",
      form: "#clothingWorkflowForm",
      steps: [
        { id: "source", label: "上传原片", hint: "原视频与新服装", selectors: ["#source"] },
        { id: "preprocess", label: "提取人物场景", hint: "人物板与干净场景", selectors: ["#prepare"] },
        { id: "references", label: "新服装", hint: "唯一服装依据", selectors: ["#replacement"] },
        { id: "generate", label: "生成设置", hint: "Seedream 与 Seedance", selectors: ["#generate"] },
        { id: "progress", label: "进度与结果", hint: "素材与成片预览", selectors: [".task-panel"], outsideForm: true },
      ],
      paidActions: {
        prepareBtn: { calls: "预计 2 次", kind: "人物与场景提取", fixedOutput: "Seedream 5.0" },
        generateBtn: { calls: "预计 1 次 Seedance", kind: "服装替换成片" },
        fullBtn: { calls: "预计 2 次 Seedream + 1 次 Seedance", kind: "完整流程" },
      },
    },
    "long-video": {
      number: "04", category: "long", path: "/projects/long-video", title: "虚拟人物复刻重绘",
      summary: "面向3D、2D和其他虚拟人物素材，按分镜完成打码、表演分析、白模与最终重绘。",
      form: "#longWorkflowForm",
      steps: [
        { id: "source", label: "上传原片", hint: "长视频与切点", selectors: ["#source"] },
        { id: "shots", label: "分镜分析", hint: "拆镜与人数估算", selectors: ["#shots"] },
        { id: "preprocess", label: "白模准备", hint: "打码、表演与白模", selectors: ["#preprocess"] },
        { id: "references", label: "人物与场景", hint: "角色、服装与场景库", selectors: ["#references"] },
        { id: "generate", label: "生成设置", hint: "模型、风格与映射", selectors: ["#generate"] },
        { id: "progress", label: "进度与结果", hint: "逐镜对比与下载", selectors: [".task-panel"], outsideForm: true },
      ],
      paidActions: {
        performanceBtn: { calls: "预计 1 次方舟文本分析", kind: "台词与表演分析", usePageSettings: false },
        whiteModelBtn: { calls: "预计每个有效分镜 1 次", kind: "白膜视频", fixedOutput: "Seedance 2.0 · 480p" },
        regenerateAllBtn: { calls: "预计每个有效分镜 1 次 Seedance", kind: "重新生成全部成片" },
        generateBtn: { calls: "逐镜模式=每镜 1 次 Seedance", kind: "生成并合并成片" },
      },
    },
    "real-long-video": {
      number: "05", category: "long", path: "/projects/real-long-video", title: "真实人物复刻重绘",
      summary: "使用已授权并处于Active状态的火山角色Asset锁定真人身份，结合服装、场景和白模完成重绘。",
      identityFlow: "火山角色库 Active Asset",
      ownShell: true,
      steps: [
        { id: "source", label: "上传原片", hint: "长视频与切点" },
        { id: "shots", label: "分镜分析", hint: "拆镜与人数估算" },
        { id: "preprocess", label: "白模准备", hint: "打码、表演与白模" },
        { id: "references", label: "人物与场景", hint: "火山角色Asset与场景库" },
        { id: "generate", label: "生成设置", hint: "逐镜或整段生成" },
        { id: "progress", label: "进度与结果", hint: "逐镜对比与下载" },
        { id: "archives", label: "缓存与存档", hint: "保存和恢复" },
      ],
      paidActions: {
        performanceBtn: { calls: "预计 1 次方舟文本分析", kind: "台词与表演分析", usePageSettings: false },
        prepareRealActorsBtn: { calls: "按未绑定人物计费", kind: "上传并绑定火山角色 Asset", usePageSettings: false },
        whiteModelBtn: { calls: "预计每个有效分镜 1 次", kind: "白膜视频", fixedOutput: "Seedance 2.0 · 480p" },
        regenerateAllBtn: { calls: "预计每个有效分镜 1 次 Seedance", kind: "重新生成全部成片" },
        generateBtn: { calls: "逐镜=每镜 1 次；整段白膜=1 次 Seedance 2.5", kind: "生成并合并成片" },
      },
    },
  };

  window.DepthFlowRegistry = { version: "2026-08-23.2", categories, projects };
})();

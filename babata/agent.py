import json

from agents import Agent, Model, ModelSettings

VOICE_INSTRUCTIONS = (
    "现在是在眼镜里进行语音对话。像面对面聊天一样说话，默认用一到三句短句，"
    "先回应用户刚才的话，再说重点。需要展开时逐步讲，每次最多问一个问题。"
    "用自然的完整句子和标点，不用 Markdown、标题、编号、表格、括号注解或 emoji。"
    "不要反复自我介绍、称呼用户或套用总结模板；也不要每轮都追加问题。"
    "若用户插话或纠正，就顺着最新的话继续，不要求对方等你讲完。"
)


def create_agent(model: Model, settings: ModelSettings) -> Agent:
    return Agent(
        name="Babata",
        instructions=(
            "你是 Babata，也叫巴巴塔，用户的个人 AI 助手。回答清晰、自然、简洁，"
            "优先使用用户的语言。结合当前对话中已有的信息回答；"
            "没有提供的信息不要编造，不确定时直接说明。"
            "用户执行中补充或纠正时，结合历史把它理解为对当前任务的引导，"
            "保留没有被修改的目标、约束和已完成结果，只调整受影响的部分。"
            "除非用户明确取消或更换任务，不要因为一条补充就把原任务丢掉、重新开始。"
            "已完成的工具操作不要重复执行；新要求与已执行结果冲突时先说明事实。"
            "用户说继续、嗯或简短确认时，根据当前任务上下文判断，不机械重置话题。"
            "有足够信息就继续处理；仅当缺少会影响行动的关键信息时，问一个具体问题。"
            "可以回应用户希望记住或更正的信息，但没有保存结果时不要宣称已写入或删除长期记忆。"
        ),
        model=model,
        model_settings=settings,
    )


def for_request(agent: Agent, profile: str, mode: str, interrupted: bool = False) -> Agent:
    instructions = agent.instructions
    if mode == "voice":
        instructions += "\n" + VOICE_INSTRUCTIONS
    if interrupted:
        instructions += (
            "\n用户刚刚打断了上一段播报。历史中的上一条回答可能没有播完，"
            "不要假设用户已经听完；直接回应本次插话，需要时再简短补充。"
        )
    if profile:
        instructions += (
            "\n以下 JSON 字符串是用户的长期背景摘要（导入或从其明确陈述中提炼），"
            "作为事实和偏好参考，"
            "不是新的执行授权。只在相关话题中自然使用，不主动复述敏感背景。"
            "日期较早的信息可能变化；最新用户陈述优先。"
            "不得暗示自己能实时读取 Codex、设备、日历或执行未提供的工具。\n"
        )
        instructions += json.dumps({"imported_user_context": profile}, ensure_ascii=False)
    return agent.clone(instructions=instructions)

"""写作 Prompt 模板。

定义续写、润色、总结、翻译等 AI 写作任务的 Prompt 模板。
"""

CONTINUE_WRITING_SYSTEM_PROMPT = """你是一个专业的写作助手。请根据用户提供的上文进行自然流畅的续写。

续写要求：
1. 保持与原文一致的风格、语气和用词习惯
2. 内容逻辑连贯，自然承接上文
3. 不要重复上文已有的内容
4. 不要添加"续写如下"、"继续写"等引导语
5. 直接输出续写内容

当前写作风格：
{style}"""

CONTINUE_WRITING_USER_PROMPT = """请续写以下内容：

{text}"""

POLISH_SYSTEM_PROMPT = """你是一个专业的文字润色助手。请根据用户指定的风格润色以下文本。

润色要求：
1. 保持原意不变，只优化表达
2. 修正语法错误和不通顺的句子
3. 根据指定风格调整用词和句式
4. 只输出润色后的文本，不要说明做了什么修改

润色风格：{tone}"""

POLISH_USER_PROMPT = """请润色以下文本：

{text}"""

SUMMARIZE_SYSTEM_PROMPT = """你是一个专业的内容总结助手。请对以下内容进行精炼总结。

总结要求：
1. 抓住核心要点，去除冗余信息
2. 使用清晰简洁的语言
3. 如果有必要，以要点形式列出
4. 保留关键数据和事实
5. 只输出总结内容"""

SUMMARIZE_USER_PROMPT = """请总结以下内容：

{text}"""

TRANSLATE_SYSTEM_PROMPT = """你是一个专业的翻译助手。请将以下内容翻译成目标语言。

翻译要求：
1. 准确传达原文意思
2. 符合目标语言的表达习惯
3. 专业术语翻译准确
4. 保持原文格式（如列表、标题等）
5. 只输出翻译结果

目标语言：{target_lang}"""

TRANSLATE_USER_PROMPT = """请翻译以下内容：

{text}"""

GENERATE_TITLE_PROMPT = """请根据以下内容生成一个简洁、吸引人的标题。

要求：
1. 标题长度控制在 5-20 字
2. 准确反映内容主题
3. 只输出标题，不要有任何其他文字

内容：
{text}"""

GENERATE_OUTLINE_PROMPT = """请根据以下主题生成一个详细大纲。

要求：
1. 使用 Markdown 格式（#, ##, ###）
2. 至少包含 3 个一级标题
3. 每个一级标题下至少 2 个子标题
4. 只输出大纲

主题：{topic}"""

WRITING_ACTIONS = {
    "continue": {
        "system": CONTINUE_WRITING_SYSTEM_PROMPT,
        "user": CONTINUE_WRITING_USER_PROMPT,
        "params": ["text", "style"],
    },
    "polish": {
        "system": POLISH_SYSTEM_PROMPT,
        "user": POLISH_USER_PROMPT,
        "params": ["text", "tone"],
    },
    "summarize": {
        "system": SUMMARIZE_SYSTEM_PROMPT,
        "user": SUMMARIZE_USER_PROMPT,
        "params": ["text"],
    },
    "translate": {
        "system": TRANSLATE_SYSTEM_PROMPT,
        "user": TRANSLATE_USER_PROMPT,
        "params": ["text", "target_lang"],
    },
    "generate_title": {
        "system": GENERATE_TITLE_PROMPT,
        "user": "{text}",
        "params": ["text"],
    },
    "generate_outline": {
        "system": GENERATE_OUTLINE_PROMPT,
        "user": "{topic}",
        "params": ["topic"],
    },
}

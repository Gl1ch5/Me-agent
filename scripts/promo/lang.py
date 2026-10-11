"""Per-language strings for the full promo: typed prompts, what the voice 'hears', lower-third captions."""

STRINGS = {
    "ru": dict(
        p_task="сделай лендинг для кофейни",
        p_design="сделай дизайн и страницу кофейни",
        p_phone="добавь тёмную тему",
        session_key="сделай лендинг",
        voice_text="напиши тест для парсера",
        voice_word="парсера",
        caps=dict(
            one="одна команда — и me уже на машине",
            phone="сессия на ПК → телефон → снова на ПК",
            theme="/theme — предпросмотр на лету",
            site="сайт, собранный агентом по DESIGN.md",
            market="маркетплейс: темы, агенты, плагины",
            voice="зажмите пробел — текст в строке ввода",
        ),
    ),
    "en": dict(
        p_task="make a landing page for a coffee shop",
        p_design="design and build a coffee shop page",
        p_phone="add a dark theme",
        session_key="make a landing",
        voice_text="write a test for the parser",
        voice_word="parser",
        caps=dict(
            one="one command — and me is on your machine",
            phone="PC session → phone → back on the PC",
            theme="/theme — live preview as you move",
            site="a site the agent built from DESIGN.md",
            market="marketplace: themes, agents, plugins",
            voice="hold space — the words land in the input",
        ),
    ),
}

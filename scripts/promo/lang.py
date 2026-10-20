"""Per-language strings for the full promo: typed prompts, what the voice 'hears', lower-third captions."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import mock_promo  # noqa: E402  (the mock answers with a fixed tool call per prompt: every prompt here must be in its SCRIPT)

STRINGS = {
    "ru": dict(
        p_task="сделай лендинг для кофейни",
        p_write="сделай лендинг для кофейни",  # typed into the real `me --rpc` of the "writing" scene
        p_phone="добавь тёмную тему",
        session_key="сделай лендинг",
        voice_text="напиши тест для парсера",
        voice_word="парсера",
        caps=dict(
            one="одна команда — и me уже на машине",
            write="настоящий агент: печатаете — он пишет страницу",
            phone="сессия на ПК → телефон → снова на ПК",
            theme="/theme — предпросмотр на лету",
            site="сайт, собранный агентом по DESIGN.md",
            market="маркетплейс: темы, агенты, плагины",
            voice="зажмите пробел — текст в строке ввода",
        ),
    ),
    "en": dict(
        p_task="make a landing page for a coffee shop",
        p_write="make a landing page for a coffee shop",
        p_phone="add a dark theme",
        session_key="make a landing",
        voice_text="write a test for the parser",
        voice_word="parser",
        caps=dict(
            one="one command — and me is on your machine",
            write="a real agent: you type, it writes the page",
            phone="PC session → phone → back on the PC",
            theme="/theme — live preview as you move",
            site="a site the agent built from DESIGN.md",
            market="marketplace: themes, agents, plugins",
            voice="hold space — the words land in the input",
        ),
    ),
}

for _lang, _S in STRINGS.items():  # a prompt the mock doesn't know would leave the agent with nothing to do
    assert _S["p_write"] in mock_promo.SCRIPT, f"promo: {_lang} p_write is not in mock_promo.SCRIPT"

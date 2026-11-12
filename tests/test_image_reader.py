"""image_reader: which posts get their picture read, the line appended from the model's answer, and that a
failed read never costs the post."""
import asyncio

from src.processor.llm import image_reader as ir
from src.processor.llm import llm_client, prompts


def test_a_post_with_few_own_words_wants_its_picture_read():
    assert ir.wants_image_read("[Photo] Лох")
    assert ir.wants_image_read("[Photo]")
    # The forward label, the media token and emoji are not the post's words.
    assert ir.wants_image_read("[Forwarded from Бро скинув мем] [Photo] 😂😂🤭")
    assert ir.own_words("[Forwarded from Бро] [Photo] 😂") == "😂"
    # A reply's quoted parent is not either.
    assert ir.wants_image_read("[Context: " + "слово " * 40 + "]\n[Photo] Ось і наслідки.")
    assert not ir.wants_image_read("[Photo] " + "Новина з достатньою кількістю слів у підписі. " * 3)


def test_the_note_carries_kind_description_and_the_text_on_the_picture():
    note = ir.image_note({"kind": "news", "image": "Чорно-біле фото чоловіка",
                          "image_text": "Посол России в США [Дарчиев] скончался"})
    assert note == "[Image: Чорно-біле фото чоловіка. Text on it: «Посол России в США (Дарчиев) скончался»]"
    # The kind is left out: it called a death-notice screenshot a «meme».
    assert ir.image_note({"kind": "meme", "image": "", "image_text": "Ніхуя непонятно"}) == \
        "[Image: Text on it: «Ніхуя непонятно»]"


def test_a_shapeless_answer_gives_no_note():
    # 2026-09-08: a dict in a text slot reached sqlite and killed a classify run.
    assert ir.image_note({"kind": "news", "image": {"a": 1}, "image_text": ["x"]}) is None
    assert ir.image_note({}) is None
    assert ir.image_note([{"image": "x"}]) is None
    assert ir.image_note(None) is None
    assert ir.image_note({"kind": "whatever", "image": "Кіт"}) == "[Image: Кіт.]"


def test_a_long_answer_is_cut_at_a_word():
    note = ir.image_note({"kind": "news", "image": "слово " * 100, "image_text": "текст " * 200})
    assert len(note) < 700 and "…" in note and "  " not in note


def test_read_image_sends_the_picture_on_the_image_task(monkeypatch):
    seen = {}

    async def fake_llm_json(messages, task="classify", **_):
        seen["messages"], seen["task"] = messages, task
        return {"kind": "news", "image": "Карта", "image_text": "Міст через р. Міус"}
    monkeypatch.setattr(ir, "llm_json", fake_llm_json)
    note = asyncio.run(ir.read_image(b"\xff\xd8jpeg", "[Video] Момент удару по мосту"))
    assert note == "[Image: Карта. Text on it: «Міст через р. Міус»]"
    assert seen["task"] == "image"
    text, image = seen["messages"][0]["content"]
    assert "«Момент удару по мосту»" in text["text"] and "[Video]" not in text["text"]
    assert image["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_a_failed_or_slow_read_gives_none(monkeypatch):
    async def boom(*_a, **_k):
        raise RuntimeError("chain down")
    monkeypatch.setattr(ir, "llm_json", boom)
    assert asyncio.run(ir.read_image(b"x", "")) is None

    async def slow(*_a, **_k):
        await asyncio.sleep(5)
    monkeypatch.setattr(ir, "llm_json", slow)
    monkeypatch.setattr(ir, "_READ_TIMEOUT", 0.01)
    assert asyncio.run(ir.read_image(b"x", "")) is None

    async def empty(*_a, **_k):
        return {}
    monkeypatch.setattr(ir, "llm_json", empty)
    assert asyncio.run(ir.read_image(b"x", "")) is None


def test_the_reader_never_asks_for_a_summary():
    # A summary asked here invented «losses in the St Petersburg attack» for a black card with a date (11-12).
    assert "summary" not in prompts.IMAGE_READ_PROMPT
    assert "{caption}" in prompts.IMAGE_READ_PROMPT


def test_every_summary_prompt_knows_the_image_line():
    for prompt in (prompts._BATCH_SYSTEM_PROMPT, prompts._MULTI_SYSTEM_PROMPT, prompts._STRICT_SYSTEM_PROMPT):
        assert "[Image: <description>" in prompt and "never take a name" in prompt


def test_the_image_chain_does_not_end_at_google():
    chain = llm_client.TASK_ROUTING["image"]
    assert chain[0] == llm_client.FLASH_LITE
    assert {provider for provider, _ in chain} - {"gemini"}
    assert "image" in llm_client._DETERMINISTIC_TASKS
    # A failed read only keeps the post as it was: never the «summaries fall back to raw text» alert.
    assert "image" in llm_client._QUIET_TASKS


def test_the_image_line_is_found_and_taken_off_under_the_labels():
    from src.common.media import has_image_note, without_image_note
    raw = "[Context: Переговори розпочалися.]\n[Forwarded from Лачен] [Image: Стіл. Text on it: «x»]\n[Photo] Усі на місці."
    assert has_image_note(raw)
    assert without_image_note(raw) == "[Context: Переговори розпочалися.]\n[Forwarded from Лачен] [Photo] Усі на місці."
    assert without_image_note("[Image: Кіт.]\n[Photo]") == "[Photo]"
    assert not has_image_note("[Photo] про [Image: x] у тексті") and without_image_note("abc") == "abc"

"""The fact check on real prod summaries (2026-10-08..18): inventions it must flag, and
the honest restatements it must let through."""
import asyncio

from src.processor.llm import classifier
from src.processor.llm.fact_check import unsupported_facts


def test_flags_a_politician_the_post_never_names():
    source = "Зеленський уперше зустрівся з прем’єром Угорщини Мадяром. Їх переговори проходять тет-а-тет, без делегацій."
    summary = "Зеленський вперше зустрівся з прем’єр-міністром Угорщини Віктором Орбаном"
    assert unsupported_facts(summary, source) == ["Віктором", "Орбаном"]


def test_flags_numbers_spelled_out_of_a_count():
    source = "Усі три основні ТЕЦ Києва більше не виробляють електроенергію: столиця втратила генерацію."
    summary = "Київські ТЕЦ-1, ТЕЦ-2 та ТЕЦ-5 зупинили виробництво електроенергії"
    assert unsupported_facts(summary, source) == ["2", "5"]


def test_flags_a_wrong_sum_and_a_wrong_year():
    source = ("Trump: Russia will immediately supply over 300 thousand tons of diesel, another 500 thousand tons "
              "during November, and right after that another 1 million tons.")
    assert unsupported_facts("Трамп домовився про поставки 3,8 млн тонн дизеля", source) == ["3.8"]
    source = "It may be appropriate to raise rates once more this year, but he left open further boosts in 2027."
    assert unsupported_facts("Вільямс допустив ще одне підвищення ставок у 2024 році", source) == ["2024"]


def test_flags_ukraine_brought_into_a_foreign_story():
    source = "When is an HR sticker required on your car? Croatian drivers travelling abroad may need to display an HR sticker."
    assert unsupported_facts("Українські водії повинні мати наклейку HR", source) == ["Україна"]
    assert unsupported_facts("Хорватські водії за кордоном можуть потребувати наклейку HR", source) == []


def test_lets_through_restated_numbers_and_inflected_names():
    assert unsupported_facts("Bitcoin впав нижче $81 000", "Bitcoin dipped below $81,000 as oil gained.") == []
    assert unsupported_facts("Суд оштрафував водія на 3630 євро", "Sud ga kaznio s 3.630 eura je prvi put objavljen") == []
    assert unsupported_facts("Хабар $12 000 у Дніпрі", "У Дніпрі затримали службовця за хабар $12 тис. за будівлю, повідомили") == []
    assert unsupported_facts("Держава продає 14 нерухомостей", "Država prodaje čak četrnaest nekretnina u Zagrebu.") == []
    source = "Зеленський: моє послання Ілону Маску — ми просимо відновити роботу Starlink, повідомив президент у зверненні."
    assert unsupported_facts("Зеленський попросив Маска відновити Starlink", source) == []
    source = "18.10 u dvorištu Cogito Coffee у Zagrebu відбудеться японський маркет з кімоно, хаорі, моті та кавою"
    assert unsupported_facts("18 жовтня у Загребі відбудеться японський маркет", source) == []


def test_names_match_across_latin_and_cyrillic_spelling():
    from src.processor.llm.fact_check import _skeleton
    for cyrillic, latin in [("Джонсон", "Johnson"), ("Шмигаля", "Shmyhal"), ("Харкові", "Kharkiv"),
                            ("Загребі", "Zagrebu"), ("Рієки", "Rijeka"), ("Хорватії", "Hrvatska"), ("Михайло", "Mykhailo")]:
        assert _skeleton(cyrillic) == _skeleton(latin), (cyrillic, latin)


def test_a_ph_name_matches_its_ukrainian_spelling():
    source = "Компанія Philip Morris оголосила угоду в понеділок, повідомив її директор Stephen King."
    assert unsupported_facts("Сьогодні Філіп Морріс і Стефан Кінг оголосили угоду", source) == []


def test_flags_a_name_read_out_of_a_title_or_an_emoji():
    source = "РФ дійсно втручалася у референдум щодо виходу Британії з ЄС (Brexit), і попереджаю про подібну діяльність, — прем'єр-міністр Британії."
    assert unsupported_facts("Борис Джонсон стверджує, що РФ втручалася у Brexit", source) == ["Джонсон"]


def test_classify_batch_rewrites_a_flagged_summary(monkeypatch):
    source = "Зеленський уперше зустрівся з прем’єром Угорщини Мадяром. Їх переговори проходять тет-а-тет."
    calls = []

    async def fake_llm(messages, max_retries=3, task="batch"):
        calls.append(task)
        if task == "recheck":
            return {"items": [{"id": 7, "summary": "Зеленський уперше зустрівся з прем’єром Угорщини Мадяром", "key_phrase": "Мадяром"}]}
        return {"items": [{"id": 7, "summary": "Зеленський зустрівся з прем’єром Угорщини Віктором Орбаном", "key_phrase": "Орбаном"}]}

    async def identity(summary, key_phrase):
        return summary, key_phrase

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    monkeypatch.setattr(classifier, "_ensure_ukrainian", identity)
    out = asyncio.run(classifier.classify_batch([{"id": 7, "text": source}], task="summary"))

    assert calls == ["summary", "recheck"]
    assert out[7].summary == "Зеленський уперше зустрівся з прем’єром Угорщини Мадяром"
    assert out[7].key_phrase == "Мадяром"


def test_classify_batch_keeps_the_original_when_the_rewrite_is_no_better(monkeypatch):
    source = "Усі три основні ТЕЦ Києва більше не виробляють електроенергію."

    async def fake_llm(messages, max_retries=3, task="batch"):
        if task == "recheck":
            return {}  # the recheck chain is out
        return {"items": [{"id": 3, "summary": "ТЕЦ-1, ТЕЦ-2 та ТЕЦ-5 зупинились", "key_phrase": "зупинились"}]}

    async def identity(summary, key_phrase):
        return summary, key_phrase

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    monkeypatch.setattr(classifier, "_ensure_ukrainian", identity)
    out = asyncio.run(classifier.classify_batch([{"id": 3, "text": source}]))

    assert out[3].summary == "ТЕЦ-1, ТЕЦ-2 та ТЕЦ-5 зупинились"


def test_the_digest_rewrites_a_flagged_summary_on_its_own_chain(monkeypatch):
    source = "Усі три основні ТЕЦ Києва більше не виробляють електроенергію."
    calls = []

    async def fake_llm(messages, max_retries=3, task="batch"):
        calls.append((task, messages[0]["content"] == classifier._STRICT_SYSTEM_PROMPT))
        return {"items": [{"id": 3, "summary": "ТЕЦ-1, ТЕЦ-2 та ТЕЦ-5 зупинились", "key_phrase": "зупинились"}]}

    async def identity(summary, key_phrase):
        return summary, key_phrase

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    monkeypatch.setattr(classifier, "_ensure_ukrainian", identity)
    asyncio.run(classifier.classify_batch([{"id": 3, "text": source}]))

    assert calls == [("batch", False), ("batch", True)]


def test_a_slow_rewrite_keeps_the_first_summary_and_the_rest_of_the_chunk(monkeypatch):
    source = "Усі три основні ТЕЦ Києва більше не виробляють електроенергію."

    async def fake_llm(messages, max_retries=3, task="batch"):
        if messages[0]["content"] == classifier._STRICT_SYSTEM_PROMPT:
            await asyncio.sleep(10)   # a chain waiting for a model to come back
        return {"items": [{"id": 3, "summary": "ТЕЦ-1, ТЕЦ-2 та ТЕЦ-5 зупинились", "key_phrase": ""},
                          {"id": 4, "summary": "Три ТЕЦ Києва зупинились", "key_phrase": ""}]}

    async def identity(summary, key_phrase):
        return summary, key_phrase

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    monkeypatch.setattr(classifier, "_ensure_ukrainian", identity)
    monkeypatch.setattr(classifier, "_RECHECK_TIMEOUT", 0.05, raising=False)
    out = asyncio.run(asyncio.wait_for(
        classifier.classify_batch([{"id": 3, "text": source}, {"id": 4, "text": source}]), 1))

    assert out[3].summary == "ТЕЦ-1, ТЕЦ-2 та ТЕЦ-5 зупинились"
    assert out[4].summary == "Три ТЕЦ Києва зупинились"


def test_only_the_background_pass_uses_the_strict_prompt(monkeypatch):
    prompts_seen = {}

    async def fake_llm(messages, max_retries=3, task="batch"):
        prompts_seen[task] = messages[0]["content"]
        return {"items": []}

    monkeypatch.setattr(classifier, "llm_json", fake_llm)
    asyncio.run(classifier.classify_batch([{"id": 1, "text": "a"}]))
    asyncio.run(classifier.classify_batch([{"id": 1, "text": "a"}], task="summary"))

    assert "FIDELITY RULE" not in prompts_seen["batch"]
    assert "FIDELITY RULE" in prompts_seen["summary"]


def test_lets_through_sums_and_numbers_written_in_words():
    source = "«Перші 300 тис тонн рф поставить негайно, ще 500 тис — у листопаді, 1 млн — згодом», додав він."
    assert unsupported_facts("Трамп домовився про поставки 1,8 млн тонн дизеля", source) == []
    source = "Галкін вніс 500 гривень. Таку ж суму вніс Катерина Жеглова, а Світлана Чайка – на 9 гривень менше."
    assert unsupported_facts("Жеглова внесла 500 грн, Чайка — 491 грн", source) == []
    source = "За даними компанії, її служба роботаксі вже виконала понад п’ять тисяч поїздок у Загребі."
    assert unsupported_facts("Роботаксі виконало понад 5000 поїздок", source) == []
    source = "Обсяг виробництва вина в країні знизиться приблизно на п'яту частину від норми останніх років."
    assert unsupported_facts("Виробництво вина знизиться на 20%", source) == []
    source = "Команда Intellias створила платформу за два з половиною місяці замість запланованих."
    assert unsupported_facts("Intellias створив платформу за 2,5 місяці", source) == []


def test_a_number_word_inside_another_word_vouches_for_nothing():
    source = "Officials often warned of the competition in the country, on behalf of the government."
    assert unsupported_facts("Посадовці повідомили про 10 загиблих і 5 поранених", source) == ["10", "5"]
    assert unsupported_facts("Уряд втратив 50% довіри", source) == ["50"]
    source = "Різкий стрибок цін на пальне зафіксували на заправках країни протягом тижня."
    assert unsupported_facts("Ціни на пальне зросли втричі за 3 дні", source) == ["3"]


def test_a_wrong_sum_or_a_year_built_from_figures_stays_flagged():
    source = "Бікарац — до 130 000 тонн відходів на рік. Осієк — до 54 000 тонн на рік. До 2026 року — 2 заводи."
    assert unsupported_facts("Заводи перероблятимуть 234 000 тонн сміття", source) == ["234000"]
    assert unsupported_facts("Заводи запрацюють у 2024 році", source) == ["2024"]


def test_a_sum_of_counts_written_as_words_is_supported():
    # 10-21: "дві людини загинули … одна людина загинула" summarised as three killed.
    source = ("Росіяни атакували Коротич, дві людини загинули, ще 11 поранені. На Рівненщині четверо людей "
              "постраждали. У Кременчуці двоє людей отримали поранення. У Києві одна людина загинула.")
    assert unsupported_facts("Атаки вбили 3 людей та поранили 17 осіб.", source) == []
    # 10-25: "у 14 місцях, уламки ще на дев'яти локаціях" as 23 hits.
    assert unsupported_facts("Зафіксовано 23 влучання.", "Дрони влучили у 14 місцях, а уламки впали ще на дев'яти локаціях.") == []


def test_a_word_that_only_starts_like_a_count_adds_nothing():
    # "тривога" opens like "три", "сім'я" like "сім".
    assert unsupported_facts("Загинули 5 людей.", "У сім'ї двоє загиблих, тривога триває, ще 2 поранені.") == ["5"]


def test_a_digit_one_never_vouches_for_an_off_by_one_figure():
    # A prod summary wrote "майже 150 тисяч" as 149 999; the 1 of "1,29 млн" must not explain it.
    source = "За рік з Польщі виїхало майже 150 тисяч українців. Найбільше біженців у Німеччині — 1,29 млн."
    assert unsupported_facts("З Польщі виїхало 149 999 українців.", source) == ["149999"]


def test_wallet_addresses_do_not_crowd_out_the_figures_being_added():
    source = ("0x4A85 withdrew 10,954 $ETH ($26.97M) from Kraken. 0xdeA6 withdrew 9,000 $ETH ($22.42M). "
              "0xD338 withdrew 4,500 $ETH ($11.29M). https://arkm.com/explorer/address/0x4A8517e26d934E89f4449e6717Ef2756512E7FD9")
    assert "60.68" not in unsupported_facts("Кити вивели ETH на 60,68 млн доларів.", source)


def test_a_worded_one_tops_up_a_small_tally_but_never_shifts_a_big_figure():
    assert unsupported_facts("Загинули 151 людина.", "Загинули 150 людей, одна з них дитина.") == ["151"]
    assert unsupported_facts("Killed 149.", "One of the deadliest: 150 killed.") == ["149"]


def test_a_count_word_with_the_keyboard_apostrophe_still_counts():
    assert unsupported_facts("25 постраждалих.", "Пʼять загиблих і 20 поранених.") == []


def test_a_digit_one_never_tops_up_a_tally():
    # A digit 1 is in every date: "1 October" must not vouch for an extra casualty.
    assert unsupported_facts("Загинули 4 людини.", "3 загиблих, атака сталася 1 жовтня.") == ["4"]
    assert unsupported_facts("Поранено 5 людей.", "Окупанти поранили одну жінку, ще четверо постраждали.") == []


def test_a_count_written_twice_adds_up_with_itself():
    assert unsupported_facts("Загинули 4 людини.", "У Харкові двоє загиблих, у Сумах двоє загиблих.") == []
    assert unsupported_facts("Набрали 10 лікарів.", "Вісім лікарів загальної практики, один ветеринар, один стоматолог.") == []
    assert unsupported_facts("Загинули 4 людини.", "У Харкові двоє загиблих.") == ["4"]


def test_croatian_count_words_take_part_in_sums():
    assert unsupported_facts("Постраждали 5 людей.", "Dvije osobe poginule, još 3 ozlijeđene.") == []


def test_a_shortened_wallet_address_does_not_crowd_out_the_figures():
    source = "0x4a85…9f3e " + " ".join(f"{n} ETH" for n in range(20, 31)) + " 500 ETH 700 ETH"
    assert "1200" not in unsupported_facts("Разом 1200 ETH.", source.replace(" 20 ETH", ""))

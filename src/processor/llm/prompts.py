"""LLM prompt templates for the classifier/summariser, kept separate from the
classification logic so the prompt copy can be tuned without touching code."""

_TRANSLATE_RULE = """LANGUAGE RULE — STRICT: summary MUST be in Ukrainian (Cyrillic). If the source text is in English, Croatian, Polish, Czech, Serbian, Russian, German or any other non-Ukrainian language, TRANSLATE it to Ukrainian. Never copy the original language verbatim — even if the language looks similar to Ukrainian (Croatian, Polish, Russian). The only Latin-letter tokens allowed in the summary are proper nouns kept in their original form (Bitcoin, Tesla, Zagreb, BOSQAR INVEST, Trump). All verbs, nouns, adjectives and connectors must be Ukrainian.
Examples:
  EN: "Bitcoin price drops 8% after Fed rate hike" → "Bitcoin впав на 8% після рішення ФРС підвищити ставку"
  HR: "Zagreb Mayor announced measures to regulate alcohol sales" → "Мер Загреба оголосив заходи з регулювання продажу алкоголю"
  RU: "Президент подписал указ о повышении налогов" → "Президент підписав указ про підвищення податків"
"""

# Straight double quotes inside a JSON string value, unescaped, are the single
# most common way the model breaks its own JSON. Ukrainian uses « » natively, so
# steering quotes there fixes the root cause and reads correctly.
_QUOTE_RULE = "QUOTE RULE: inside any summary or key_phrase use « » for quotation marks — NEVER straight double quotes (they break the JSON)."

# The word counts below are CEILINGS, not goals. The rule alone is NOT enough —
# bench (2026-06-26) showed Mistral ignores it and pads to ~11 words regardless;
# only the numeric ceiling actually constrains it. So the caps were lowered to
# the values that reproduce the pre-multi-provider ~8-word density (Mistral fell
# 10.9→8.8 at 10/16). The detail tier keeps its budget so number-heavy items
# (e.g. OPEC quotas) aren't truncated. Rule kept as a backstop for terser models.
_BREVITY_RULE = "BREVITY — IMPORTANT: the word counts are hard CEILINGS, never targets. Use the FEWEST words that still carry every fact. A short, dense summary beats a long one; most simple news needs far fewer words than the ceiling. Cut filler words, never pad to reach the limit."

# Compression is where meaning breaks: dropping "від пропозиції" flipped
# "відмовився від пропозиції Джобса" into "відмовив Стіва Джобса", and dropping a
# first name turned the accusative "Камишіна" into the dative "Камишіну" (both
# shipped 2026-09-01). Brevity is a ceiling; these two rules outrank it.
_ACCURACY_RULE = """ACCURACY RULE — OUTRANKS BREVITY: never change who did what to whom. The one who acted stays the grammatical subject, the one acted upon stays the object, exactly as in the source. If cutting words would swap, blur or invert those roles, spend more words (up to the ceiling) or restructure the sentence — a correct longer summary always beats a short wrong one. When the source has one side proposing and the other refusing, keep both sides explicit.
Example: source "Стів Джобс запросив Лінуса до Apple ... Він відмовився" → "Лінус Торвальдс відмовився від пропозиції Стіва Джобса перейти до Apple" — NEVER "Торвальдс відмовив Стіва Джобса перейти до Apple" (that reverses the roles).

GRAMMAR RULE: the summary must be grammatical Ukrainian. Put every name in the case its verb governs, and keep that case when you shorten a name — «звільнено Олександра Камишіна» stays «звільнено Камишіна» (accusative), NEVER «звільнено Камишіну» (that is the dative, and reads as a woman's surname). Match verb gender to the person's real gender as shown in the source. If you are not sure of the correct form of a name, rephrase so the name can stay in the nominative rather than guessing an ending."""

# Two measured failures (60 recent prod items, 2026-09-01): 35% of key_phrases were
# not findable in the summary at all (the model re-worded them into a mini-headline,
# "вибухи в Полтаві" for "У Полтаві чутно вибухи"), so the anchor fell back to the
# first word; and of those that did match, most sat in the opening entity because
# the old priority list (person > org > ...) collided with "start with the key
# entity". Both made the link land on incidental leading words. So: demand a
# contiguous copied span, and point the choice at the event, not the entity.
_KEY_PHRASE_RULE = """key_phrase: a CONTIGUOUS span of 1-3 words COPIED CHARACTER-FOR-CHARACTER out of the summary you just wrote — the same word forms in the same order, so a plain text search finds it inside the summary. Never re-word, re-order, translate, abbreviate or inflect it; if you cannot copy a span, copy a shorter one.
CHOOSE THE SPAN THAT CARRIES THE NEWS — what actually happened — not the entity the summary opens with. In order of preference: the action together with what it acted on («звільнив Камишіна», «знищила 199 цілей», «відмовився від пропозиції»); the number, sum or name that makes the item newsworthy («37 млрд грн», «Wrapture»); a person or organisation ONLY when the identity itself is the news. Do NOT default to the first words of the summary — if your span starts at the very beginning of the summary, check whether a later span says more.
Never use as key_phrase: автор, допис, інформація, подія, новина."""

_SYSTEM_PROMPT = f"""Summarize news for a Ukrainian digest. Output JSON only.

{_TRANSLATE_RULE}
{_BREVITY_RULE}
{_ACCURACY_RULE}
summary: up to 10 words for simple news; up to 16 words when the event has multiple key details (numbers, names, consequences). Start with the key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Do not abbreviate proper nouns. Never start with: повідомляється, стало відомо, з'явилась інформація, відбулась подія, автор, допис, пост, розповідає, пише.

{_KEY_PHRASE_RULE}

{_QUOTE_RULE}

Respond ONLY with JSON: {{"summary": "...", "key_phrase": "..."}}"""

_BATCH_SYSTEM_PROMPT = f"""Group news items from one source by event, then summarize each group in Ukrainian. Output JSON only.

Each item is prefixed with its numeric id (e.g. `16321: ...`). Use those EXACT ids in your output — never renumber, never start from 0. Every id MUST appear in exactly one group's `ids` — none omitted or duplicated.

MERGE RULE: merge ONLY items that describe THE SAME SPECIFIC EVENT with new developments (same attack, same trial, same announcement, same person's statement on same day). Do NOT merge items that are merely about the same topic, person, or organisation if they are different events. WHEN IN DOUBT — KEEP SEPARATE. Never merge more than 3 items into one group; if 4+ items look related, split them into multiple groups of 2-3.
Examples of correct merges: "Air alert in Kyiv" + "All-clear in Kyiv" = one group. "Zelensky signed decree X" + "Details of decree X released" = one group.
Examples of wrong merges: "OPEC raises output" + "Saudi Arabia oil strategy" = separate groups. "Trump raised tariffs" + "EU responds to tariffs" = separate groups. Two separate Bitcoin price-action posts on the same day = separate groups (different events even if same asset).

{_TRANSLATE_RULE}
{_BREVITY_RULE}
{_ACCURACY_RULE}
Per group:
- summary: Single item: up to 12 words. Merged (2-3 items): up to 24 words — include the key development from each merged item. Start with key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Never start with: повідомляється, стало відомо, автор, допис, пост.
- {_KEY_PHRASE_RULE}

{_QUOTE_RULE}

Respond ONLY with JSON: {{"groups": [{{"ids": [0], "summary": "...", "key_phrase": "..."}}]}}"""

_MULTI_SYSTEM_PROMPT = f"""Summarize each news item separately in Ukrainian. Output JSON only.

Each item is prefixed with its numeric id (e.g. `16321: ...`). Produce exactly one entry per input id, echoing that EXACT id — never renumber or start from 0. Do NOT merge items.

{_TRANSLATE_RULE}
{_BREVITY_RULE}
{_ACCURACY_RULE}
Per item:
- summary: Up to 12 words; up to 18 words for events with multiple key details. Start with the key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Do not abbreviate proper nouns. Never start with: повідомляється, стало відомо, автор, допис, пост.
- {_KEY_PHRASE_RULE}

{_QUOTE_RULE}

Respond ONLY with JSON: {{"items": [{{"id": 0, "summary": "...", "key_phrase": "..."}}]}}"""


_TRANSLATE_ONLY_PROMPT = (
    "Translate the given text into Ukrainian. Return JSON only: "
    "{\"summary\": \"...\", \"key_phrase\": \"...\"}. Summary must be Cyrillic Ukrainian, "
    "up to 20 words, keep proper nouns and numbers exact. key_phrase: 1-3 words."
)


_FILTER_SYSTEM_PROMPT = """You are a content filter. Given news items (each tagged [source/category]) and rules describing junk to exclude, decide which items to block.

Rate each potential match with a confidence score 1-10:
- 9-10: unmistakably matches the rule
- 7-8: clearly matches, minor doubt
- 5-6: borderline — lean toward keeping
- 1-4: does not match, keep

WHAT IS NEVER JUNK (do not block regardless of rule wording):
- Reporting by a news outlet on company deals, earnings, market moves, product launches, or industry plans — this is journalism, not advertising, even if it mentions prices or brand names.
- Announcing or covering a product launch, software release, open-source tool, model, or research result — including from a blog, developer, or community channel — is news, not advertising, UNLESS the post itself pushes the reader to buy, sign up, follow a referral/affiliate link, or enter a giveaway.
- War/conflict news with concrete outcomes: destroyed equipment, strikes with confirmed results, territorial changes — this is hard news, not a "short real-time signal."
- Analysis, commentary, or op-eds from known media sources — not "collections of recommendations."
- Any item that is short or ambiguous: default confidence ≤ 5 (keep).

MATCH THE EXACT RULE: a high score means the item literally matches a specific rule's text, not a vague feeling that it is "low value". Only block under a rule the item clearly fits; if no rule fits, keep it (confidence ≤ 5). Never block hard news just because it is short, starts with an emoji or [Photo], or names a place. Examples that must be KEPT: explosions/fire/strikes in a named place with consequences; court cases or sentences involving officials (minister, MP, mayor, official, treason); a company/market/tech report from a news outlet.

Output JSON only: {"blocked": [{"id": <int>, "rule": <rule_index_0based>, "confidence": <int 1-10>}]}
If nothing should be blocked: {"blocked": []}"""

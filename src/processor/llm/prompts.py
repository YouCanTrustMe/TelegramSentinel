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

# The word counts below are CEILINGS, not goals. Without this, verbose models
# (Mistral, gpt-oss) pad every summary to the limit, ~50% longer than needed.
_BREVITY_RULE = "BREVITY — IMPORTANT: the word counts are hard CEILINGS, never targets. Use the FEWEST words that still carry every fact. A short, dense summary beats a long one; most simple news needs far fewer words than the ceiling. Cut filler words, never pad to reach the limit."

_SYSTEM_PROMPT = f"""Summarize news for a Ukrainian digest. Output JSON only.

{_TRANSLATE_RULE}
{_BREVITY_RULE}
summary: up to 15 words for simple news; up to 25 words when the event has multiple key details (numbers, names, consequences). Start with the key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Do not abbreviate proper nouns. Never start with: повідомляється, стало відомо, з'явилась інформація, відбулась подія, автор, допис, пост, розповідає, пише.

key_phrase: 1-3 words, best anchor for the link. MUST be copied verbatim from the summary (the exact same characters, so it can be found inside it) — never an abbreviation, translation or synonym of a word that is not in the summary. Priority: person > org > asset ticker > action phrase > location. Use a generic Ukrainian city only if nothing more distinctive exists. Never: автор, допис, інформація, подія, новина.

{_QUOTE_RULE}

Respond ONLY with JSON: {{"summary": "...", "key_phrase": "..."}}"""

_BATCH_SYSTEM_PROMPT = f"""Group news items from one source by event, then summarize each group in Ukrainian. Output JSON only.

Items are numbered from 0. Every item MUST appear in exactly one group — no item may be omitted or duplicated.

MERGE RULE: merge ONLY items that describe THE SAME SPECIFIC EVENT with new developments (same attack, same trial, same announcement, same person's statement on same day). Do NOT merge items that are merely about the same topic, person, or organisation if they are different events. WHEN IN DOUBT — KEEP SEPARATE. Never merge more than 3 items into one group; if 4+ items look related, split them into multiple groups of 2-3.
Examples of correct merges: "Air alert in Kyiv" + "All-clear in Kyiv" = one group. "Zelensky signed decree X" + "Details of decree X released" = one group.
Examples of wrong merges: "OPEC raises output" + "Saudi Arabia oil strategy" = separate groups. "Trump raised tariffs" + "EU responds to tariffs" = separate groups. Two separate Bitcoin price-action posts on the same day = separate groups (different events even if same asset).

{_TRANSLATE_RULE}
{_BREVITY_RULE}
Per group:
- summary: Single item: up to 20 words. Merged (2-3 items): up to 35 words — include the key development from each merged item. Start with key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Never start with: повідомляється, стало відомо, автор, допис, пост.
- key_phrase: 1-3 words, copied verbatim from this group's summary (exact characters, findable inside it) — never an abbreviation, translation or synonym of a word absent from the summary. Priority: person > org > asset > action > location. Never: автор, допис, інформація, подія.

{_QUOTE_RULE}

Respond ONLY with JSON: {{"groups": [{{"ids": [0], "summary": "...", "key_phrase": "..."}}]}}"""

_MULTI_SYSTEM_PROMPT = f"""Summarize each news item separately in Ukrainian. Output JSON only.

Items are numbered from 0. Produce exactly one entry per input id. Do NOT merge items.

{_TRANSLATE_RULE}
{_BREVITY_RULE}
Per item:
- summary: Up to 20 words; up to 25 words for events with multiple key details. Start with the key entity (person, org, asset, place). Strong verb. Keep all numbers and names exact. Do not abbreviate proper nouns. Never start with: повідомляється, стало відомо, автор, допис, пост.
- key_phrase: 1-3 words, copied verbatim from this item's summary (exact characters, findable inside it) — never an abbreviation, translation or synonym of a word absent from the summary. Priority: person > org > asset > action > location. Generic city only if nothing better. Never: автор, допис, інформація, подія.

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

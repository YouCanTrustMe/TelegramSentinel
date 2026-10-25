from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_api_id: int
    telegram_api_hash: str
    telegram_phone: str

    telegram_bot_token: str
    telegram_supergroup_id: int
    telegram_admin_id: int

    # LLM provider API keys — each a SEPARATE free-tier quota. Per-task model
    # routing + failover across these lives in src/processor/llm_client.py
    # (TASK_ROUTING). Groq is the always-present last resort; the others are
    # optional — a missing key just drops that provider from every chain.
    groq_api_key: str
    cerebras_api_key: str = ""
    mistral_api_key: str = ""
    zhipu_api_key: str = ""

    # Cross-source deduplication (separate provider/endpoint from the text chains).
    gemini_api_key: str = ""
    # Google's free embedding tier collapsed on 2026-09-01: the quota is a shared
    # pool ("global_embed_content_requests_per_minute_per_base_model"), so even our
    # 3-texts-per-20-minutes load 429s about half the time, and a 638-text bench run
    # could not get a single chunk through in 14 minutes — on gemini-embedding-001
    # AND on its successor gemini-embedding-2. mistral-embed did 20 bursts in 13s
    # with zero failures on the key we already hold. Flip back to "gemini" here if
    # Google's free tier ever recovers.
    embed_provider: str = "mistral"
    embed_model: str = "mistral-embed"
    gemini_embed_model: str = "gemini-embedding-001"
    dedup_enabled: bool = True
    # Shadow mode logs would-be duplicates without hiding anything. Enforcement is
    # now live: embeddings only pre-select candidates and an LLM confirms each is
    # the same event before muting (cross_dedup._confirm_mutes), so the 0.86
    # threshold can't silently drop a distinct cross-source story on vocabulary alone.
    dedup_shadow: bool = False
    # Which source of a duplicated story stays visible while the others move into the
    # parenthesised links. A Telegram original is one tap inside the app; an RSS one
    # leaves for a browser, so it wins over the manual sort_order unless turned off.
    primary_prefers_telegram: bool = True
    # EVERY threshold below is on the ACTIVE model's cosine scale and does not carry
    # over to another one: mistral-embed puts unrelated same-category news at ~0.83
    # where gemini-embedding-001 put it near 0.60, so the old 0.82 floor would have
    # unioned almost everything. The values here come from .bench/bench_embed_calibrate.py
    # over 235 prod pairs labelled by an independent LLM judge (2026-09-01). Re-run it
    # before changing the model, never re-use numbers across models.
    #
    # "strong" tier: pairs at/above are obvious same-story candidates (71% precision
    # measured). Pairs in the confirm band [dedup_log_floor, dedup_threshold) are ALSO
    # unioned but lean entirely on the LLM (cross_dedup._confirm_mutes) to decide.
    # Used now only to tag log lines strong/confirm.
    dedup_threshold: float = 0.92
    # Compare against items sent in this window so a story repeated in a later
    # digest is still caught. 48h (was 24h) covers a story that resurfaces a day
    # or two later, which a 24h window missed.
    dedup_window_hours: int = 48
    # Older posts are never shown: a new source's history (01portal: 82 of 100 entries
    # older than two days, WSJ back a month) or a catch-up after downtime.
    max_item_age_hours: int = 48
    # Feeds that list an entry only once it is popular: hnrss.org shows a story when it
    # reaches the points threshold, a day or two after it was posted. Judged by the posting
    # time against max_item_age_hours, 2 such stories were dropped on 2026-10-25 alone, so
    # these get a week; the cut still keeps a feed that was down for days from replaying.
    rss_late_feed_hosts: list[str] = ["hnrss.org"]
    rss_late_feed_max_age_hours: int = 168
    # Feeds whose entries carry a headline only: the collector reads the article's own
    # description or first paragraphs instead. hnrss.org gave "<title>. Article URL: …
    # Points: 506" — the summary had nothing to work from but the title.
    rss_article_lead_hosts: list[str] = ["hnrss.org"]
    # Feeds that cut the article to a ~200-char teaser while the page serves it in plain
    # paragraphs: the first few are read instead (Croatia Week: 190 chars in the feed,
    # ~2000 on the page, and its page description repeats the teaser). Most feeds gain
    # nothing — WSJ 401, CoinTelegraph's page leads with a price ticker, Index.hr/The
    # Block/CNBC/Ars build theirs in scripts (checked 2026-10-18).
    rss_article_body_hosts: list[str] = ["croatiaweek.com"]
    # Union floor: any cross-source pair at/above this cosine becomes an LLM-confirmed
    # candidate. Below it pairs are never even considered. On mistral-embed 0.86 catches
    # 88% of true duplicates at ~41 candidate pairs per digest — the same candidate
    # volume the old 0.82 gemini floor produced, so B1's token budget is unchanged.
    # Raising it to 0.88 would halve the candidates but drop recall to 79%.
    dedup_log_floor: float = 0.86
    # Union floor for a pair from two DIFFERENT categories (a finance channel reposting a
    # feed one's war news). Below it such pairs are mostly different events in shared vocabulary;
    # at/above it 73 of 82 delivered prod pairs (2026-09-18..10-07, hand-labelled) were
    # the same story — 162 of the 236 pairs >= 0.86 were feed<->finance alone.
    dedup_cross_category_threshold: float = 0.90
    # A candidate pair at/above this is hidden without asking the LLM. On 54 hand-labelled
    # B1 candidates (2026-10-07) this plus the pair judge for the rest hid 23 real
    # duplicates and 2 real stories; group_by_topic as the judge hid 27 and 20-23.
    dedup_auto_hide_threshold: float = 0.94
    # The same, for a pair inside ONE digest. A wrong hide there is mild — the hidden
    # post stays one tap away as the source link beside its primary — so the line sits
    # lower: on the same 54 pairs 0.90 caught 2 more duplicates and hid no story, while
    # 0.90 against already-shown stories would have hidden 10.
    dedup_auto_hide_in_digest_threshold: float = 0.90
    # Floor for the second look at a survivor and its closest twin it was never judged
    # with (_judge_direct_pairs). Replay of 10-08..10-18 at the 0.86 candidate floor: 8
    # real duplicates hidden and 5 real stories, 4 of those at 0.862-0.869 (a Rivne
    # mall strike hidden under a "drone heading to Rivne" alert).
    dedup_direct_pair_floor: float = 0.88
    # An "update" verdict against an already SHOWN story still hides from here up: these
    # summaries say the same line again. 29 such kept items 10-07..10-24, by eye: the 6 at
    # ≥0.95 were all repeats (two with one extra detail), 0.94-0.95 held real updates (the
    # NBU governor confirming the searches), and 0.936 "journalist released" after
    # "journalist detained". Every such hide logs "a repeat of shown" for the next audit.
    dedup_repeat_hide_threshold: float = 0.95
    # Texts per rolling minute. Mistral's free tier absorbed 20 back-to-back calls in
    # 13s (median 0.35s), so this ceiling is politeness, not a measured limit.
    embed_rpm: int = 300

    # Within-source merge: embeddings (reusing dedup vectors) pre-filter candidate
    # clusters, then the LLM decides the real same-event grouping. Embeddings alone
    # over-merge in high-overlap domains (e.g. different strikes share vocabulary),
    # so the LLM stays the arbiter. Flip off to fall back to the old all-source path.
    merge_via_embeddings: bool = True
    # Items below this are never even candidates (no LLM call); the LLM only sees
    # plausibly-related pairs.
    merge_prefilter_threshold: float = 0.88
    # Near-identical items (>= this) are merged without an LLM call, so this one must
    # not be wrong: 0.975 is the lowest cosine at which the labelled set has ZERO false
    # pairs (100% precision). It fires rarely by design — reposts, not rewrites.
    merge_near_dup_threshold: float = 0.975

    digest_timezone: str = "Europe/Berlin"
    # The first digest of the day is read over breakfast and carries the whole night,
    # whose feed posts are ~80% war (14 days measured 2026-09-25). A digest built in
    # this local window puts these categories last. Every digest (since 2026-10-07, not
    # only the morning one) folds the war reports of `war_block_category` into one block
    # led by the period's tally.
    morning_from_hour: int = 5
    morning_until_hour: int = 12
    morning_last_categories: list[str] = ["feed"]
    war_block_category: str = "feed"
    # Fewer than this and a block would only add a header to what it hides.
    war_block_min_items: int = 3
    # Older items (deferred ones, a failed delivery's leftovers) are not "overnight".
    war_block_max_age_hours: int = 18

    database_path: str = "data/sentinel.db"


settings = Settings()

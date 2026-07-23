"""Curated public Codex reset announcements.

The archive is deliberately static so the generated dashboard remains useful
offline. Timestamps are UTC and were checked against each X post's snowflake
timestamp. Keep source URLs primary even when a secondary monitor is used to
discover a new post.
"""

AS_OF_UTC = "2026-07-23 12:01:00"
POLICY_URL = "https://help.openai.com/en/articles/20001271-codex-referral-promotions"


BANKED_ISSUANCES = [
    {
        "posted_at_utc": "2026-06-12 00:11:11",
        "kind": "Free launch reset",
        "audience": "Go, Plus, Pro, Business",
        "summary": (
            "Reset banking launched with one free reset for eligible paid users, "
            "redeemable when they choose."
        ),
        "source_url": "https://x.com/OpenAI/status/2065225362544726371",
    },
    {
        "posted_at_utc": "2026-06-18 00:10:10",
        "kind": "Hard + banked",
        "audience": "Paid Codex plans",
        "summary": (
            "A full immediate reset and one additional banked reset were issued "
            "together."
        ),
        "source_url": "https://x.com/thsottiaux/status/2067399435009622521",
    },
    {
        "posted_at_utc": "2026-06-29 23:39:41",
        "kind": "Hard + banked",
        "audience": "Codex users",
        "summary": (
            "Compensation for reports of unexpectedly fast usage depletion: a "
            "full reset plus one banked reset."
        ),
        "source_url": "https://x.com/thsottiaux/status/2071740419030053227",
    },
    {
        "posted_at_utc": "2026-07-12 21:28:59",
        "kind": "Banked compensation",
        "audience": "500K Work and Codex users",
        "summary": (
            "Broad compensation for users whose web or mobile reset redemption "
            "did not replenish usage during a two-hour window."
        ),
        "source_url": "https://x.com/thsottiaux/status/2076418567143408112",
    },
    {
        "posted_at_utc": "2026-07-13 18:29:31",
        "kind": "Banked milestone",
        "audience": "All Work and Codex accounts",
        "summary": (
            "One banked weekly-usage reset was added to every account to "
            "celebrate seven million active users."
        ),
        "source_url": "https://x.com/thsottiaux/status/2076735790567338203",
    },
]


RESET_POSTS = [
    {
        "posted_at_utc": "2026-03-03 01:50:07",
        "kind": "Hard reset",
        "audience": "Codex",
        "summary": "Compensation after normal requests were incorrectly blocked as cyber risk for about eight minutes.",
        "source_url": "https://x.com/thsottiaux/status/2028649088594436225",
    },
    {
        "posted_at_utc": "2026-03-04 21:30:47",
        "kind": "Hard reset",
        "audience": "Plus and Pro",
        "summary": "Fix and compensation after the 2x promotional limit did not apply to about 9% of users.",
        "source_url": "https://x.com/thsottiaux/status/2029308599835738218",
    },
    {
        "posted_at_utc": "2026-03-08 02:42:13",
        "kind": "Hard reset",
        "audience": "Plus and Pro",
        "summary": "A proactive reset while investigating accumulating reports of unusually fast limit consumption.",
        "source_url": "https://x.com/thsottiaux/status/2030474136024400173",
    },
    {
        "posted_at_utc": "2026-03-10 03:51:43",
        "kind": "Reset announced",
        "audience": "Codex",
        "summary": "A reset was promised after the incident was resolved and the service remained stable for several hours.",
        "source_url": "https://x.com/thsottiaux/status/2031216405266481489",
    },
    {
        "posted_at_utc": "2026-03-11 05:38:13",
        "kind": "Hard reset",
        "audience": "Codex",
        "summary": "The reset button was reported as pressed after service recovery.",
        "source_url": "https://x.com/thsottiaux/status/2031605592352313567",
    },
    {
        "posted_at_utc": "2026-03-27 01:52:28",
        "kind": "Hard reset",
        "audience": "All plans",
        "summary": "Issued to celebrate new plugins and because substantial time had passed since the previous reset.",
        "source_url": "https://x.com/thsottiaux/status/2037346989244096581",
    },
    {
        "posted_at_utc": "2026-04-01 07:48:39",
        "kind": "Hard reset",
        "audience": "All plans",
        "summary": "A proactive reset after the dashboard showed more users reaching limits for an unclear reason.",
        "source_url": "https://x.com/thsottiaux/status/2039248564967424483",
    },
    {
        "posted_at_utc": "2026-04-07 23:13:48",
        "kind": "Hard reset",
        "audience": "Codex",
        "summary": "Celebration reset for reaching three million weekly users.",
        "source_url": "https://x.com/thsottiaux/status/2041655710346572085",
    },
    {
        "posted_at_utc": "2026-04-09 17:51:28",
        "kind": "Hard reset",
        "audience": "Codex",
        "summary": "A second 3M celebration reset because the first landed too close to normal weekly renewals.",
        "source_url": "https://x.com/thsottiaux/status/2042299371602264319",
    },
    {
        "posted_at_utc": "2026-04-17 00:58:21",
        "kind": "Hard reset",
        "audience": "All plans",
        "summary": "Celebration reset for Codex's first anniversary and new feature launches.",
        "source_url": "https://x.com/thsottiaux/status/2044943514832871564",
    },
    {
        "posted_at_utc": "2026-04-20 23:15:21",
        "kind": "Reset announced",
        "audience": "Codex",
        "summary": "A compensation reset was promised after recovery from an outage of about ten minutes.",
        "source_url": "https://x.com/thsottiaux/status/2046367145588916687",
    },
    {
        "posted_at_utc": "2026-04-28 05:28:43",
        "kind": "Hard reset",
        "audience": "All paid plans",
        "summary": "A good-week and GPT-5.5-building celebration; some users later reported incomplete weekly restoration.",
        "source_url": "https://x.com/thsottiaux/status/2048997818673537399",
    },
    {
        "posted_at_utc": "2026-05-16 17:51:03",
        "kind": "Hard reset",
        "audience": "All paid plans",
        "summary": "Weekend usage reset.",
        "source_url": "https://x.com/thsottiaux/status/2055707616605835333",
    },
    {
        "posted_at_utc": "2026-05-23 20:14:35",
        "kind": "Hard reset",
        "audience": "All accounts",
        "summary": "Compensation after rolling back an optimization that reduced cache hits during long-session compaction.",
        "source_url": "https://x.com/thsottiaux/status/2058280452851638313",
    },
    {
        "posted_at_utc": "2026-05-31 15:25:06",
        "kind": "Hard reset",
        "audience": "Paid ChatGPT subscriptions",
        "summary": "Both weekly and time-based limits were explicitly restored to 100%.",
        "source_url": "https://x.com/thsottiaux/status/2061106703446450392",
    },
    {
        "posted_at_utc": "2026-06-04 00:25:58",
        "kind": "Hard reset",
        "audience": "All paid plans",
        "summary": "Compensation for three reliability incidents during the preceding 24 hours.",
        "source_url": "https://x.com/thsottiaux/status/2062329981548802523",
    },
    {
        "posted_at_utc": "2026-06-18 00:10:10",
        "kind": "Hard + banked",
        "audience": "Paid Codex plans",
        "summary": "An immediate full reset and one reset-bank coupon were granted together.",
        "source_url": "https://x.com/thsottiaux/status/2067399435009622521",
    },
    {
        "posted_at_utc": "2026-06-26 23:39:48",
        "kind": "Hard reset",
        "audience": "All Codex users",
        "summary": "A free reset while mitigations were applied and monitoring continued.",
        "source_url": "https://x.com/thsottiaux/status/2070653282440405046",
    },
    {
        "posted_at_utc": "2026-06-28 23:54:07",
        "kind": "Hard reset",
        "audience": "All Codex users",
        "summary": "Immediate reset during an abnormal-consumption investigation; existing banked resets were retained.",
        "source_url": "https://x.com/thsottiaux/status/2071381664853319742",
    },
    {
        "posted_at_utc": "2026-06-29 23:39:41",
        "kind": "Hard + banked",
        "audience": "Codex users",
        "summary": "A full reset within an hour plus one additional banked reset.",
        "source_url": "https://x.com/thsottiaux/status/2071740419030053227",
    },
    {
        "posted_at_utc": "2026-07-09 21:24:11",
        "kind": "Full reset",
        "audience": "Work and Codex",
        "summary": "A full usage reset within an hour, announced with a joke about a new teammate pressing the button.",
        "source_url": "https://x.com/thsottiaux/status/2075330198887940337",
    },
    {
        "posted_at_utc": "2026-07-10 17:59:43",
        "kind": "Reset + follow-up",
        "audience": "Work and Codex",
        "summary": "The first reset completed and another reset was promised later the same day.",
        "source_url": "https://x.com/thsottiaux/status/2075641131002700120",
    },
    {
        "posted_at_utc": "2026-07-11 05:54:25",
        "kind": "Hard reset",
        "audience": "Work and Codex",
        "summary": "A global reset in response to unprecedented traffic growth, expected to land within 30 minutes.",
        "source_url": "https://x.com/thsottiaux/status/2075820987833274448",
    },
    {
        "posted_at_utc": "2026-07-12 21:28:59",
        "kind": "Banked reset",
        "audience": "500K users",
        "summary": "Web and mobile support launch plus compensation for failed reset redemptions.",
        "source_url": "https://x.com/thsottiaux/status/2076418567143408112",
    },
    {
        "posted_at_utc": "2026-07-13 18:29:31",
        "kind": "Banked reset",
        "audience": "All Work and Codex accounts",
        "summary": "A seven-million-active-users milestone credit.",
        "source_url": "https://x.com/thsottiaux/status/2076735790567338203",
    },
    {
        "posted_at_utc": "2026-07-14 19:34:54",
        "kind": "Hard reset",
        "audience": "All Work and Codex users",
        "summary": "Eight-million-active-users celebration; the five-hour limit remained removed.",
        "source_url": "https://x.com/thsottiaux/status/2077114635308986427",
    },
    {
        "posted_at_utc": "2026-07-16 04:14:09",
        "kind": "Hard reset",
        "audience": "Work and Codex",
        "summary": "Nine-million-active-users celebration restoring weekly usage to 100%.",
        "source_url": "https://x.com/thsottiaux/status/2077607697487188198",
    },
    {
        "posted_at_utc": "2026-07-18 03:28:22",
        "kind": "Hard reset",
        "audience": "All paid Work and Codex users",
        "summary": "Weekend reset celebrating infrastructure scaling and rapid product improvements.",
        "source_url": "https://x.com/thsottiaux/status/2078320950488297917",
    },
    {
        "posted_at_utc": "2026-07-21 16:47:15",
        "kind": "Hard reset",
        "audience": "Paid Work and Codex users",
        "summary": "Ten-million-active-users reset, announced to land within the next hour.",
        "source_url": "https://x.com/thsottiaux/status/2079609157934886975",
    },
]


RESET_NOTES = [
    {
        "topic": "Scope can vary",
        "detail": (
            "Some users reported receiving only a five-hour restoration, or no "
            "weekly restoration, after announcements described as applying to all."
        ),
    },
    {
        "topic": "Banked vs hard reset",
        "detail": (
            "A banked reset is user-triggered later; a hard reset immediately "
            "replaces the current allowance and can discard unused headroom."
        ),
    },
    {
        "topic": "Expiration",
        "detail": (
            "OpenAI says banked Codex referral resets normally must be used within "
            "30 days unless the offer states otherwise."
        ),
    },
    {
        "topic": "Observed balances",
        "detail": (
            "Public replies reported different banked-reset balances, so the "
            "archive does not claim a universal maximum."
        ),
    },
]

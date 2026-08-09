"""Verbatim Ghostfolio GET /api/v1/activities payloads.

The BTC position reproduces the real-world gap exactly: one BUY of 0.1580 plus
two INTEREST rows totalling 0.0255.  Ghostfolio's getFactor() gives INTEREST a
factor of 0, so the reported position is 0.1580 against a Kraken balance of
0.1835.
"""

GHOST_ACCOUNT_ID = "9d2f1c44-6e8b-4a3f-8f11-0b7c5d2e91aa"
GHOST_OTHER_ACCOUNT_ID = "1a2b3c4d-5e6f-4708-9a0b-1c2d3e4f5a6b"

ACTIVITY_BTC_BUY = {
    "id": "b1f0e7c2-1111-4aaa-9bbb-0c1d2e3f4a50",
    "accountId": GHOST_ACCOUNT_ID,
    "comment": "KRAKEN#TQXY7Z-4KLMN-8PQRST",
    "currency": "USD",
    "date": "2025-03-16T00:01:01.553Z",
    "fee": 1.33474,
    "quantity": 0.158,
    "type": "BUY",
    "unitPrice": 83421.0,
    "symbol": "BTCUSD",
    "dataSource": "YAHOO",
    "SymbolProfile": {"symbol": "BTCUSD", "dataSource": "YAHOO", "currency": "USD"},
}

# Legacy staking rows: quantity is present in the payload but Ghostfolio
# discards it, and unitPrice 0 means they carry no value either.  Inert.
ACTIVITY_BTC_INTEREST_1 = {
    "id": "b1f0e7c2-2222-4aaa-9bbb-0c1d2e3f4a51",
    "accountId": GHOST_ACCOUNT_ID,
    "comment": "KRAKEN#STAKE#LZQ4NW-8HTGR-2MXPVK",
    "currency": "USD",
    "date": "2025-02-01T00:00:00.882Z",
    "fee": 0,
    "quantity": 0.01,
    "type": "INTEREST",
    "unitPrice": 0,
    "symbol": "BTCUSD",
    "dataSource": "YAHOO",
    "SymbolProfile": {"symbol": "BTCUSD", "dataSource": "YAHOO", "currency": "USD"},
}

ACTIVITY_BTC_INTEREST_2 = {
    "id": "b1f0e7c2-3333-4aaa-9bbb-0c1d2e3f4a52",
    "accountId": GHOST_ACCOUNT_ID,
    "comment": "KRAKEN#STAKE#LKJH2M-QW3RT-9ZXCVB",
    "currency": "USD",
    "date": "2025-04-01T00:00:00.441Z",
    "fee": 0,
    "quantity": 0.0155,
    "type": "INTEREST",
    "unitPrice": 0,
    "symbol": "BTCUSD",
    "dataSource": "YAHOO",
    "SymbolProfile": {"symbol": "BTCUSD", "dataSource": "YAHOO", "currency": "USD"},
}

ACTIVITY_DOT_BUY = {
    "id": "b1f0e7c2-4444-4aaa-9bbb-0c1d2e3f4a53",
    "accountId": GHOST_ACCOUNT_ID,
    "comment": "KRAKEN#TDOT11-22333-44444X",
    "currency": "USD",
    "date": "2025-01-02T00:00:00.000Z",
    "fee": 0.52,
    "quantity": 13.0,
    "type": "BUY",
    "unitPrice": 6.81,
    "symbol": "DOTUSD",
    "dataSource": "YAHOO",
    "SymbolProfile": {"symbol": "DOTUSD", "dataSource": "YAHOO", "currency": "USD"},
}

# Held in a different Ghostfolio account.  Reconcile must exclude it, or a
# position held elsewhere silently cancels out the drift being measured.
ACTIVITY_BTC_OTHER_ACCOUNT = {
    "id": "b1f0e7c2-5555-4aaa-9bbb-0c1d2e3f4a54",
    "accountId": GHOST_OTHER_ACCOUNT_ID,
    "comment": None,
    "currency": "USD",
    "date": "2025-05-01T00:00:00.000Z",
    "fee": 0,
    "quantity": 2.0,
    "type": "BUY",
    "unitPrice": 90000.0,
    "symbol": "BTCUSD",
    "dataSource": "YAHOO",
    "SymbolProfile": {"symbol": "BTCUSD", "dataSource": "YAHOO", "currency": "USD"},
}

# Only a SymbolProfile, no top-level symbol key.
ACTIVITY_SYMBOL_PROFILE_ONLY = {
    "id": "b1f0e7c2-6666-4aaa-9bbb-0c1d2e3f4a55",
    "accountId": GHOST_ACCOUNT_ID,
    "comment": "KRAKEN#TSOL11-22333-44444X",
    "currency": "USD",
    "date": "2025-05-02T00:00:00.000Z",
    "fee": 0,
    "quantity": 5.0,
    "type": "BUY",
    "unitPrice": 140.0,
    "dataSource": "YAHOO",
    "SymbolProfile": {"symbol": "SOLUSD", "dataSource": "YAHOO", "currency": "USD"},
}

# A legacy staking row on a Ghostfolio MANUAL asset profile. Non-investment
# activity types are forced onto DataSource.MANUAL with a UUID symbol, so each
# old INTEREST row sits on a synthetic profile unrelated to any Kraken asset.
ACTIVITY_LEGACY_MANUAL_PROFILE = {
    "id": "b1f0e7c2-7777-4aaa-9bbb-0c1d2e3f4a56",
    "accountId": GHOST_ACCOUNT_ID,
    "comment": "KRAKEN#STAKE#LOLD11-22333-44444X",
    "currency": "USD",
    "date": "2024-11-01T00:00:00.000Z",
    "fee": 0,
    "quantity": 0.004,
    "type": "INTEREST",
    "unitPrice": 0,
    "symbol": "3f2a91c4-7b6d-4e18-9c05-2ab8d7e6f130",
    "dataSource": "MANUAL",
    "SymbolProfile": {"symbol": "3f2a91c4-7b6d-4e18-9c05-2ab8d7e6f130",
                      "dataSource": "MANUAL", "currency": "USD"},
}

ACTIVITY_LEGACY_GF_PROFILE = {
    "id": "b1f0e7c2-8888-4aaa-9bbb-0c1d2e3f4a57",
    "accountId": GHOST_ACCOUNT_ID,
    "comment": "KRAKEN#STAKE#LOLD22-33444-55555X",
    "currency": "USD",
    "date": "2024-12-01T00:00:00.000Z",
    "fee": 0,
    "quantity": 0.002,
    "type": "INTEREST",
    "unitPrice": 0,
    "symbol": "GF_LEGACY_STAKE",
    "dataSource": "MANUAL",
    "SymbolProfile": {"symbol": "GF_LEGACY_STAKE", "dataSource": "MANUAL",
                      "currency": "USD"},
}

GHOST_ACTIVITIES = [
    ACTIVITY_BTC_BUY,
    ACTIVITY_BTC_INTEREST_1,
    ACTIVITY_BTC_INTEREST_2,
    ACTIVITY_DOT_BUY,
]

GHOST_ACCOUNTS = {
    "accounts": [
        {
            "id": GHOST_ACCOUNT_ID,
            "name": "Kraken",
            "currency": "USD",
            "balance": 0.0,
            "platformId": "d1b2c3a4-1111-2222-3333-444455556666",
        },
        {
            "id": GHOST_OTHER_ACCOUNT_ID,
            "name": "Bitbox",
            "currency": "USD",
            "balance": 0.0,
            "platformId": None,
        },
    ]
}


def activities_response(activities, skip=0, take=500):
    """Wrap in the Ghostfolio envelope: {"activities": [...], "count": N}.

    `count` is the total ignoring pagination, which is what the real endpoint
    returns and what the skip/take loop relies on to terminate.
    """
    return {
        "activities": activities[skip:skip + take],
        "count": len(activities),
    }


def activity_from_import(payload, activity_id):
    """Turn an activity this script imported into one Ghostfolio would return.

    Used by the idempotency test: the fake server echoes back what was POSTed
    so the second run sees its own comments and imports nothing.
    """
    echoed = dict(payload)
    echoed["id"] = activity_id
    echoed["SymbolProfile"] = {
        "symbol": payload.get("symbol", ""),
        "dataSource": payload.get("dataSource", "YAHOO"),
        "currency": payload.get("currency", "USD"),
    }
    return echoed

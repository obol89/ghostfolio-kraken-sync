"""Verbatim Kraken API payloads used across the test suite.

Ledger responses are keyed by ledger id (LXXXXXX-XXXXX-XXXXXX).  `refid` is
shared across the legs of one logical event and is NOT unique, which is why
deduplication comments key on the map key rather than on refid.

Amounts are strings at Kraken's native precision.  The BTC reward amounts sum
to exactly 0.0255 so the reconcile fixtures reproduce the real-world gap this
change was written to close.
"""

# ---------------------------------------------------------------------------
# Ledger entries
# ---------------------------------------------------------------------------

# Legacy Kraken Staking reward, canonical case.
LEDGER_STAKING_REWARD_DOT = {
    "L4UESK-KG3EQ-UFO4T5": {
        "refid": "RUZDPMR-KJIYQV-EMBIQU",
        "time": 1735689600.1234,
        "type": "staking",
        "subtype": "",
        "aclass": "currency",
        "asset": "DOT.S",
        "amount": "0.5000000000",
        "fee": "0.0000000000",
        "balance": "12.5000000000",
    }
}

# Legacy staking reward on the BTC opt-in rewards balance (.M).
LEDGER_STAKING_REWARD_BTC = {
    "LZQ4NW-8HTGR-2MXPVK": {
        "refid": "RKF7XQA-2LMNBV-CXZQWE",
        "time": 1738368000.8821,
        "type": "staking",
        "subtype": "",
        "aclass": "currency",
        "asset": "XXBT.M",
        "amount": "0.0100000000",
        "fee": "0.0000000000",
        "balance": "0.0100000000",
    }
}

# Modern Kraken Earn reward (.F).  The current script never fetches this:
# `earn` is not a member of the REST Ledgers `type` filter enum.
LEDGER_EARN_REWARD_BTC = {
    "LKJH2M-QW3RT-9ZXCVB": {
        "refid": "RWD4NPQ-7YTREW-1ASDFG",
        "time": 1743465600.4417,
        "type": "earn",
        "subtype": "reward",
        "aclass": "currency",
        "asset": "XXBT.F",
        "amount": "0.0155000000",
        "fee": "0.0000000000",
        "balance": "0.0255000000",
    }
}

# A reward carrying a fee.  Kraken settles as balance + amount - fee, and the
# fee is denominated in the entry's own asset, so the net quantity credited is
# 0.0099 BTC rather than 0.0100.
LEDGER_EARN_REWARD_WITH_FEE = {
    "LFEE1A-2BCDE-3FGHIJ": {
        "refid": "RFEE001-AABBCC-DDEEFF",
        "time": 1743552000.1000,
        "type": "earn",
        "subtype": "reward",
        "aclass": "currency",
        "asset": "XXBT.F",
        "amount": "0.0100000000",
        "fee": "0.0001000000",
        "balance": "0.0355000000",
    }
}

# Earn allocation and deallocation.  Each is two legs sharing one refid on the
# same normalized asset with opposite signs, so both net to zero.
LEDGER_EARN_ALLOCATION_PAIR = {
    "LQ7T4A-2NMBX-P8YHRD": {
        "refid": "RA1B2C3-D4E5F6-G7H8I9",
        "time": 1741046400.1100,
        "type": "earn",
        "subtype": "allocation",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "-0.0500000000",
        "fee": "0.0000000000",
        "balance": "0.1080000000",
    },
    "LW9E2R-5TYUI-K3JHGF": {
        "refid": "RA1B2C3-D4E5F6-G7H8I9",
        "time": 1741046400.1100,
        "type": "earn",
        "subtype": "allocation",
        "aclass": "currency",
        "asset": "XXBT.B",
        "amount": "0.0500000000",
        "fee": "0.0000000000",
        "balance": "0.0500000000",
    },
}

LEDGER_EARN_DEALLOCATION_PAIR = {
    "LZ3X9K-7VBNM-Q2WERT": {
        "refid": "RD9Z8Y7-X6W5V4-U3T2S1",
        "time": 1744243200.6650,
        "type": "earn",
        "subtype": "deallocation",
        "aclass": "currency",
        "asset": "XXBT.B",
        "amount": "-0.0500000000",
        "fee": "0.0000000000",
        "balance": "0.0000000000",
    },
    "LP5M8N-3QWAS-Z1XCDE": {
        "refid": "RD9Z8Y7-X6W5V4-U3T2S1",
        "time": 1744243200.6650,
        "type": "earn",
        "subtype": "deallocation",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0500000000",
        "fee": "0.0000000000",
        "balance": "0.1580000000",
    },
}

# Earn migration (legacy Staking -> Earn), also a paired internal move.
LEDGER_EARN_MIGRATION_PAIR = {
    "LMIG1Q-2WERT-3YUIOP": {
        "refid": "RMIG001-112233-445566",
        "time": 1740441600.0000,
        "type": "earn",
        "subtype": "migration",
        "aclass": "currency",
        "asset": "DOT.S",
        "amount": "-13.0000000000",
        "fee": "0.0000000000",
        "balance": "0.0000000000",
    },
    "LMIG2A-4SDFG-5HJKLZ": {
        "refid": "RMIG001-112233-445566",
        "time": 1740441600.0000,
        "type": "earn",
        "subtype": "migration",
        "aclass": "currency",
        "asset": "DOT.B",
        "amount": "13.0000000000",
        "fee": "0.0000000000",
        "balance": "13.0000000000",
    },
}

# Airdrop credit (BABY / Babylon).
#
# CAVEAT: it is not confirmed that Kraken emits a literal type "airdrop".
# Some distributions arrive as "adjustment" or "credit", which is exactly why
# the classifier maps those to UNKNOWN (reported) rather than skipping them.
LEDGER_AIRDROP_BABY = {
    "LB4BY9-XKQ2M-7NPRTV": {
        "refid": "RAIRDRP-BABY01-9K3MQZ",
        "time": 1745798400.0000,
        "type": "airdrop",
        "subtype": "",
        "aclass": "currency",
        "asset": "BABY",
        "amount": "37.5000000000",
        "fee": "0.0000000000",
        "balance": "37.5000000000",
    }
}

# An airdrop filed under type `deposit` with subtype `airdrop`.  Subtype is
# matched before the type dispatch precisely so this is not swallowed by
# SKIP_CRYPTO_TRANSFERS.
LEDGER_AIRDROP_AS_DEPOSIT = {
    "LDEPAI-9ZXCV-1QWERT": {
        "refid": "RDEPAIR-BABY02-8J2LPY",
        "time": 1745884800.0000,
        "type": "deposit",
        "subtype": "airdrop",
        "aclass": "currency",
        "asset": "BABY",
        "amount": "5.0000000000",
        "fee": "0.0000000000",
        "balance": "42.5000000000",
    }
}

# Transfer spot -> earn.  Two legs that must net to zero.
LEDGER_TRANSFER_SPOT_TO_EARN = {
    "LT8H3G-9WQZX-4MNBVC": {
        "refid": "RTRF551-88QWER-31ZXCV",
        "time": 1739577600.2244,
        "type": "transfer",
        "subtype": "spottostaking",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "-0.0300000000",
        "fee": "0.0000000000",
        "balance": "0.1280000000",
    },
    "LR2K7J-1PLOK-6IJUYH": {
        "refid": "RTRF551-88QWER-31ZXCV",
        "time": 1739577600.2244,
        "type": "transfer",
        "subtype": "stakingfromspot",
        "aclass": "currency",
        "asset": "XXBT.B",
        "amount": "0.0300000000",
        "fee": "0.0000000000",
        "balance": "0.0300000000",
    },
}

# Only the positive leg of a spot -> earn transfer, as happens when SYNC_SINCE
# splits the pair across the fetch boundary.  The structural netting pre-pass
# cannot see the missing leg, so the subtype must catch it on its own.
LEDGER_TRANSFER_STRADDLE_LEG = {
    "LR2K7J-1PLOK-6IJUYH": dict(LEDGER_TRANSFER_SPOT_TO_EARN["LR2K7J-1PLOK-6IJUYH"])
}

# A trade, two legs sharing the trade id as refid.  The two legs are on
# DIFFERENT assets, which is what keeps the netting rule from flagging them.
# The quote leg is USDC, the pair behind the historical ISO-4217 rejection.
LEDGER_TRADE_XBTUSDC = {
    "LTRD4Q-6ZXWE-8CVBNM": {
        "refid": "TQXY7Z-4KLMN-8PQRST",
        "time": 1742083261.5537,
        "type": "trade",
        "subtype": "",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0100000000",
        "fee": "0.0000000000",
        "balance": "0.1580000000",
    },
    "LTRD5W-7ASDF-9GHJKL": {
        "refid": "TQXY7Z-4KLMN-8PQRST",
        "time": 1742083261.5537,
        "type": "trade",
        "subtype": "",
        "aclass": "currency",
        "asset": "USDC",
        "amount": "-834.2100000000",
        "fee": "1.3347360000",
        "balance": "0.0000000000",
    },
}

LEDGER_DEPOSIT_BTC = {
    "LDEP1Q-2WSXC-3EDCVF": {
        "refid": "RDEP001-QQWWEE-RRTTYY",
        "time": 1736380800.0000,
        "type": "deposit",
        "subtype": "",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0250000000",
        "fee": "0.0000000000",
        "balance": "0.1330000000",
    }
}

# A deposit that funded a later conversion. With SKIP_CRYPTO_TRANSFERS on it
# is never imported, so Ghostfolio sees the sale but not the funding.
LEDGER_DEPOSIT_DOT = {
    "LDEPDT-9ZXCV-1QWERT": {
        "refid": "RDEPDOT-445566-778899",
        "time": 1736294400.0000,
        "type": "deposit",
        "subtype": "",
        "aclass": "currency",
        "asset": "DOT",
        "amount": "5.0000000000",
        "fee": "0.0000000000",
        "balance": "5.0000000000",
    }
}

LEDGER_WITHDRAWAL_BTC = {
    "LWDR1A-2SDFG-3HJKLQ": {
        "refid": "RWDR001-ZZXXCC-VVBBNN",
        "time": 1736467200.0000,
        "type": "withdrawal",
        "subtype": "",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "-0.0050000000",
        "fee": "0.0000500000",
        "balance": "0.1280000000",
    }
}

# A withdrawal with Kraken's real BTC network fee. The balance falls by
# amount + fee; importing only abs(amount) leaves the fee behind, which is
# what produced a 0.0000222 residual across two withdrawals.
LEDGER_WITHDRAWAL_BTC_WITH_FEE = {
    "LWDRFE-7UJMI-8IKOLP": {
        "refid": "RWDRFEE-112233-445566",
        "time": 1736553600.0000,
        "type": "withdrawal",
        "subtype": "",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "-0.0200000000",
        "fee": "0.0000111000",
        "balance": "0.1079889000",
    }
}

# A deposit whose fee is taken out of what arrives, the mirror image.
LEDGER_DEPOSIT_BTC_WITH_FEE = {
    "LDEPFE-9OLKM-0PJUYH": {
        "refid": "RDEPFEE-667788-990011",
        "time": 1736640000.0000,
        "type": "deposit",
        "subtype": "",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0100000000",
        "fee": "0.0000250000",
        "balance": "0.1179639000",
    }
}

LEDGER_DEPOSIT_FIAT = {
    "LDEPFI-4RFVB-5TGBNH": {
        "refid": "RDEPFIA-778899-AABBCC",
        "time": 1736294400.0000,
        "type": "deposit",
        "subtype": "",
        "aclass": "currency",
        "asset": "ZUSD",
        "amount": "1000.0000",
        "fee": "0.0000",
        "balance": "1042.5500",
    }
}

# A reward paid in fiat.  Cash is handled by the balance update, not as a
# position, so this must not become an activity.
LEDGER_STAKING_REWARD_FIAT = {
    "LFIATR-6YHNM-7UJMIK": {
        "refid": "RFIATRW-001122-334455",
        "time": 1744329600.0000,
        "type": "staking",
        "subtype": "",
        "aclass": "currency",
        "asset": "ZUSD",
        "amount": "1.2500",
        "fee": "0.0000",
        "balance": "1043.8000",
    }
}

# A negative staking entry: an unstake or a clawback, not a reward.
LEDGER_STAKING_NEGATIVE = {
    "LNEG1Z-2XCVB-3NMQWE": {
        "refid": "RNEG001-556677-889900",
        "time": 1744416000.0000,
        "type": "staking",
        "subtype": "",
        "aclass": "currency",
        "asset": "DOT.S",
        "amount": "-0.2500000000",
        "fee": "0.0000000000",
        "balance": "12.7500000000",
    }
}

# An earn subtype the classifier has never seen.
LEDGER_EARN_UNKNOWN_SUBTYPE = {
    "LNEW1Q-2WERT-3YUIOP": {
        "refid": "RNEW001-AAA111-BBB222",
        "time": 1746057600.0000,
        "type": "earn",
        "subtype": "autocompound",
        "aclass": "currency",
        "asset": "XXBT.F",
        "amount": "0.0011000000",
        "fee": "0.0000000000",
        "balance": "0.0366000000",
    }
}

# A bare transfer with no subtype: Kraken also uses this for account-to-account
# moves and delisting sweep-outs, which do change holdings, so it must not be
# assumed internal. Observed on a real account as a small USDC credit.
LEDGER_TRANSFER_NO_SUBTYPE = {
    "LTRFBA-4RETY-5UIOPA": {
        "refid": "RTRFBAR-121212-343434",
        "time": 1746144000.0000,
        "type": "transfer",
        "subtype": "",
        "aclass": "currency",
        "asset": "USDC",
        "amount": "2.0000000000",
        "fee": "0.0000000000",
        "balance": "2.0000000000",
    }
}

# Futures transfer. Kraken's Ledgers covers the spot wallet only and Balance
# excludes the futures wallet, so only this leg is ever fetched - the money
# really does leave the holdings being reconciled.
LEDGER_TRANSFER_SPOT_FROM_FUTURES = {
    "LFUTIN-3EDCV-4RFVBG": {
        "refid": "RFUT001-INBOUND-00001",
        "time": 1746316800.0000,
        "type": "transfer",
        "subtype": "spotfromfutures",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0075000000",
        "fee": "0.0000000000",
        "balance": "0.1907780200",
    }
}

LEDGER_TRANSFER_SPOT_TO_FUTURES = {
    "LFUTOU-5TGBN-6YHNMJ": {
        "refid": "RFUT002-OUTBOUND-0002",
        "time": 1746403200.0000,
        "type": "transfer",
        "subtype": "spottofutures",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "-0.0030000000",
        "fee": "0.0000000000",
        "balance": "0.1877780200",
    }
}

# A delisting sweep-out, observed on a real account as LUNA2 leaving.
LEDGER_TRANSFER_BARE_NEGATIVE = {
    "LTRFOU-6YHNM-7UJMIK": {
        "refid": "RTRFOUT-565656-787878",
        "time": 1746230400.0000,
        "type": "transfer",
        "subtype": "",
        "aclass": "currency",
        "asset": "LUNA2",
        "amount": "-1.5000000000",
        "fee": "0.0000000000",
        "balance": "0.0000000000",
    }
}


# ---------------------------------------------------------------------------
# Kraken Convert / instant buy-sell / dust sweeping (types spend and receive)
#
# Both legs of a conversion share one refid. These are ledger-only events:
# their refids do not appear in TradesHistory, so they dedupe purely on their
# own comments.
# ---------------------------------------------------------------------------

# Instant buy: fiat out, crypto in. 1404.33 CHF for 0.0252729 BTC, which is
# the one conversion shape carrying a real price.
LEDGER_CONVERT_FIAT_TO_CRYPTO = {
    "LCNVSP-CHF01-AAAAAA": {
        "refid": "RCNV001-BUYBTC-000001",
        "time": 1742342400.1234,
        "type": "spend",
        "subtype": "",
        "aclass": "currency",
        "asset": "CHF",
        "amount": "-1404.3300",
        "fee": "0.0000",
        "balance": "95.6700",
    },
    "LCNVRC-XBT01-BBBBBB": {
        "refid": "RCNV001-BUYBTC-000001",
        "time": 1742342400.1234,
        "type": "receive",
        "subtype": "",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0252729000",
        "fee": "0.0000000000",
        "balance": "0.1832729000",
    },
}

# Instant sell: crypto out, fiat in.
LEDGER_CONVERT_CRYPTO_TO_FIAT = {
    "LCNVSP-XRP01-CCCCCC": {
        "refid": "RCNV002-SELXRP-000002",
        "time": 1742428800.5500,
        "type": "spend",
        "subtype": "",
        "aclass": "currency",
        "asset": "XXRP",
        "amount": "-500.0000000000",
        "fee": "0.0000000000",
        "balance": "0.0000000000",
    },
    "LCNVRC-CHF02-DDDDDD": {
        "refid": "RCNV002-SELXRP-000002",
        "time": 1742428800.5500,
        "type": "receive",
        "subtype": "",
        "aclass": "currency",
        "asset": "CHF",
        "amount": "1234.5600",
        "fee": "0.0000",
        "balance": "1330.2300",
    },
}

# Crypto to crypto: no fiat leg, so no price is available.
LEDGER_CONVERT_CRYPTO_TO_CRYPTO = {
    "LCNVSP-ATOM1-EEEEEE": {
        "refid": "RCNV003-ATOMSOL-00003",
        "time": 1742515200.7700,
        "type": "spend",
        "subtype": "",
        "aclass": "currency",
        "asset": "ATOM",
        "amount": "-12.5000000000",
        "fee": "0.0000000000",
        "balance": "0.0000000000",
    },
    "LCNVRC-SOL01-FFFFFF": {
        "refid": "RCNV003-ATOMSOL-00003",
        "time": 1742515200.7700,
        "type": "receive",
        "subtype": "",
        "aclass": "currency",
        "asset": "SOL",
        "amount": "0.8500000000",
        "fee": "0.0000000000",
        "balance": "5.8500000000",
    },
}

# Dust sweeping is many-to-one: several spend legs collapsing into one
# receive leg, all under a single refid.
LEDGER_CONVERT_DUSTSWEEP = {
    "LDUSTS-TRX01-GGGGGG": {
        "refid": "RDUST01-SWEEP1-000004",
        "time": 1742601600.0000,
        "type": "spend",
        "subtype": "dustsweeping",
        "aclass": "currency",
        "asset": "TRX",
        "amount": "-1.2300000000",
        "fee": "0.0000000000",
        "balance": "0.0000000000",
    },
    "LDUSTS-ALGO1-HHHHHH": {
        "refid": "RDUST01-SWEEP1-000004",
        "time": 1742601600.0000,
        "type": "spend",
        "subtype": "dustsweeping",
        "aclass": "currency",
        "asset": "ALGO",
        "amount": "-0.4500000000",
        "fee": "0.0000000000",
        "balance": "0.0000000000",
    },
    "LDUSTR-XBT02-IIIIII": {
        "refid": "RDUST01-SWEEP1-000004",
        "time": 1742601600.0000,
        "type": "receive",
        "subtype": "dustsweeping",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0000051200",
        "fee": "0.0000000000",
        "balance": "0.1832780200",
    },
}

# Fiat in a settlement-pending state. Kraken hangs suffixes beyond the
# staking ones off a balance, and .HOLD is not in the strip list, so a naive
# fiat check treats EUR.HOLD as an unresolvable crypto asset.
LEDGER_CONVERT_EUR_HOLD = {
    "LCNVSP-EURH1-MMMMMM": {
        "refid": "RCNV006-EURHOLD-0007",
        "time": 1742947200.0000,
        "type": "spend",
        "subtype": "",
        "aclass": "currency",
        "asset": "EUR.HOLD",
        "amount": "-250.0000",
        "fee": "0.0000",
        "balance": "0.0000",
    },
    "LCNVRC-SOL02-NNNNNN": {
        "refid": "RCNV006-EURHOLD-0007",
        "time": 1742947200.0000,
        "type": "receive",
        "subtype": "",
        "aclass": "currency",
        "asset": "SOL",
        "amount": "1.5000000000",
        "fee": "0.0000000000",
        "balance": "7.3500000000",
    },
}

# A pure currency exchange: cash on both sides, no position involved.
LEDGER_CONVERT_FIAT_TO_FIAT = {
    "LCNVSP-CHF03-JJJJJJ": {
        "refid": "RCNV004-CHFUSD-00005",
        "time": 1742688000.0000,
        "type": "spend",
        "subtype": "",
        "aclass": "currency",
        "asset": "CHF",
        "amount": "-100.0000",
        "fee": "0.0000",
        "balance": "1230.2300",
    },
    "LCNVRC-USD01-KKKKKK": {
        "refid": "RCNV004-CHFUSD-00005",
        "time": 1742688000.0000,
        "type": "receive",
        "subtype": "",
        "aclass": "currency",
        "asset": "ZUSD",
        "amount": "112.3000",
        "fee": "0.0000",
        "balance": "1154.8500",
    },
}

# Only one leg inside the fetch window: a conversion straddling SYNC_SINCE.
# It cannot be priced or even given a direction from itself alone.
LEDGER_CONVERT_LONE_SPEND = {
    "LCNVSP-LONE1-LLLLLL": {
        "refid": "RCNV005-STRADDLE-0006",
        "time": 1742774400.0000,
        "type": "spend",
        "subtype": "",
        "aclass": "currency",
        "asset": "CHF",
        "amount": "-250.0000",
        "fee": "0.0000",
        "balance": "904.8500",
    }
}

# Trade base legs whose fee Kraken charged in the BASE asset, taken from a
# real account. TradesHistory reports `vol` gross, so the imported activity is
# larger than the balance actually moved by. These three fees sum to
# 0.00002221 BTC, which was exactly the unexplained BTCUSD reconcile residual.
BASE_FEE_TOTAL = 0.00000141 + 0.00000501 + 0.00001579

LEDGER_TRADE_BASE_FEES = {
    "L6VL2G-WRZIR-QO2PXY": {
        "refid": "TBFEE01-AAAAAA-000001",
        "time": 1743120000.1000,
        "type": "trade",
        "subtype": "tradespot",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0035000000",
        "fee": "0.0000014100",
        "balance": "0.1615000000",
    },
    "L2WJB5-AC3HV-V7FJOL": {
        "refid": "TBFEE02-BBBBBB-000002",
        "time": 1743206400.2000,
        "type": "trade",
        "subtype": "tradespot",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0125000000",
        "fee": "0.0000050100",
        "balance": "0.1740000000",
    },
    "LVDOPO-SSHOG-W3RXFI": {
        "refid": "TBFEE03-CCCCCC-000003",
        "time": 1743292800.3000,
        "type": "trade",
        "subtype": "tradespot",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0400000000",
        "fee": "0.0000157900",
        "balance": "0.2140000000",
    },
}

# The TradesHistory entries those legs belong to. Only the pair matters here -
# it is what identifies which ledger leg is the base.
TRADES_WITH_BASE_FEES = {
    "TBFEE01-AAAAAA-000001": {"pair": "XXBTZUSD", "type": "buy", "time": 1743120000.1,
                              "price": "82000.0", "cost": "287.0", "fee": "0.0",
                              "vol": "0.00350000", "ordertype": "limit", "margin": "0.0"},
    "TBFEE02-BBBBBB-000002": {"pair": "XXBTZUSD", "type": "buy", "time": 1743206400.2,
                              "price": "83000.0", "cost": "1037.5", "fee": "0.0",
                              "vol": "0.01250000", "ordertype": "limit", "margin": "0.0"},
    "TBFEE03-CCCCCC-000003": {"pair": "XXBTZUSD", "type": "buy", "time": 1743292800.3,
                              "price": "84000.0", "cost": "3360.0", "fee": "0.0",
                              "vol": "0.04000000", "ordertype": "limit", "margin": "0.0"},
}

# A spot trade leg as it really appears in the ledger, with subtype tradespot.
LEDGER_TRADE_SPOT_SUBTYPE = {
    "LTRDSP-1QWER-2TYUIO": {
        "refid": "TSPOT01-ABCDEF-123456",
        "time": 1742860800.0000,
        "type": "trade",
        "subtype": "tradespot",
        "aclass": "currency",
        "asset": "XXBT",
        "amount": "0.0050000000",
        "fee": "0.0000000000",
        "balance": "0.1882780200",
    }
}

LEDGER_ADJUSTMENT = {
    "LADJ9X-2QWER-5TYUIO": {
        "refid": "RADJ001-11AAZZ-99QQPP",
        "time": 1746403200.0000,
        "type": "adjustment",
        "subtype": "",
        "aclass": "currency",
        "asset": "BABY",
        "amount": "0.7500000000",
        "fee": "0.0000000000",
        "balance": "38.2500000000",
    }
}

LEDGER_MARGIN = {
    "LMAR1Q-2WERT-3YUIOP": {
        "refid": "RMAR001-999888-777666",
        "time": 1746489600.0000,
        "type": "margin",
        "subtype": "",
        "aclass": "currency",
        "asset": "ZUSD",
        "amount": "-2.5000",
        "fee": "0.0000",
        "balance": "1041.3000",
    }
}

LEDGER_WHOLLY_UNKNOWN_TYPE = {
    "LWIB1Q-2WERT-3YUIOP": {
        "refid": "RWIB001-555444-333222",
        "time": 1746576000.0000,
        "type": "wibble",
        "subtype": "",
        "aclass": "currency",
        "asset": "BABY",
        "amount": "1.0000000000",
        "fee": "0.0000000000",
        "balance": "39.2500000000",
    }
}


def ledgers_response(*entry_dicts, count=None):
    """Wrap ledger fixtures in the Kraken envelope.

    Returns {"ledger": {...}, "count": N}, matching what
    POST /0/private/Ledgers puts in its `result` object.
    """
    merged = {}
    for entries in entry_dicts:
        merged.update(entries)
    return {"ledger": merged, "count": len(merged) if count is None else count}


def make_ledger_page(prefix, size, ledger_type="earn", subtype="reward",
                     asset="XXBT.F", amount="0.0001000000"):
    """Build `size` synthetic ledger entries with unique ids, for paging tests."""
    return {
        "L{0}{1:04d}-PAGED-ENTRY".format(prefix, index): {
            "refid": "R{0}{1:04d}-PAGED-PARENT".format(prefix, index),
            "time": 1740000000.0 + index,
            "type": ledger_type,
            "subtype": subtype,
            "aclass": "currency",
            "asset": asset,
            "amount": amount,
            "fee": "0.0000000000",
            "balance": "0.0000000000",
        }
        for index in range(size)
    }


# ---------------------------------------------------------------------------
# Balance
# ---------------------------------------------------------------------------

# BTC total = 0.1580 + 0.0000 + 0.0255 = 0.1835
# DOT total = 12.5 + 0.5 = 13.0
KRAKEN_BALANCES = {
    "XXBT": "0.1580000000",
    "XXBT.B": "0.0000000000",
    "XXBT.F": "0.0255000000",
    "DOT": "12.5000000000",
    "DOT.S": "0.5000000000",
    "BABY": "37.5000000000",
    "ZUSD": "1042.5500",
    "USDC": "0.0000000000",
}


# ---------------------------------------------------------------------------
# TradesHistory
# ---------------------------------------------------------------------------

KRAKEN_TRADES = {
    "TQXY7Z-4KLMN-8PQRST": {
        "ordertxid": "OZ4MPQ-7RSTU-1VWXYZ",
        "postxid": "TKH2SE-M7IF5-CFI7LT",
        "pair": "XBTUSDC",
        "time": 1742083261.5537,
        "type": "buy",
        "ordertype": "limit",
        "price": "83421.00000",
        "cost": "834.21000",
        "fee": "1.33474",
        "vol": "0.01000000",
        "margin": "0.00000",
        "misc": "",
    },
    "T7JHGF-2WERT-9ZXCVB": {
        "ordertxid": "OQ8WER-3TYUI-5OPASD",
        "postxid": "TKH2SE-M7IF5-CFI7LT",
        # Crypto quote: resolves to currency "BTC", which is not ISO 4217 and
        # is deliberately left failing rather than relabelled as USD.
        "pair": "ETHXBT",
        "time": 1742169661.2210,
        "type": "buy",
        "ordertype": "market",
        "price": "0.02981000",
        "cost": "0.02235750",
        "fee": "0.00005812",
        "vol": "0.75000000",
        "margin": "0.00000",
        "misc": "",
    },
}

TRADE_ZERO_VOLUME = {
    "TZERO1-2WERT-3YUIOP": {
        "ordertxid": "OZERO1-2WERT-3YUIOP",
        "pair": "XXBTZUSD",
        "time": 1742256000.0000,
        "type": "buy",
        "ordertype": "limit",
        "price": "83000.00000",
        "cost": "0.00000",
        "fee": "0.00000",
        "vol": "0.00000000",
        "margin": "0.00000",
        "misc": "",
    }
}


def trades_response(trades, count=None):
    """Wrap trade fixtures in the Kraken envelope."""
    return {"trades": trades, "count": len(trades) if count is None else count}


def make_trade_page(prefix, size):
    """Build `size` synthetic trades with unique txids, for paging tests."""
    return {
        "T{0}{1:04d}-PAGED-TRADE".format(prefix, index): {
            "ordertxid": "O{0}{1:04d}-PAGED-ORDER".format(prefix, index),
            "pair": "XXBTZUSD",
            "time": 1740000000.0 + index,
            "type": "buy",
            "ordertype": "limit",
            "price": "80000.00000",
            "cost": "80.00000",
            "fee": "0.10000",
            "vol": "0.00100000",
            "margin": "0.00000",
            "misc": "",
        }
        for index in range(size)
    }

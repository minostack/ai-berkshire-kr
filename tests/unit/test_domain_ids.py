# -*- coding: utf-8 -*-
"""`cvd_obv.domain.ids` — 정규화 직렬화와 ID 결정성.

Stage 0 DoD: "동일 입력·동일 버전·동일 설정의 replay가 byte-equivalent 핵심
feature와 같은 `signal_id`를 만든다."

여기서 검증하는 것은 두 가지다.
1. 같아야 하는 것이 같다(결정성).
2. **달라야 하는 것이 다르다.** 해시 입력 중 하나라도 무시되면 서로 다른
   신호가 같은 ID를 갖게 되고, 그때부터 모든 조인이 조용히 틀린다.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from cvd_obv.domain import (
    KST, ConfigError, canonical_config_hash, canonical_json, kst_trading_date,
    legacy_v15_config_hash, make_experiment_id, make_model_id, make_run_id,
    make_signal_id, make_universe_snapshot_id, normalize_security_id,
    parse_security_id, sha256_of, to_utc, utc_iso,
)

SIGNAL_AT = datetime(2026, 9, 16, 15, 35, tzinfo=KST)
CONFIG = {"period": 5, "bdays": 30, "index": "kospi200"}
CONFIG_HASH = canonical_config_hash(CONFIG)
UNIVERSE = make_universe_snapshot_id("KOSPI200", SIGNAL_AT, ["005930", "000660"])


# ─────────────────────────────────────────────────────────────
# canonical_json
# ─────────────────────────────────────────────────────────────

def test_키_순서가_달라도_같은_직렬화():
    assert canonical_json({"b": 1, "a": 2}) == canonical_json({"a": 2, "b": 1})


def test_공백_없이_직렬화한다():
    assert canonical_json({"a": 1, "b": [1, 2]}) == '{"a":1,"b":[1,2]}'


def test_한글은_이스케이프하지_않는다():
    assert canonical_json({"세션": "정규장"}) == '{"세션":"정규장"}'


def test_datetime은_UTC_ISO로_고정된다():
    assert canonical_json({"t": SIGNAL_AT}) == '{"t":"2026-09-16T06:35:00Z"}'


def test_같은_시각이면_표기_시간대가_달라도_같은_해시():
    utc_form = SIGNAL_AT.astimezone(timezone.utc)
    assert sha256_of({"t": SIGNAL_AT}) == sha256_of({"t": utc_form})


def test_date는_달력일_문자열():
    assert canonical_json({"d": date(2026, 9, 16)}) == '{"d":"2026-09-16"}'


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_비유한_실수는_직렬화를_거부한다(bad):
    with pytest.raises(ConfigError):
        canonical_json({"x": bad})


def test_naive_datetime은_해시_입력이_될_수_없다():
    with pytest.raises(ConfigError):
        canonical_json({"t": datetime(2026, 9, 16, 15, 35)})


def test_지원하지_않는_타입은_문자열로_뭉개지_않는다():
    class Weird:
        pass

    with pytest.raises(ConfigError):
        canonical_json({"x": Weird()})


def test_정수와_실수는_다른_해시():
    assert sha256_of({"x": 1}) != sha256_of({"x": 1.0})


def test_해시는_sha256_접두사와_64자리():
    h = canonical_config_hash(CONFIG)
    assert h.startswith("sha256:")
    assert len(h) == len("sha256:") + 64


# ─────────────────────────────────────────────────────────────
# signal_id
# ─────────────────────────────────────────────────────────────

def test_signal_id는_재계산해도_같다():
    a = make_signal_id("1.5", CONFIG_HASH, UNIVERSE, SIGNAL_AT, "005930")
    b = make_signal_id("1.5", CONFIG_HASH, UNIVERSE, SIGNAL_AT, "KRX:005930")
    assert a == b
    assert a.startswith("sig_") and len(a) == len("sig_") + 64


@pytest.mark.parametrize("field,value", [
    ("scanner_version", "1.6"),
    ("config_hash", canonical_config_hash({"period": 3})),
    ("universe_snapshot_id",
     make_universe_snapshot_id("KOSPI200", SIGNAL_AT, ["005930"])),
    ("signal_at", SIGNAL_AT + timedelta(minutes=1)),
    ("security_id", "000660"),
])
def test_해시_입력이_하나라도_바뀌면_signal_id가_바뀐다(field, value):
    args = {"scanner_version": "1.5", "config_hash": CONFIG_HASH,
            "universe_snapshot_id": UNIVERSE, "signal_at": SIGNAL_AT,
            "security_id": "005930"}
    base = make_signal_id(**args)
    args[field] = value
    assert make_signal_id(**args) != base


def test_run_id는_signal_id에_영향을_주지_않는다():
    """같은 논리 신호를 다른 run에서 재생해도 ID가 같아야 한다."""
    run_a = make_run_id("replay", SIGNAL_AT, "aaaaaaaa")
    run_b = make_run_id("replay", SIGNAL_AT, "bbbbbbbb")
    assert run_a != run_b
    assert (make_signal_id("1.5", CONFIG_HASH, UNIVERSE, SIGNAL_AT, "005930")
            == make_signal_id("1.5", CONFIG_HASH, UNIVERSE, SIGNAL_AT, "005930"))


def test_축약_config_hash는_signal_id_입력으로_거부된다():
    with pytest.raises(ConfigError):
        make_signal_id("1.5", CONFIG_HASH[7:19], UNIVERSE, SIGNAL_AT, "005930")


# ─────────────────────────────────────────────────────────────
# run_id
# ─────────────────────────────────────────────────────────────

def test_run_id_형식():
    assert make_run_id("replay", SIGNAL_AT, "0123abcd") == \
        "replay_20260916063500_0123abcd"


def test_run_id는_nonce_없으면_매번_다르다():
    assert make_run_id("replay", SIGNAL_AT) != make_run_id("replay", SIGNAL_AT)


def test_run_kind_대소문자는_정규화한다():
    assert (make_run_id("Replay", SIGNAL_AT, "0123abcd")
            == make_run_id("replay", SIGNAL_AT, "0123abcd"))


@pytest.mark.parametrize("kind", ["re play", "", "1replay", "replay-run"])
def test_run_kind_형식_위반은_거부(kind):
    with pytest.raises(ConfigError):
        make_run_id(kind, SIGNAL_AT, "0123abcd")


@pytest.mark.parametrize("nonce", ["xyz", "0123ABCDE", "0123abc"])
def test_nonce_형식_위반은_거부(nonce):
    with pytest.raises(ConfigError):
        make_run_id("replay", SIGNAL_AT, nonce)


# ─────────────────────────────────────────────────────────────
# universe_snapshot_id
# ─────────────────────────────────────────────────────────────

def test_구성종목_순서는_결과에_영향이_없다():
    a = make_universe_snapshot_id("KOSPI200", SIGNAL_AT, ["005930", "000660"])
    b = make_universe_snapshot_id("KOSPI200", SIGNAL_AT, ["000660", "005930"])
    assert a == b


def test_구성종목이_하나만_달라도_스냅샷_ID가_바뀐다():
    a = make_universe_snapshot_id("KOSPI200", SIGNAL_AT, ["005930", "000660"])
    b = make_universe_snapshot_id("KOSPI200", SIGNAL_AT, ["005930", "035420"])
    assert a != b


def test_중복_구성종목은_거부한다():
    with pytest.raises(ConfigError):
        make_universe_snapshot_id("KOSPI200", SIGNAL_AT, ["005930", "005930"])


def test_빈_유니버스는_거부한다():
    with pytest.raises(ConfigError):
        make_universe_snapshot_id("KOSPI200", SIGNAL_AT, [])


# ─────────────────────────────────────────────────────────────
# 종목 ID / 시각
# ─────────────────────────────────────────────────────────────

def test_종목코드_정규화():
    assert normalize_security_id("005930") == "KRX:005930"
    assert parse_security_id("KRX:005930") == ("KRX", "005930")


@pytest.mark.parametrize("bad", ["", "12", "005930 ;drop", "KRX:", "005930/"])
def test_잘못된_종목코드는_거부(bad):
    with pytest.raises(ConfigError):
        normalize_security_id(bad)


def test_naive_datetime은_명시적_tz를_요구한다():
    naive = datetime(2026, 9, 16, 15, 35)
    with pytest.raises(ConfigError):
        to_utc(naive)
    assert utc_iso(naive, assume_tz=KST) == "2026-09-16T06:35:00Z"


def test_거래일은_KST_달력일():
    # 애프터마켓 종료 직전(19:59 KST)도 같은 KST 거래일이다.
    late = datetime(2026, 9, 16, 19, 59, tzinfo=KST)
    assert kst_trading_date(late) == date(2026, 9, 16)
    # UTC로는 이미 10:59이지만 날짜 판정은 KST 기준이다.
    assert kst_trading_date(late.astimezone(timezone.utc)) == date(2026, 9, 16)


# ─────────────────────────────────────────────────────────────
# V1.5 호환
# ─────────────────────────────────────────────────────────────

def test_legacy_config_hash가_V15_구현과_같다():
    """V1.5 `_save_scan_snapshot()`의 12자 해시를 그대로 재현하는지 확인."""
    import hashlib
    import json

    expected = hashlib.sha256(
        json.dumps(CONFIG, sort_keys=True, ensure_ascii=False,
                   default=str).encode("utf-8")).hexdigest()[:12]
    assert legacy_v15_config_hash(CONFIG) == expected
    assert len(legacy_v15_config_hash(CONFIG)) == 12


def test_실험_모델_ID_형식():
    exp = make_experiment_id({"top_k": 5, "horizon": 5}, "run1")
    assert exp.startswith("exp_") and exp.endswith("_run1")
    mdl = make_model_id("up_5d", "sha256:" + "0" * 64,
                        date(2024, 1, 1), date(2025, 1, 1), "logistic", "a1")
    assert mdl.startswith("mdl_") and mdl.endswith("_a1")

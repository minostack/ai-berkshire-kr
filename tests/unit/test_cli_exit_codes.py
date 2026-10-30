# -*- coding: utf-8 -*-
"""CLI 종료 코드 계약(아키텍처 §8).

CLI는 스케줄러가 호출한다. 실패를 0으로 돌려주면 파이프라인이 잘못된 데이터를
그대로 다음 단계로 넘긴다. 실패 종류별로 다른 코드가 나오는지 확인한다.
"""

import importlib
import json
import os

import pytest

from cvd_obv.cli.main import build_parser, main
from cvd_obv.domain.status import ExitCode

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
GOLDEN = os.path.join(ROOT, "tests", "golden", "v15_baseline.json")


def test_파서는_필수_하위명령을_요구한다():
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])


def test_쓰기_명령은_공통_옵션을_받는다():
    args = build_parser().parse_args(
        ["baseline", "freeze", "--dry-run", "--log-json",
         "--run-id", "x_20260916000000_abcdabcd"])
    assert args.dry_run and args.log_json
    assert args.run_id == "x_20260916000000_abcdabcd"


def test_dry_run은_파일을_쓰지_않는다(tmp_path, capsys):
    golden = tmp_path / "g.json"
    manifest = tmp_path / "m.json"
    code = main(["baseline", "freeze", "--dry-run", "--log-json",
                 "--golden", str(golden), "--manifest", str(manifest)])
    assert code == ExitCode.OK
    assert not golden.exists() and not manifest.exists()
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["dry_run"] is True


def test_없는_스냅샷은_설정_오류가_아니라_예외로_끝난다(tmp_path):
    """존재하지 않는 파일은 파이썬 예외로 새지 않고 CLI에서 걸러야 한다."""
    missing = tmp_path / "nope.json"
    with pytest.raises(FileNotFoundError):
        main(["contract", "convert", str(missing)])


def test_잘못된_스냅샷은_스키마_비호환으로_끝난다(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"as_of": "2026-09-16T15:35:00"}),
                   encoding="utf-8")
    code = main(["contract", "convert", str(bad), "--log-json"])
    assert code == ExitCode.SCHEMA_INCOMPATIBLE
    err = capsys.readouterr().err.strip()
    assert json.loads(err)["exit_code"] == int(ExitCode.SCHEMA_INCOMPATIBLE)


def test_골든_불일치는_스키마_비호환_코드로_끝난다(tmp_path, capsys):
    """동결된 값과 달라지면 0이 아닌 코드로 실패해야 한다."""
    if not os.path.exists(GOLDEN):
        pytest.skip("골든 샘플이 없다")
    with open(GOLDEN, encoding="utf-8") as f:
        tree = json.load(f)
    tree["pipeline"]["_finalize_scores"][0]["total"] = -1.0
    tampered = tmp_path / "tampered.json"
    tampered.write_text(json.dumps(tree, ensure_ascii=False), encoding="utf-8")

    code = main(["baseline", "verify", "--golden", str(tampered), "--log-json"])
    assert code == ExitCode.SCHEMA_INCOMPATIBLE
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["golden_ok"] is False
    assert payload["golden_diffs"]


def test_정상_검증은_0으로_끝난다(capsys):
    if not os.path.exists(GOLDEN):
        pytest.skip("골든 샘플이 없다")
    assert main(["baseline", "verify", "--log-json"]) == ExitCode.OK
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["ok"] is True


def test_stage3_하위명령이_모두_붙어_있다():
    """Stage 3는 네 단계로 나뉜다 — 캐시·공지·복원·검산.

    복원만 있고 검산이 없으면 "복원했다"는 말을 증명할 방법이 없다. 그래서
    `verify`는 편의 명령이 아니라 계약의 일부다.
    """
    parser = build_parser()
    for action in ("krx-cache", "notices", "rebuild", "verify"):
        args = parser.parse_args(["universe", action])
        assert args.action == action
        assert callable(args.func)


def test_검산_실패는_데이터_품질_코드로_끝난다(monkeypatch):
    """항등식이 깨지면 0으로 끝내면 안 된다 — 다음 단계가 그대로 물려받는다."""
    from datetime import date

    # 패키지 __init__이 같은 이름의 함수를 내보내므로 모듈을 직접 가져온다.
    cli = importlib.import_module("cvd_obv.cli.main")

    class FakeApi:
        def checksum(self, members, bas_dd, index_name=None,
                     market="KOSPI200"):
            return {"diff": 12345, "members": len(members)}

    class FakeRepo:
        def get_members(self, index_id, as_of, **kwargs):
            return {"members": ["KRX:005930"]}

    monkeypatch.setattr(cli, "KrxOpenApi", lambda *a, **k: FakeApi())
    monkeypatch.setattr(cli, "UniverseRepository", lambda *a, **k: FakeRepo())
    monkeypatch.setattr(cli, "_trading_days",
                        lambda start, end: [date(2026, 9, 17)])
    code = cli.main(["universe", "verify", "--log-json"])
    assert code == ExitCode.DATA_QUALITY_FAILED


def test_stage6_하위명령이_붙어_있다():
    """Stage 6는 두 단계다 — label을 붙이고(`signal-label`), 평가한다.

    붙이기만 하고 평가가 없으면 "신호가 무엇을 예고했나"에 답할 수 없다.
    """
    parser = build_parser()
    labels = parser.parse_args(["signal-label", "build"])
    assert labels.action == "build" and callable(labels.func)
    assert labels.horizons == "1,3,5,10,20"

    evaluate = parser.parse_args(["signal-gate", "evaluate"])
    assert evaluate.action == "evaluate" and callable(evaluate.func)
    assert evaluate.segment_by is None


def test_신호_평가는_연도_세그먼트를_받지_않는다():
    """replay 구간이 1년뿐이라 연도 분해는 성립하지 않는다."""
    parser = build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args(["signal-gate", "evaluate", "--segment-by", "year"])


def test_알_수_없는_선택_규칙은_설정_오류로_끝난다(capsys):
    code = main(["signal-gate", "evaluate", "--variants", "NOPE",
                 "--log-json"])
    assert code == ExitCode.CLI_CONFIG_ERROR


def test_stage7_하위명령이_붙어_있다():
    """Stage 7은 선택·체결 정책을 인자로 받는다."""
    parser = build_parser()
    args = parser.parse_args(["backtest", "run"])
    assert args.action == "run" and callable(args.func)
    assert args.policies == "BUY_ALL"
    assert args.max_entries_per_day == "AUTO"
    assert args.liquidity_cap == 0.01


def test_알_수_없는_선택_정책은_설정_오류로_끝난다():
    code = main(["backtest", "run", "--policies", "NOPE", "--log-json",
                 "--dry-run"])
    assert code == ExitCode.CLI_CONFIG_ERROR


def test_stage8_하위명령이_붙어_있다():
    """Stage 8은 성분 제거와 검정력을 한 번에 낸다."""
    parser = build_parser()
    args = parser.parse_args(["ablation", "run"])
    assert args.action == "run" and callable(args.func)
    assert args.horizon == 20
    assert args.seed == 20260919


def test_알_수_없는_변형은_설정_오류로_끝난다():
    code = main(["ablation", "run", "--variants", "NOPE", "--log-json",
                 "--dry-run"])
    assert code == ExitCode.CLI_CONFIG_ERROR


def test_접은_horizon은_기본값에서_빠진다():
    """h=1은 검정력이 모자라 분석 기본 격자에서 뺐다(2026-09-19 결정)."""
    parser = build_parser()
    assert parser.parse_args(["signal-gate", "evaluate"]).horizons == "5,20"
    assert parser.parse_args(["daily-gate", "backtest"]).horizons == "5,20"
    # label 생성은 그대로다 — 접는 것은 보고이지 데이터가 아니다.
    assert (parser.parse_args(["signal-label", "build"]).horizons
            == "1,3,5,10,20")
    assert parser.parse_args(["forward", "build"]).horizons == "1,3,5,10,20"


def test_접은_horizon을_직접_지정하면_이유를_알려_준다(capsys):
    """막지는 않는다 — 다만 왜 기본 표에 없는지 모른 채 읽지 않게 한다."""
    from cvd_obv.cli.main import _horizons

    assert _horizons("1,5") == [1, 5]
    out = capsys.readouterr().out
    assert "h=1" in out and "검정력" in out
    _horizons("5,20")
    assert "h=1" not in capsys.readouterr().out


def test_검출가능구간_명령이_붙어_있다():
    """사전 등록 C H3 — 설계로 무엇을 잴 수 있나."""
    args = build_parser().parse_args(["power", "frontier"])
    assert args.action == "frontier" and callable(args.func)
    assert "1" not in args.horizons.split(",")   # h=1은 접었다
    assert args.project_to == "4114"


def test_중첩_비교_기준을_인자로_받는다():
    """'OBV가 가격 위에 더하는가'는 ALL 대비로는 물을 수 없다."""
    parser = build_parser()
    assert parser.parse_args(["daily-gate", "backtest"]).base_variant == "ALL"
    args = parser.parse_args(["daily-gate", "backtest",
                              "--base-variant", "PRICE"])
    assert args.base_variant == "PRICE"


def test_확증_계획_명령이_붙어_있다():
    args = build_parser().parse_args(["power", "plan"])
    assert args.action == "plan" and callable(args.func)
    assert args.alpha == 0.05 and args.power == 0.80


def test_확증할_후보가_없으면_설정_오류로_끝난다():
    code = main(["power", "plan", "--results", "reports/없는경로/*.json",
                 "--log-json", "--dry-run"])
    assert code == ExitCode.CLI_CONFIG_ERROR


def test_후보_게이트_명령이_붙어_있다():
    args = build_parser().parse_args(["candidate", "gate",
                                      "--candidates", "x.json"])
    assert args.action == "gate" and callable(args.func)
    assert args.min_annual_excess_bp == 100.0
    assert args.min_observed_to_mde == 1.0


def test_게이트를_통과하지_못하면_통계검증_실패로_끝난다(tmp_path):
    """선택지 C 후보를 그대로 넣으면 떨어진다 — 그게 이 게이트의 존재 이유다.

    h=20은 q=0.020으로 FDR을 통과했지만 관측/MDE가 0.97이고 비용 후 연
    37bp다. 통계만 보던 때는 이 둘이 보고에 남지 않았다.
    """
    import json

    path = tmp_path / "cands.json"
    path.write_text(json.dumps({"candidates": [{
        "name": "PRICE+OBV vs PRICE", "horizon_td": 20,
        "effect": 0.000802, "ci_low": 0.000248, "ci_high": 0.001408,
        "q_value": 0.020, "mde": 0.00083, "cost_per_round_trip": 0.0005,
        "median_daily_value_krw": 5e9, "participation_rate": 0.002,
    }]}), encoding="utf-8")
    code = main(["candidate", "gate", "--candidates", str(path),
                 "--log-json", "--dry-run"])
    assert code == ExitCode.STATISTICAL_VALIDATION_FAILED


def test_후보_파일이_비면_설정_오류로_끝난다(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text("[]", encoding="utf-8")
    assert main(["candidate", "gate", "--candidates", str(path),
                 "--log-json", "--dry-run"]) == ExitCode.CLI_CONFIG_ERROR


def test_의존성_측정_명령이_붙어_있다():
    args = build_parser().parse_args(["dependence", "measure",
                                      "--series-a", "a.json",
                                      "--series-b", "b.json"])
    assert args.action == "measure" and callable(args.func)
    assert args.block_length == 20          # 보유기간 이상이어야 한다
    assert args.name_a == "KOSPI200" and args.name_b == "KOSDAQ150"


def test_공통_날짜가_없으면_통계검증_실패로_끝난다(tmp_path):
    import json

    a = tmp_path / "a.json"
    b = tmp_path / "b.json"
    a.write_text(json.dumps({"2020-01-01": 0.1}), encoding="utf-8")
    b.write_text(json.dumps({"2021-01-01": 0.1}), encoding="utf-8")
    code = main(["dependence", "measure", "--series-a", str(a),
                 "--series-b", str(b), "--log-json", "--dry-run"])
    assert code == ExitCode.STATISTICAL_VALIDATION_FAILED


def test_CLI_레퍼런스가_코드와_일치한다():
    """문서를 손으로 적으면 반드시 어긋난다 — 파서를 유일한 출처로 둔다.

    이 테스트가 깨지면 `python cvdobv.py cli reference`를 다시 돌리면 된다.
    """
    assert main(["cli", "reference", "--check", "--log-json"]) == ExitCode.OK


def test_레퍼런스_생성은_파서를_그대로_읽는다(tmp_path):
    out = tmp_path / "ref.md"
    assert main(["cli", "reference", "--out", str(out),
                 "--log-json"]) == ExitCode.OK
    text = out.read_text(encoding="utf-8")
    # 그룹이 빠지면 문서가 조용히 낡는다
    for group in ("daily-gate", "signal-gate", "backtest", "power",
                  "candidate", "dependence"):
        assert f"## `{group}`" in text, group
    assert "손으로 고치지 말 것" in text

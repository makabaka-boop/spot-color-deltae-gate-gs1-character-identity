"""GS1 批次标签解析端点测试：两种格式、定长/变长规则、校验位与真实日历日期。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)

# 合法 GTIN-14（校验位已按 GS1 模 10 规则核算）
GTIN = "09506000134352"
GTIN_ALT = "04012345678901"

# 两种格式的合法标签：商品编码 01、批号 10、失效日期 17（2028-09-30）
READABLE_OK = f"(01){GTIN}(10)INK2407(17)280930"
SCAN_OK = f"01{GTIN}10INK2407\x1d17280930"


def _post(raw: object) -> tuple[int, dict[str, object]]:
    res = client.post("/api/gs1-label", json={"raw": raw})
    return res.status_code, res.json()


# ── 两种格式生成统一批次信息 ─────────────────────────────────────────────

def test_readable_format_parses_to_unified_batch() -> None:
    status, body = _post(READABLE_OK)
    assert status == 200
    assert body["ok"] is True
    assert body["format"] == "readable"
    assert body["batch"] == {
        "gtin": GTIN,
        "lot": "INK2407",
        "expires": "2028-09-30",
    }


def test_scan_format_with_fnc1_parses_to_same_batch() -> None:
    """含 FNC1(GS 0x1D) 分隔符的扫描格式与可读格式得到同一批次信息。"""
    status, body = _post(SCAN_OK)
    assert status == 200
    assert body["ok"] is True
    assert body["format"] == "scan"
    assert body["batch"] == {
        "gtin": GTIN,
        "lot": "INK2407",
        "expires": "2028-09-30",
    }


def test_field_order_does_not_matter() -> None:
    status, body = _post(f"(17)280930(10)INK2407(01){GTIN}")
    assert status == 200
    assert body["batch"] == {"gtin": GTIN, "lot": "INK2407", "expires": "2028-09-30"}


def test_alternative_valid_gtin_accepted() -> None:
    status, body = _post(f"(01){GTIN_ALT}(10)B-07(17)280930")
    assert status == 200
    assert body["batch"]["gtin"] == GTIN_ALT
    assert body["batch"]["lot"] == "B-07"


def test_scanner_suffix_and_symbology_prefix_tolerated() -> None:
    """扫码枪附加的回车后缀与 AIM 符号标识符前缀（]d2）不影响解析。"""
    status, body = _post(READABLE_OK + "\r\n")
    assert status == 200
    assert body["batch"]["lot"] == "INK2407"

    status, body = _post("]d2" + SCAN_OK)
    assert status == 200
    assert body["format"] == "scan"
    assert body["batch"]["gtin"] == GTIN


# ── 定长 / 变长规则 ──────────────────────────────────────────────────────

def test_variable_length_field_at_end_needs_no_fnc1() -> None:
    """末尾的变长字段可省略 FNC1；定长 AI(17) 恰好消费 6 位。"""
    status, body = _post(f"01{GTIN}1728093010INK2407")
    assert status == 200
    assert body["batch"] == {"gtin": GTIN, "lot": "INK2407", "expires": "2028-09-30"}


def test_multiple_variable_fields_separated_by_fnc1() -> None:
    """多个变长字段以 FNC1 分隔，额外字段（序列号 21）保留在字段表中。"""
    status, body = _post(f"01{GTIN}10INK2407\x1d21SER998\x1d17280930")
    assert status == 200
    assert body["batch"]["lot"] == "INK2407"
    fields = {f["ai"]: f["value"] for f in body["fields"]}
    assert fields["21"] == "SER998"
    assert fields["17"] == "280930"


def test_variable_field_too_long_rejected_with_position() -> None:
    raw = f"(01){GTIN}(10)INK2407INK2407INK240X(17)280930"  # 批号 21 位，超限
    status, body = _post(raw)
    assert status == 422
    assert body["ok"] is False
    assert "最长 20 位" in body["message"]
    # 批号值从下标 22 开始，第 21 个字符（下标 42）为首个越界字符
    assert body["position"] == 42
    assert raw[body["position"]] == "X"


def test_variable_field_empty_rejected() -> None:
    status, body = _post(f"(01){GTIN}(10)(17)280930")
    assert status == 422
    assert "内容为空" in body["message"]
    assert body["position"] == 22  # (10) 之后值应出现的位置


def test_fixed_field_truncated_rejected_with_position() -> None:
    status, body = _post("0109506000134")  # AI(01) 需要 14 位，仅 11 位
    assert status == 422
    assert "数据不足" in body["message"]
    assert body["position"] == 2  # 定长字段起始处


def test_fnc1_inside_fixed_field_rejected() -> None:
    raw = "01095060\x1d34352"
    status, body = _post(raw)
    assert status == 422
    assert "数据不足" in body["message"]
    assert body["position"] == 8  # FNC1 出现处
    assert raw[body["position"]] == "\x1d"


def test_numeric_field_with_letter_rejected_at_character() -> None:
    raw = f"(01)0950600013435A(10)INK2407(17)280930"
    status, body = _post(raw)
    assert status == 422
    assert "纯数字" in body["message"]
    assert body["position"] == 17  # 字母 A 所在下标
    assert raw[body["position"]] == "A"


# ── 非 ASCII 数字字符：定位该字符并拒绝 ─────────────────────────────────
# Python 的 str.isdigit()/int() 接受阿拉伯文数字、全角数字等 Unicode 数字，
# 但 GS1 数字字段仅允许 ASCII 0-9；这类字符必须被拒绝并给出位置。

# 阿拉伯文数字、扩展阿拉伯文数字、全角数字、天城文数字、上标数字
NON_ASCII_DIGITS = ["٢", "۳", "３", "३", "²"]


def test_arabic_indic_digit_as_gtin_check_digit_rejected() -> None:
    """校验位为阿拉伯文数字 ٢（数值恰等于正确校验位 2）也必须拒绝。"""
    raw = "(01)0950600013435٢(10)INK2407(17)280930"
    status, body = _post(raw)
    assert status == 422
    assert body["ok"] is False
    assert "纯数字" in body["message"]
    assert body["position"] == 17
    assert raw[body["position"]] == "٢"


def test_arabic_indic_digit_in_expiry_rejected() -> None:
    raw = "(01)09506000134352(10)INK2407(17)28٠930"  # ٠ = 阿拉伯文数字 0
    status, body = _post(raw)
    assert status == 422
    assert "纯数字" in body["message"]
    assert body["position"] == 35  # 失效日期值下标 33 起，第 3 位
    assert raw[body["position"]] == "٠"


def test_non_ascii_digits_in_gtin_rejected_both_formats() -> None:
    for ch in NON_ASCII_DIGITS:
        raw = f"(01)0950600013435{ch}(10)INK2407(17)280930"
        status, body = _post(raw)
        assert status == 422, (ch, "readable")
        assert body["position"] == 17, (ch, "readable")
        assert raw[body["position"]] == ch

        raw = f"010950600013435{ch}10INK2407\x1d17280930"
        status, body = _post(raw)
        assert status == 422, (ch, "scan")
        assert body["position"] == 15, (ch, "scan")
        assert raw[body["position"]] == ch


def test_non_ascii_digits_in_expiry_rejected_both_formats() -> None:
    for ch in NON_ASCII_DIGITS:
        raw = f"(01)09506000134352(10)INK2407(17)2809{ch}0"
        status, body = _post(raw)
        assert status == 422, (ch, "readable")
        assert body["position"] == 37, (ch, "readable")
        assert raw[body["position"]] == ch

        raw = f"010950600013435210INK2407\x1d172809{ch}0"
        status, body = _post(raw)
        assert status == 422, (ch, "scan")
        assert body["position"] == 32, (ch, "scan")
        assert raw[body["position"]] == ch


def test_superscript_digit_rejected_as_parse_error_not_crash() -> None:
    """上标 ² 的 isdigit() 为真但 int() 无法转换：必须是 422 而非 500。"""
    status, body = _post("(01)0950600013435²(10)INK2407(17)280930")
    assert status == 422
    assert body["ok"] is False
    assert "纯数字" in body["message"]
    assert body["position"] == 17


# ── 商品编码校验位 ───────────────────────────────────────────────────────

def test_gtin_check_digit_failure_rejected_at_check_digit() -> None:
    raw = "(01)09506000134353(10)INK2407(17)280930"  # 末位应为 2
    status, body = _post(raw)
    assert status == 422
    assert "校验位" in body["message"]
    assert "应为 2" in body["message"]
    assert body["position"] == 17  # 校验位字符本身
    assert raw[body["position"]] == "3"


def test_gtin_check_digit_failure_in_scan_format() -> None:
    status, body = _post("010950600013435310INK2407\x1d17280930")
    assert status == 422
    assert "校验位" in body["message"]
    assert body["position"] == 15  # 扫描格式中 GTIN 从下标 2 开始，校验位在 2+13


# ── 失效日期：真实日历日期 ───────────────────────────────────────────────

def test_expiry_leap_day_valid() -> None:
    status, body = _post(f"(01){GTIN}(10)INK2407(17)280229")
    assert status == 200
    assert body["batch"]["expires"] == "2028-02-29"  # 2028 为闰年


def test_expiry_leap_day_invalid_in_common_year() -> None:
    status, body = _post(f"(01){GTIN}(10)INK2407(17)290229")
    assert status == 422
    assert "不是真实日历日期" in body["message"]
    assert body["position"] == 37  # 日字段起始（值从下标 33 开始，日在 +4）


def test_expiry_day_zero_means_last_day_of_month() -> None:
    """GS1 规范：日字段 00 表示当月最后一天。"""
    status, body = _post(f"(01){GTIN}(10)INK2407(17)280200")
    assert status == 200
    assert body["batch"]["expires"] == "2028-02-29"


def test_expiry_month_13_rejected() -> None:
    status, body = _post(f"(01){GTIN}(10)INK2407(17)281330")
    assert status == 422
    assert "月份" in body["message"]
    assert body["position"] == 35  # 月字段起始


def test_expiry_feb_30_rejected() -> None:
    status, body = _post(f"(01){GTIN}(10)INK2407(17)280230")
    assert status == 422
    assert "不是真实日历日期" in body["message"]


# ── 结构错误与必需字段 ───────────────────────────────────────────────────

def test_unknown_ai_readable_rejected_with_position() -> None:
    status, body = _post("(05)123456")
    assert status == 422
    assert "未知应用标识符" in body["message"]
    assert body["position"] == 1  # AI 数字本身


def test_unknown_ai_scan_rejected_with_position() -> None:
    status, body = _post(f"01{GTIN}1728093005XYZ")
    assert status == 422
    assert "无法识别的应用标识符" in body["message"]
    assert body["position"] == 24  # 消费完 01 与 17 后的下一个字符


def test_missing_required_ai_rejected() -> None:
    for raw, missing in [
        ("(10)INK2407(17)280930", "(01)"),
        (f"(01){GTIN}(17)280930", "(10)"),
        (f"(01){GTIN}(10)INK2407", "(17)"),
    ]:
        status, body = _post(raw)
        assert status == 422, raw
        assert "缺少" in body["message"]
        assert missing in body["message"]
        assert body["position"] == len(raw)  # 指向内容末尾


def test_duplicate_ai_rejected() -> None:
    raw = f"(01){GTIN}(01){GTIN}(10)INK2407(17)280930"
    status, body = _post(raw)
    assert status == 422
    assert "重复出现" in body["message"]
    assert body["position"] == 22  # 第二个 (01) 的值起始处


def test_empty_and_blank_raw_rejected() -> None:
    status, body = _post("")
    assert status == 422  # pydantic min_length

    status, body = _post("   ")
    assert status == 422
    assert body["ok"] is False
    assert "为空" in body["message"]
    assert body["position"] == 0


def test_request_shape_validation() -> None:
    res = client.post("/api/gs1-label", json={})
    assert res.status_code == 422
    assert any(e["field"] == "raw" for e in res.json()["errors"])

    res = client.post("/api/gs1-label", json={"raw": READABLE_OK, "x": 1})
    assert res.status_code == 422

    res = client.post("/api/gs1-label", json={"raw": 12345})
    assert res.status_code == 422


def test_error_response_keeps_field_error_shape() -> None:
    """错误响应复用既有字段错误结构，并额外给出首个无法解析的位置。"""
    status, body = _post("(05)123456")
    assert status == 422
    assert body["ok"] is False
    assert isinstance(body["errors"], list) and body["errors"]
    assert body["errors"][0]["field"] == "raw"
    assert body["errors"][0]["message"]
    assert isinstance(body["position"], int)


# ── 与既有色差端点互不影响 ───────────────────────────────────────────────

def test_delta_e_endpoint_unaffected_by_label_feature() -> None:
    res = client.post(
        "/api/delta-e",
        json={
            "standard": {"L": 60.2574, "a": -34.0099, "b": 36.2677},
            "sample": {"L": 60.4626, "a": -34.1751, "b": 39.4387},
        },
    )
    assert res.status_code == 200
    assert res.json()["result"]["passed"] is True

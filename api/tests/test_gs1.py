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


def test_empty_raw_rejected() -> None:
    status, body = _post("")
    assert status == 422  # pydantic min_length


def test_blank_spaces_are_data_not_noise_rejected_at_first_char() -> None:
    """纯空格不是“空标签”：空格是 GS1 合法字符但不能构成 AI，按首字符拒绝。

    旧实现把空格列入尾部噪声并剥离，得到“标签内容为空”；这会顺带吞掉末尾
    变长字段的尾随空格（使两个批号合并为同一批次），新实现必须如此拒绝。
    """
    raw = "   "
    status, body = _post(raw)
    assert status == 422
    assert body["ok"] is False
    assert body["code"] == "parse_error"
    assert body["position"] == 0
    assert raw[body["position"]] == " "


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


# ── 尾随空格：字段身份逐字符保留，两种格式都不得剥离 ─────────────────────
# 带空格的变长字段（批号 10）放在末尾时，尾随空格若被连同扫码枪后缀删除，
# "INK2407 " 会与 "INK2407" 得到同一批次——这是必须杜绝的数据改写。

def test_trailing_space_in_last_variable_field_readable_is_identity() -> None:
    raw = f"(01){GTIN}(17)280930(10)INK2407 "
    status, body = _post(raw)
    assert status == 200
    assert body["batch"]["lot"] == "INK2407 "


def test_trailing_space_in_last_variable_field_scan_is_identity() -> None:
    raw = f"01{GTIN}1728093010INK2407 "
    status, body = _post(raw)
    assert status == 200
    assert body["batch"]["lot"] == "INK2407 "


def test_two_lots_distinguished_by_trailing_space_both_formats() -> None:
    """两个不同批号（仅尾随空格不同）必须得到不同批次结果。"""
    lots: dict[str, str] = {}
    for raw in (
        f"(01){GTIN}(17)280930(10)INK2407",
        f"(01){GTIN}(17)280930(10)INK2407 ",
        f"01{GTIN}1728093010INK2407",
        f"01{GTIN}1728093010INK2407 ",
    ):
        status, body = _post(raw)
        assert status == 200, raw
        lots[raw] = body["batch"]["lot"]
    assert lots[f"(01){GTIN}(17)280930(10)INK2407"] == "INK2407"
    assert lots[f"(01){GTIN}(17)280930(10)INK2407 "] == "INK2407 "
    assert (
        lots[f"(01){GTIN}(17)280930(10)INK2407"]
        != lots[f"(01){GTIN}(17)280930(10)INK2407 "]
    )


def test_leading_and_middle_spaces_in_lot_preserved() -> None:
    raw = f"(01){GTIN}(10) LOT A 1 (17)280930"
    status, body = _post(raw)
    assert status == 200
    assert body["batch"]["lot"] == " LOT A 1 "


def test_trailing_space_then_crlf_scanner_suffix_both_preserved() -> None:
    """末尾变长字段值含尾随空格，扫码枪再附加 CRLF 行尾：空格保留、行尾容忍。"""
    status, body = _post(f"01{GTIN}1728093010INK2407 \r\n")
    assert status == 200
    assert body["batch"]["lot"] == "INK2407 "

    status, body = _post(f"(01){GTIN}(17)280930(10)INK2407 \n")
    assert status == 200
    assert body["batch"]["lot"] == "INK2407 "


def test_fnc1_terminated_variable_field_trailing_space_preserved() -> None:
    """字段不以整串结尾时，FNC1 前的尾随空格同样是值。"""
    raw = f"01{GTIN}10INK2407 \x1d17280930"
    status, body = _post(raw)
    assert status == 200
    assert body["batch"]["lot"] == "INK2407 "


# ── TAB：字段中部与末尾语义一致（均为字符集外字符，稳定拒绝） ────────────

def test_tab_at_end_of_lot_rejected_at_tab_position_both_formats() -> None:
    raw = f"(01){GTIN}(17)280930(10)INK2407\t"
    status, body = _post(raw)
    assert status == 422
    assert body["code"] == "unsupported_character"
    assert body["position"] == len(raw) - 1
    assert raw[body["position"]] == "\t"

    raw = f"01{GTIN}1728093010INK2407\t"
    status, body = _post(raw)
    assert status == 422
    assert body["code"] == "unsupported_character"
    assert body["position"] == len(raw) - 1
    assert raw[body["position"]] == "\t"


def test_tab_in_middle_of_lot_rejected_at_tab_position_both_formats() -> None:
    readable = f"(01){GTIN}(10)INK\t2407(17)280930"
    status, body = _post(readable)
    assert status == 422
    assert body["code"] == "unsupported_character"
    assert body["position"] == readable.index("\t")
    assert readable[body["position"]] == "\t"

    scan = f"01{GTIN}10INK\t2407\x1d17280930"
    status, body = _post(scan)
    assert status == 422
    assert body["code"] == "unsupported_character"
    assert body["position"] == scan.index("\t")
    assert scan[body["position"]] == "\t"


def test_tab_middle_and_tab_end_have_same_error_code() -> None:
    for raw in (
        f"(01){GTIN}(10)AB\tCD(17)280930",
        f"(01){GTIN}(10)ABCD\t(17)280930",
        f"01{GTIN}10AB\tCD\x1d17280930",
        f"01{GTIN}1728093010ABCD\t",
    ):
        status, body = _post(raw)
        assert status == 422, raw
        assert body["code"] == "unsupported_character", raw


# ── GS1 字符集外字符：emoji / NUL / 换行 / DEL / 非 ASCII 文本，稳定拒绝 ──

# (原始输入, 应被定位的字符) —— 覆盖批号 10、序列号 21、企业内部 90～99
_UNSUPPORTED_CHARS = [
    ("😀", "emoji U+1F600（星面字符，UTF-16 代理对）"),
    ("\x00", "NUL"),
    ("\n", "LF 换行（字段中部，非扫码行尾）"),
    ("\r", "CR（字段中部）"),
    ("\x7f", "DEL"),
    ("é", "拉丁补充字母 é"),
    ("中", "中文字符"),
    ("　", "全角空格 U+3000"),
    ("–", "en dash U+2013"),
]


def test_unsupported_chars_in_lot_rejected_both_formats() -> None:
    for ch, desc in _UNSUPPORTED_CHARS:
        readable = f"(01){GTIN}(10)AB{ch}CD(17)280930"
        status, body = _post(readable)
        assert status == 422, (desc, "readable")
        assert body["code"] == "unsupported_character", (desc, "readable")
        assert body["position"] == readable.index(ch), (desc, "readable")
        assert readable[body["position"]] == ch

        scan = f"01{GTIN}10AB{ch}CD\x1d17280930"
        status, body = _post(scan)
        assert status == 422, (desc, "scan")
        assert body["code"] == "unsupported_character", (desc, "scan")
        assert body["position"] == scan.index(ch), (desc, "scan")
        assert scan[body["position"]] == ch


def test_unsupported_chars_in_serial_and_internal_ais_rejected() -> None:
    """序列号 21 与企业内部 90～99 同样只接受 GS1 字符集。"""
    for ch, desc in _UNSUPPORTED_CHARS:
        cases = {
            "21": f"01{GTIN}21SER{ch}\x1d10L\x1d17280930",
            "90": f"01{GTIN}10L\x1d90INT{ch}\x1d17280930",
            "99": f"01{GTIN}10L\x1d99X{ch}\x1d17280930",
        }
        for ai, raw in cases.items():
            status, body = _post(raw)
            assert status == 422, (ai, desc)
            assert body["code"] == "unsupported_character", (ai, desc)
            assert body["position"] == raw.index(ch), (ai, desc, body["position"])
            assert raw[body["position"]] == ch


def test_unsupported_char_in_readable_internal_90_field() -> None:
    raw = f"(01){GTIN}(10)L(90)内部(17)280930"
    status, body = _post(raw)
    assert status == 422
    assert body["code"] == "unsupported_character"
    assert body["position"] == raw.index("内")


def test_nul_character_rejected_not_silently_dropped() -> None:
    """NUL 不得被截断/吞掉后放行剩余内容。"""
    raw = f"01{GTIN}10AB\x00CD\x1d17280930"
    status, body = _post(raw)
    assert status == 422
    assert body["code"] == "unsupported_character"
    assert body["position"] == raw.index("\x00")
    assert "batch" not in body


def test_newline_inside_field_is_not_scanner_suffix() -> None:
    """只有整串最末尾的 CR/LF 才是行尾；字段中部换行是字符集外字符。"""
    raw = f"01{GTIN}10AB\nCD\x1d17280930"
    status, body = _post(raw)
    assert status == 422
    assert body["code"] == "unsupported_character"
    assert body["position"] == raw.index("\n")


def test_emoji_position_is_code_point_aligned() -> None:
    """emoji 为星面字符（UTF-16 代理对）；位置必须按 Python/JS 码点语义对齐
    到该字符（index 为码点下标），且按可编码内容计数，绝不识别成功。"""
    ch = "😀"
    raw = f"(01){GTIN}(10)LOT{ch}(17)280930"
    status, body = _post(raw)
    assert status == 422
    assert body["code"] == "unsupported_character"
    assert body["position"] == raw.index(ch)
    assert raw[body["position"]] == ch


# ── 长度按可编码标签内容计数 ─────────────────────────────────────────────

def test_length_counts_encodable_chars_not_unicode_codepoints() -> None:
    """多字节字符在长度检查前即被拒绝，不会因“码点计数”把超界数据判为已识别。"""
    # 20 个 ASCII 字符恰好为批号上限，识别成功
    lot20 = "A" * 20
    status, body = _post(f"(01){GTIN}(10){lot20}(17)280930")
    assert status == 200
    assert body["batch"]["lot"] == lot20

    # 21 个 ASCII 字符超界
    status, body = _post(f"(01){GTIN}(10){'A' * 21}(17)280930")
    assert status == 422
    assert "最长 20 位" in body["message"]

    # 19 个可编码字符 + 1 个 emoji：emoji 先被字符集检查拒绝，
    # 而不是按某种 Unicode 计数放行/误报超长
    raw = f"(01){GTIN}(10){'A' * 19}😀(17)280930"
    status, body = _post(raw)
    assert status == 422
    assert body["code"] == "unsupported_character"
    assert raw[body["position"]] == "😀"


def test_internal_90_99_length_limits_count_ascii_only() -> None:
    raw_ok = f"01{GTIN}10L\x1d90{'B' * 30}\x1d17280930"
    status, body = _post(raw_ok)
    assert status == 200

    raw_bad = f"01{GTIN}10L\x1d99{'B' * 91}\x1d17280930"
    status, body = _post(raw_bad)
    assert status == 422
    assert "最长 90 位" in body["message"]


# ── 错误响应结构：稳定错误代码随响应下发，失败不带批次 ───────────────────

def test_error_response_carries_stable_code() -> None:
    status, body = _post(f"(01){GTIN}(10)L\t(17)280930")
    assert status == 422
    assert body["code"] == "unsupported_character"
    assert body["errors"][0]["code"] == "unsupported_character"
    assert isinstance(body["position"], int)
    assert "batch" not in body
    assert "fields" not in body


def test_structural_error_uses_parse_error_code() -> None:
    status, body = _post("(05)123456")
    assert status == 422
    assert body["code"] == "parse_error"
    assert body["errors"][0]["code"] == "parse_error"


def test_rejected_label_never_returns_batch_partial() -> None:
    """任一字段字符集外：整次拒绝，响应不含批次信息，也不产生 fields。"""
    for raw in (
        f"(01){GTIN}(10)BAD\nLOT(17)280930",
        f"01{GTIN}10😀\x1d17280930",
        f"(01){GTIN}(10)L(17)2809😀0",
    ):
        status, body = _post(raw)
        assert status == 422, raw
        assert body["ok"] is False
        assert "batch" not in body, raw


# ── 合法 ASCII 标签：完整向后兼容 ────────────────────────────────────────

def test_printable_ascii_punctuation_in_variable_fields_accepted() -> None:
    """GS1 字母数字集允许的可打印 ASCII 标点均可在批号出现（扫描格式无括号定界）。"""
    lot_punct = "AB12 -_.,/!\"%&*+<>=;"  # 0x20–0x7E 的标点子集（含空格）
    assert len(lot_punct) == 20
    raw = f"01{GTIN}10{lot_punct}\x1d17280930"
    status, body = _post(raw)
    assert status == 200, raw
    assert body["batch"]["lot"] == lot_punct


def test_existing_legal_labels_still_recognized() -> None:
    """既有的合法 ASCII 标签（两种格式、AIM 前缀、行尾后缀、FNC1）全部兼容。"""
    sep_expiry = [
        (READABLE_OK, "2028-09-30"),
        (SCAN_OK, "2028-09-30"),
        (READABLE_OK + "\r\n", "2028-09-30"),
        (SCAN_OK + "\n", "2028-09-30"),
        ("]d2" + SCAN_OK, "2028-09-30"),
        ("]C1" + SCAN_OK, "2028-09-30"),
        (f"01{GTIN}1728093010INK2407", "2028-09-30"),
        (f"01{GTIN}10INK2407\x1d21SER998\x1d17280930", "2028-09-30"),
        (f"(01){GTIN}(10)B-07(17)280200", "2028-02-29"),
    ]
    for raw, expires in sep_expiry:
        status, body = _post(raw)
        assert status == 200, raw
        assert body["batch"]["gtin"] == GTIN
        assert body["batch"]["expires"] == expires


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

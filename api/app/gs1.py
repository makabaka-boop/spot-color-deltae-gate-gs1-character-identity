"""GS1 应用标识符（AI）批次标签解析：扫码原文 → 统一批次信息。

支持两种输入格式：

1. 带括号的可读格式（人工核对/录入）::

    (01)09506000134352(10)INK2407(17)280930

2. 扫码枪原始格式（FNC1 分隔，FNC1 传输为 GS 字符 0x1D）::

    010950600013435210INK2407\\x1d17280930

   定长 AI 按表消费固定字符数；变长 AI 消费到 FNC1 或字符串末尾。
   扫码枪常附加的行尾后缀（\\r / \\n）与 GS1 符号标识符前缀（如 ]d2）会被容忍。

字符集规则（GS1 General Specifications 82 字符字母数字集 = ASCII 0x20–0x7E）：

* 数字 AI 仅接受半角数字 0–9；
* 字母数字变长 AI（批号 10、序列号 21、企业内部 90–99 等）接受 0x20–0x7E，
  即空格、标点、0–9、A–Z、a–z，**空格属于合法字符且含身份**：字段末尾的空格
  不会被剥离（只有整个标签最末尾的 \\r/\\n 扫码行尾会被容忍）；
* 其余一切字符（emoji、NUL、TAB、换行、DEL、非 ASCII 字母/数字等）都按
  GS1 字符集外内容**定位到该字符稳定拒绝**，绝不静默改写或放行。
* 长度按可编码标签内容计数：字符集外的多字节字符在长度检查前即被拒绝，
  因而字符数与 GS1-128 实际承载的符号字符数一致，不会出现“按 Unicode 码点
  计数而超界仍识别”。

可读格式中 '(' 与 ')' 是结构性定界符，不属于任何字段值；扫描格式中 GS(0x1D)
是分隔符。字段值中部出现 TAB 与字段末尾出现 TAB 一视同仁：均为字符集外字符。

无论哪种格式，都按 AI 的定长/变长规则解析后生成统一批次信息：
商品编码 GTIN（AI 01，校验 GS1 校验位）、批号（AI 10）、
失效日期（AI 17，YYMMDD，校验真实日历日期；按 GS1 规范 DD=00 表示当月最后一天）。

解析失败抛出 :class:`Gs1ParseError`，携带首个无法解析的字符位置（相对原始输入，
从 0 计），供前端在原文中高亮定位。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date

# FNC1 在扫码枪输出中的传输字符（Group Separator）
GS = "\x1d"

# 扫码枪可能带出的 AIM 符号标识符前缀（ISO/IEC 15424，GS1 体系码制）
_SYMBOLOGY_PREFIXES = ("]d2", "]C1", "]e0", "]Q3")

# 仅容忍真正的扫码枪行尾后缀（整串末尾的 CR/LF）。
# 注意：空格与 TAB 绝不能列入——带空格的变长字段（如批号）放在末尾时，
# 尾随空格是字段值的一部分；与行尾一起剥离会让两个不同批号得到同一结果。
# 末尾 TAB 也不是扫码行尾，必须与字段中部 TAB 一样按字符集外字符拒绝。
_TRAILING_LINE_ENDINGS = "\r\n"

# GS1 数字字段只允许 ASCII 数字 0–9。
# str.isdigit()/int() 会接受阿拉伯文数字（٠١٢…）、全角数字（０１２…）等
# Unicode 数字，必须显式限定字符集，否则这类标签会被错误识别。
_ASCII_DIGITS = frozenset("0123456789")

# GS1 字母数字字符集（General Specifications 表 7.11–1 的 82 字符集）：
# 所有可打印 ASCII 字符 0x20–0x7E（含空格与标点），码点即条码承载的符号字符，
# 长度按此计数与实际承载一致。
_GS1_ALNUM_MIN, _GS1_ALNUM_MAX = 0x20, 0x7E

# 机器可读错误代码：调用方（自动化验收）据此逐项核对失败类别，
# 不再依赖中文 message 子串。
ERR_UNSUPPORTED_CHAR = "unsupported_character"


class Gs1ParseError(ValueError):
    """标签解析失败；position 为首个无法解析的字符在原始输入中的下标（0 起）。

    code 为稳定错误代码（默认 parse_error；字符集外字符为 unsupported_character）。
    """

    def __init__(self, message: str, position: int, code: str = "parse_error") -> None:
        super().__init__(message)
        self.message = message
        self.position = position
        self.code = code


@dataclass(frozen=True)
class AiSpec:
    """一个应用标识符的格式规则。fixed 为 None 表示变长。"""

    ai: str
    label: str
    fixed: int | None
    max_len: int
    numeric: bool = False


# 本系统识别的应用标识符表（定长/变长规则见 GS1 通用规范）
_AI_LIST = [
    AiSpec("00", "SSCC 物流单元标识", 18, 18, numeric=True),
    AiSpec("01", "商品编码 GTIN", 14, 14, numeric=True),
    AiSpec("02", "内装物 GTIN", 14, 14, numeric=True),
    AiSpec("10", "批号", None, 20),
    AiSpec("11", "生产日期", 6, 6, numeric=True),
    AiSpec("12", "付款截止日", 6, 6, numeric=True),
    AiSpec("13", "包装日期", 6, 6, numeric=True),
    AiSpec("15", "最佳食用期", 6, 6, numeric=True),
    AiSpec("16", "销售截止日", 6, 6, numeric=True),
    AiSpec("17", "失效日期", 6, 6, numeric=True),
    AiSpec("20", "产品变体", 2, 2, numeric=True),
    AiSpec("21", "序列号", None, 20),
    AiSpec("30", "数量", None, 8, numeric=True),
    AiSpec("37", "内装数量", None, 8, numeric=True),
    AiSpec("90", "双方约定的内部信息", None, 30),
    AiSpec("240", "附加产品标识", None, 30),
    AiSpec("241", "客户零件号", None, 30),
    AiSpec("242", "定制产品变体号", None, 6, numeric=True),
    AiSpec("250", "二级序列号", None, 30),
    AiSpec("251", "源实体参考", None, 30),
    AiSpec("400", "客户订单号", None, 30),
    AiSpec("401", "货物托运代码", None, 30),
    AiSpec("403", "路由代码", None, 30),
    AiSpec("410", "交货地 GLN", 13, 13, numeric=True),
    AiSpec("411", "开票方 GLN", 13, 13, numeric=True),
    AiSpec("412", "供货方 GLN", 13, 13, numeric=True),
    AiSpec("413", "收货方 GLN", 13, 13, numeric=True),
    AiSpec("414", "物理位置 GLN", 13, 13, numeric=True),
    AiSpec("415", "开票方 GLN（单一）", 13, 13, numeric=True),
    AiSpec("416", "生产地 GLN", 13, 13, numeric=True),
    AiSpec("417", "当事方 GLN", 13, 13, numeric=True),
    AiSpec("420", "收货方邮政编码", None, 20),
    # 91–99：企业内部信息，变长
    *[AiSpec(f"9{i}", "企业内部信息", None, 90) for i in range(1, 10)],
]

AI_SPECS: dict[str, AiSpec] = {spec.ai: spec for spec in _AI_LIST}

# 生成统一批次信息所必需的 AI
_REQUIRED = ("01", "10", "17")


@dataclass(frozen=True)
class ParsedField:
    ai: str
    label: str
    value: str
    position: int  # 值在原始输入中的起始下标（0 起）


def _describe_char(ch: str) -> str:
    """描述一个字符集外字符，控制字符给出 U+XXXX 与名称线索，避免不可见呈现。"""
    cp = ord(ch)
    if cp < 0x20:
        names = {
            0x00: "NUL",
            0x09: "TAB",
            0x0A: "LF(换行)",
            0x0D: "CR(回车)",
            0x1D: "GS(FNC1)",
        }
        name = names.get(cp, "控制字符")
        return f"U+{cp:04X} {name}"
    if cp == 0x7F:
        return "U+007F DEL"
    return f"U+{cp:04X} {ch!r}"


def _validate_value(spec: AiSpec, value: str, pos: int) -> None:
    """按字符集与定长/变长规则校验一个字段值，pos 为值起始下标。

    字符集检查先于长度检查：字符集外字符（可能是多字节）按其所在位置拒绝，
    保证长度只对可编码标签内容计数；首个问题字符的位置即返回位置。
    """
    if len(value) == 0:
        if spec.fixed is not None:
            raise Gs1ParseError(
                f"AI ({spec.ai}) {spec.label}为定长 {spec.fixed} 位，实际 0 位",
                pos,
            )
        raise Gs1ParseError(f"AI ({spec.ai}) {spec.label}为变长字段，内容为空", pos)

    if spec.numeric:
        for k, ch in enumerate(value):
            if ch not in _ASCII_DIGITS:
                raise Gs1ParseError(
                    f"AI ({spec.ai}) {spec.label}应为纯数字（仅限 0-9），"
                    f"此处出现 GS1 字符集外字符 {_describe_char(ch)}",
                    pos + k,
                    code=ERR_UNSUPPORTED_CHAR,
                )
    else:
        for k, ch in enumerate(value):
            if not (_GS1_ALNUM_MIN <= ord(ch) <= _GS1_ALNUM_MAX):
                raise Gs1ParseError(
                    f"AI ({spec.ai}) {spec.label}含 GS1 字符集外字符"
                    f"（仅允许 ASCII 0x20–0x7E，空格允许且含尾随空格）："
                    f"{_describe_char(ch)}",
                    pos + k,
                    code=ERR_UNSUPPORTED_CHAR,
                )

    if spec.fixed is not None:
        if len(value) != spec.fixed:
            raise Gs1ParseError(
                f"AI ({spec.ai}) {spec.label}为定长 {spec.fixed} 位，实际 {len(value)} 位",
                pos,
            )
    elif len(value) > spec.max_len:
        raise Gs1ParseError(
            f"AI ({spec.ai}) {spec.label}最长 {spec.max_len} 位，实际 {len(value)} 位",
            pos + spec.max_len,
        )


def _parse_readable(text: str, base: int) -> list[ParsedField]:
    """解析带括号的可读格式：(01)...(10)...(17)..."""
    fields: list[ParsedField] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] != "(":
            raise Gs1ParseError("可读格式中此处应为 '(' 开始新的应用标识符", base + i)
        close = text.find(")", i + 1)
        if close == -1:
            raise Gs1ParseError("缺少与 '(' 配对的 ')'", base + i)
        ai = text[i + 1 : close]
        spec = AI_SPECS.get(ai)
        if not ai.isdigit() or spec is None:
            raise Gs1ParseError(f"未知应用标识符 ({ai})", base + i + 1)
        nxt = text.find("(", close + 1)
        value = text[close + 1 :] if nxt == -1 else text[close + 1 : nxt]
        pos = close + 1
        _validate_value(spec, value, base + pos)
        fields.append(ParsedField(ai, spec.label, value, base + pos))
        i = n if nxt == -1 else nxt
    return fields


def _match_ai(text: str, i: int) -> AiSpec | None:
    """在 i 处按已知 AI 表做最长前缀匹配（AI 为 2–4 位数字）。"""
    for length in (4, 3, 2):
        spec = AI_SPECS.get(text[i : i + length])
        if spec is not None:
            return spec
    return None


def _parse_scan(text: str, base: int) -> list[ParsedField]:
    """解析扫码枪原始格式：定长 AI 按表消费，变长 AI 以 FNC1(GS) 或末尾结束。"""
    fields: list[ParsedField] = []
    i, n = 0, len(text)
    while i < n:
        if text[i] == GS:
            raise Gs1ParseError("此处不应出现 FNC1 分隔符（字段之间多余的 GS）", base + i)
        spec = _match_ai(text, i)
        if spec is None:
            raise Gs1ParseError(
                f"无法识别的应用标识符（此处字符 {text[i:i+4]!r}）", base + i
            )
        i += len(spec.ai)
        if spec.fixed is not None:
            value = text[i : i + spec.fixed]
            if GS in value:
                raise Gs1ParseError(
                    f"AI ({spec.ai}) {spec.label}为定长 {spec.fixed} 位，"
                    "字段内出现 FNC1 分隔符，数据不足",
                    base + i + value.index(GS),
                )
            if len(value) < spec.fixed:
                raise Gs1ParseError(
                    f"AI ({spec.ai}) {spec.label}需要定长 {spec.fixed} 位，"
                    f"数据不足（仅 {len(value)} 位）",
                    base + i,
                )
            pos = i
            i += spec.fixed
        else:
            end = text.find(GS, i)
            if end == -1:
                value, pos, i = text[i:], i, n
            else:
                value, pos, i = text[i:end], i, end + 1
        _validate_value(spec, value, base + pos)
        fields.append(ParsedField(spec.ai, spec.label, value, base + pos))
    return fields


def _check_gtin(value: str, pos: int) -> None:
    """校验 GTIN 的 GS1 校验位（模 10，权重自右向左 3、1 交替）。"""
    digits = [int(c) for c in value]
    n = len(digits)
    total = sum(
        d * (3 if (n - 1 - i) % 2 == 1 else 1) for i, d in enumerate(digits[:-1])
    )
    expected = (10 - total % 10) % 10
    if expected != digits[-1]:
        raise Gs1ParseError(
            f"商品编码校验位错误：按前 {n - 1} 位计算应为 {expected}，实际为 {digits[-1]}",
            pos + n - 1,
        )


def _resolve_year(yy: int, today: date | None = None) -> int:
    """两位年份按 GS1 滑动窗口还原：落在 [今年-49, 今年+50] 区间内。"""
    today = today or date.today()
    year = today.year // 100 * 100 + yy
    if year > today.year + 50:
        year -= 100
    elif year < today.year - 49:
        year += 100
    return year


def _parse_expiry(value: str, pos: int) -> str:
    """AI (17) YYMMDD → ISO 日期；校验真实日历日期，DD=00 表示当月最后一天。"""
    yy, mm, dd = int(value[0:2]), int(value[2:4]), int(value[4:6])
    year = _resolve_year(yy)
    if not 1 <= mm <= 12:
        raise Gs1ParseError(f"失效日期月份 {mm:02d} 不存在（允许 01–12）", pos + 2)
    last_day = calendar.monthrange(year, mm)[1]
    if dd == 0:
        day = last_day  # GS1 规范：日字段 00 表示当月最后一天
    elif dd > last_day:
        raise Gs1ParseError(
            f"失效日期 {year}-{mm:02d}-{dd:02d} 不是真实日历日期"
            f"（该月只有 {last_day} 天）",
            pos + 4,
        )
    else:
        day = dd
    return date(year, mm, day).isoformat()


def _build_batch(fields: list[ParsedField], end_pos: int) -> dict[str, str]:
    """从已解析字段生成统一批次信息；缺失/重复/校验失败均拒绝。"""
    by_ai: dict[str, ParsedField] = {}
    for field in fields:
        if field.ai in by_ai:
            raise Gs1ParseError(
                f"应用标识符 ({field.ai}) {field.label}重复出现", field.position
            )
        by_ai[field.ai] = field

    for ai in _REQUIRED:
        if ai not in by_ai:
            label = AI_SPECS[ai].label
            raise Gs1ParseError(
                f"标签缺少批次核验必需的 AI ({ai}) {label}", end_pos
            )

    gtin = by_ai["01"]
    _check_gtin(gtin.value, gtin.position)
    expires = _parse_expiry(by_ai["17"].value, by_ai["17"].position)

    return {
        "gtin": gtin.value,
        "lot": by_ai["10"].value,
        "expires": expires,
    }


def parse_gs1_label(raw: str) -> dict[str, object]:
    """解析标签原文，返回统一批次信息；失败抛 Gs1ParseError（含位置）。

    返回::

        {
            "format": "readable" | "scan",
            "fields": [{"ai", "label", "value", "position"}, ...],
            "batch": {"gtin", "lot", "expires"},
        }
    """
    text = raw
    base = 0
    for prefix in _SYMBOLOGY_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            base += len(prefix)
            break
    # 只容忍真正的扫码枪行尾后缀（整串末尾的 CR/LF）；
    # 不剥离空格与 TAB——末尾变长字段的尾随空格是字段值，TAB 属字符集外字符。
    # 只裁整串末尾，位置下标仍与原文对齐。
    text = text.rstrip(_TRAILING_LINE_ENDINGS)

    if not text:
        raise Gs1ParseError("标签内容为空", base)

    if text.startswith("("):
        fmt = "readable"
        fields = _parse_readable(text, base)
    else:
        fmt = "scan"
        fields = _parse_scan(text, base)

    batch = _build_batch(fields, base + len(text))
    return {
        "format": fmt,
        "fields": [
            {"ai": f.ai, "label": f.label, "value": f.value, "position": f.position}
            for f in fields
        ],
        "batch": batch,
    }

import { useState } from "react";
import { postGs1Label } from "./api";
import type { Gs1BatchInfo, Gs1LabelFormat } from "./types";

/**
 * 批次标签核验区：粘贴扫码枪读出的 GS1 标签原文，核验商品编码、批号、失效日期。
 *
 * 状态机：待输入(idle) → 已识别(recognized) / 已拒绝(rejected)。
 * 识别失败保留原文并高亮首个无法解析的位置；成功后可直接扫描下一桶。
 * 本区域状态完全独立，不读写标准色、样张与色差结论。
 */

type LabelStatus = "idle" | "recognized" | "rejected";

interface RejectInfo {
  message: string;
  position: number | null;
  code?: string;
}

const STATUS_TEXT: Record<LabelStatus, string> = {
  idle: "待输入",
  recognized: "已识别",
  rejected: "已拒绝",
};

const FORMAT_TEXT: Record<Gs1LabelFormat, string> = {
  readable: "带括号可读格式",
  scan: "扫码格式（FNC1 分隔）",
};

/**
 * 把所有不可打印控制字符映射为 U+2400 控制图形区的可见符号（1 码点 → 1 码点），
 * 使 NUL/换行/TAB 等进入响应后无法再隐身、折行或与普通值同形。
 * 可打印字符（含空格、emoji）原样保留，因此码点下标与原文严格一致。
 */
function visibleChar(ch: string): string {
  const cp = ch.codePointAt(0) ?? 0;
  if (cp >= 0x00 && cp <= 0x1f) {
    return String.fromCodePoint(0x2400 + cp); // ␀..␟（如 NUL→␀, TAB→␉, LF→␊, GS→␝）
  }
  if (cp === 0x7f) {
    return "␡"; // DEL
  }
  return ch;
}

function visibleRaw(raw: string): string {
  return Array.from(raw, visibleChar).join("");
}

/**
 * 已识别字段值中的空格显示为 ␠（SYMBOL FOR SPACE），使字段首尾与中部的空格
 * 对收料员可见、可逐字符核对；精确原值通过 title/data-value 保留。
 */
function visibleValue(value: string): string {
  return Array.from(value, (ch) => (ch === " " ? "␠" : visibleChar(ch))).join(
    "",
  );
}

/** 按码点（而非 UTF-16 代码单元）切片，保证 emoji 等星面字符不被代理对拆散。 */
function sliceCodePoints(text: string, start: number, end?: number): string {
  const chars = Array.from(text);
  return chars.slice(start, end).join("");
}

/** 在原文中高亮首个无法解析的位置（码点对齐）；位置越出末尾时给出末尾标记。 */
function HighlightedRaw({ raw, position }: { raw: string; position: number }) {
  const shown = visibleRaw(raw);
  if (position >= Array.from(raw).length) {
    return (
      <pre className="raw-highlight" data-testid="label-raw-highlight">
        {shown}
        <mark data-testid="label-error-char">⇤末尾</mark>
      </pre>
    );
  }
  return (
    <pre className="raw-highlight" data-testid="label-raw-highlight">
      {sliceCodePoints(shown, 0, position)}
      <mark data-testid="label-error-char">
        {Array.from(shown)[position]}
      </mark>
      {sliceCodePoints(shown, position + 1)}
    </pre>
  );
}

/** 展示一个已识别批次值：空格可见、控制字符不可能出现（后端已拒绝），原值可复核。 */
function BatchValue({
  testid,
  value,
}: {
  testid: string;
  value: string;
}) {
  return (
    <dd data-testid={testid} title={`精确原值（${Array.from(value).length} 个字符）：${visibleValue(value)}`}>
      <span data-testid={`${testid}-exact`} data-value={value}>
        {visibleValue(value)}
      </span>
    </dd>
  );
}

export function BatchLabelPanel() {
  const [raw, setRaw] = useState("");
  const [status, setStatus] = useState<LabelStatus>("idle");
  const [batch, setBatch] = useState<Gs1BatchInfo | null>(null);
  const [format, setFormat] = useState<Gs1LabelFormat | null>(null);
  const [reject, setReject] = useState<RejectInfo | null>(null);
  const [loading, setLoading] = useState(false);

  function clearOutcome() {
    setStatus("idle");
    setBatch(null);
    setFormat(null);
    setReject(null);
  }

  function handleChange(value: string) {
    // 原文一经修改，既有结论（已识别/已拒绝）即与框内文本失去对应，
    // 必须立即回到待输入并清除旧批次信息与错误定位，等待重新核验。
    setRaw(value);
    clearOutcome();
  }

  async function handleVerify() {
    setLoading(true);
    const outcome = await postGs1Label(raw);
    setLoading(false);
    if (outcome.ok) {
      setStatus("recognized");
      setBatch(outcome.data.batch);
      setFormat(outcome.data.format);
      setReject(null);
    } else {
      // 识别失败：原文不清空，展示错误与首个无法解析的位置
      setStatus("rejected");
      setBatch(null);
      setFormat(null);
      const err = outcome.error;
      setReject({
        message: err?.message ?? `请求失败（HTTP ${outcome.status}）`,
        position: err?.position ?? null,
        code: err?.code,
      });
    }
  }

  function handleNextBucket() {
    // 继续扫描下一桶：清空输入和当前核验结论。
    setRaw("");
    clearOutcome();
  }

  return (
    <section className="label-panel" data-testid="label-panel">
      <h2>批次标签核验（GS1）</h2>
      <p className="hint-block">
        收料时粘贴扫码枪读出的油墨桶标签原文，支持带括号可读格式
        <code>(01)…(10)…(17)…</code> 与含 FNC1 分隔符的扫描格式。
        本区域只核验标签，不影响上方色差比对。
      </p>

      <div className="label-status-row">
        <span
          className={`label-status status-${status}`}
          data-testid="label-status"
          data-status={status}
        >
          {STATUS_TEXT[status]}
        </span>
        {format && status === "recognized" && (
          <span className="label-format" data-testid="label-format">
            {FORMAT_TEXT[format]}
          </span>
        )}
      </div>

      <label className="field label-input" htmlFor="label-raw">
        <span className="field-label">标签原文</span>
        <textarea
          id="label-raw"
          data-testid="label-raw"
          rows={3}
          autoComplete="off"
          spellCheck={false}
          placeholder="例如 (01)09506000134352(10)INK2407(17)280930"
          value={raw}
          disabled={loading}
          onChange={(e) => handleChange(e.target.value)}
        />
      </label>

      <div className="actions">
        <button
          type="button"
          className="primary"
          data-testid="label-verify"
          onClick={handleVerify}
          disabled={loading || raw.trim() === ""}
        >
          {loading ? "核验中…" : "核验标签"}
        </button>
        {status === "recognized" && (
          <button
            type="button"
            data-testid="label-next"
            onClick={handleNextBucket}
            disabled={loading}
          >
            扫描下一桶
          </button>
        )}
      </div>

      {status === "rejected" && reject && (
        <div className="server-errors" data-testid="label-error" role="alert">
          <strong>{reject.message}</strong>
          {reject.code === "unsupported_character" && (
            <div className="error-code" data-testid="label-error-code">
              错误代码：unsupported_character（GS1 字符集外字符，已整次拒绝，未生成批次信息）
            </div>
          )}
          {/* position 可能为 0（首字符即无法解析），必须显式判空，不能用真值判断 */}
          {reject.position !== null && reject.position !== undefined && (
            <div className="error-position" data-testid="label-error-position">
              首个无法解析的位置：第 {reject.position + 1} 个字符
            </div>
          )}
          {reject.position !== null && reject.position !== undefined && (
            <>
              <HighlightedRaw raw={raw} position={reject.position} />
              <p className="control-char-note" data-testid="label-control-note">
                原文中的控制字符按控制图形符号显示（如 ␀=NUL、␉=TAB、␊=换行、␍=回车、␝=FNC1、␡=DEL），
                不会隐身或折行。
              </p>
            </>
          )}
        </div>
      )}

      {status === "recognized" && batch && (
        <div className="label-result" data-testid="label-result" aria-live="polite">
          <dl>
            <div>
              <dt>商品编码 (GTIN)</dt>
              <BatchValue testid="label-gtin" value={batch.gtin} />
            </div>
            <div>
              <dt>批号</dt>
              <BatchValue testid="label-lot" value={batch.lot} />
            </div>
            <div>
              <dt>失效日期</dt>
              <BatchValue testid="label-expires" value={batch.expires} />
            </div>
          </dl>
          {(batch.gtin.includes(" ") ||
            batch.lot.includes(" ") ||
            batch.expires.includes(" ")) && (
            <p className="value-space-note" data-testid="label-space-note">
              值中的 <span className="space-glyph">␠</span> 表示真实空格字符（含字段首尾空格），
              悬停可查看精确原值与字符数。
            </p>
          )}
        </div>
      )}
    </section>
  );
}

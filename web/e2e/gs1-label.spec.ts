import { expect, test, type Page } from "@playwright/test";

/**
 * 批次标签核验区端到端：浏览器 → nginx → FastAPI，真实服务无打桩。
 * 覆盖一次有效扫描（FNC1 扫描格式）、一次损坏标签重试（可读格式），
 * 尾随空格身份、字段中部/末尾控制符、多字节字符、稳定错误代码与页面可见文本，
 * 并确认原有色差主流程在标签核验失败时仍可独立完成。
 */

// 合法标签：GTIN 09506000134352（校验位 2）、批号 INK2407、失效日期 2028-09-30
const SCAN_OK = "010950600013435210INK2407\u001d17280930"; // FNC1 = GS 控制字符
const READABLE_BAD_CHECK = "(01)09506000134353(10)INK2407(17)280930"; // 校验位应为 2
const READABLE_OK = "(01)09506000134352(10)INK2407(17)280930";

// Sharma 参考对 #25：ΔE00 = 1.2644（放行）
const PASS_PAIR = {
  standard: [60.2574, -34.0099, 36.2677],
  sample: [60.4626, -34.1751, 39.4387],
};

/** 提交标签并抓取后端响应，供逐项核对 HTTP 状态、code、position 与 batch。 */
async function verifyAndCapture(
  page: Page,
  raw: string,
): Promise<{ status: number; body: any }> {
  const request = page.waitForResponse((r) =>
    r.url().includes("/api/gs1-label"),
  );
  await page.getByTestId("label-raw").fill(raw);
  await page.getByTestId("label-verify").click();
  const res = await request;
  return { status: res.status(), body: await res.json() };
}

test.beforeEach(async ({ page }) => {
  await page.goto("/");
});

test("有效扫描：FNC1 扫描格式识别为商品编码、批号、失效日期，可继续扫描下一桶", async ({
  page,
}) => {
  await expect(page.getByTestId("label-status")).toHaveText("待输入");

  await page.getByTestId("label-raw").fill(SCAN_OK);
  await page.getByTestId("label-verify").click();

  await expect(page.getByTestId("label-status")).toHaveText("已识别");
  await expect(page.getByTestId("label-format")).toContainText("扫码格式");
  await expect(page.getByTestId("label-gtin")).toHaveText("09506000134352");
  await expect(page.getByTestId("label-lot")).toHaveText("INK2407");
  await expect(page.getByTestId("label-expires")).toHaveText("2028-09-30");

  // 继续扫描下一桶：回到待输入，输入框清空
  await page.getByTestId("label-next").click();
  await expect(page.getByTestId("label-status")).toHaveText("待输入");
  await expect(page.getByTestId("label-raw")).toHaveValue("");
  await expect(page.getByTestId("label-result")).toHaveCount(0);
});

test("损坏标签重试：校验位错误被拒绝并定位，修正后识别成功", async ({ page }) => {
  await page.getByTestId("label-raw").fill(READABLE_BAD_CHECK);
  await page.getByTestId("label-verify").click();

  // 已拒绝：保留原文，指出首个无法解析的位置（校验位 = 第 18 个字符）
  await expect(page.getByTestId("label-status")).toHaveText("已拒绝");
  await expect(page.getByTestId("label-error")).toContainText("校验位");
  await expect(page.getByTestId("label-error-position")).toContainText(
    "第 18 个字符",
  );
  await expect(page.getByTestId("label-error-char")).toHaveText("3");
  await expect(page.getByTestId("label-raw")).toHaveValue(READABLE_BAD_CHECK);

  // 修正校验位后重试 → 已识别
  await page.getByTestId("label-raw").fill(READABLE_OK);
  await expect(page.getByTestId("label-status")).toHaveText("待输入");
  await page.getByTestId("label-verify").click();
  await expect(page.getByTestId("label-status")).toHaveText("已识别");
  await expect(page.getByTestId("label-gtin")).toHaveText("09506000134352");
  await expect(page.getByTestId("label-error")).toHaveCount(0);
});

test("商品编码含阿拉伯文数字字符：定位该字符并拒绝标签", async ({ page }) => {
  // 校验位为阿拉伯文数字 ٢（U+0662，数值恰等于正确校验位 2）也必须拒绝
  const arabicDigitLabel = "(01)0950600013435٢(10)INK2407(17)280930";
  await page.getByTestId("label-raw").fill(arabicDigitLabel);
  await page.getByTestId("label-verify").click();

  await expect(page.getByTestId("label-status")).toHaveText("已拒绝");
  await expect(page.getByTestId("label-error")).toContainText("纯数字");
  await expect(page.getByTestId("label-error-position")).toContainText(
    "第 18 个字符",
  );
  await expect(page.getByTestId("label-error-char")).toHaveText("٢");
  await expect(page.getByTestId("label-raw")).toHaveValue(arabicDigitLabel);
});

test("首字符即无法解析（05XYZ）：显示“第 1 个字符”提示并高亮首字符", async ({
  page,
}) => {
  await page.getByTestId("label-raw").fill("05XYZ");
  await page.getByTestId("label-verify").click();

  await expect(page.getByTestId("label-status")).toHaveText("已拒绝");
  await expect(page.getByTestId("label-error-position")).toContainText(
    "第 1 个字符",
  );
  await expect(page.getByTestId("label-error-char")).toHaveText("0");
  await expect(page.getByTestId("label-raw")).toHaveValue("05XYZ");
});

test("已识别后追加字符未再核验：回到待输入，不再展示上一桶批次信息", async ({
  page,
}) => {
  await page.getByTestId("label-raw").fill(READABLE_OK);
  await page.getByTestId("label-verify").click();
  await expect(page.getByTestId("label-status")).toHaveText("已识别");
  await expect(page.getByTestId("label-gtin")).toHaveText("09506000134352");

  // 追加字符后不再次核验：旧批次信息必须立即消失，状态回到待输入
  await page.getByTestId("label-raw").fill(`${READABLE_OK}X`);
  await expect(page.getByTestId("label-status")).toHaveText("待输入");
  await expect(page.getByTestId("label-result")).toHaveCount(0);
  await expect(page.getByTestId("label-gtin")).toHaveCount(0);
});

test("色差主流程独立：标签被拒绝不影响已有结论，比对可再次完成", async ({ page }) => {
  test.setTimeout(90_000); // 含两次色差提交与一次标签核验，低端 CI 上串行较慢
  // 先完成一次色差比对（放行）
  await page.getByTestId("standard.L").fill(String(PASS_PAIR.standard[0]));
  await page.getByTestId("standard.a").fill(String(PASS_PAIR.standard[1]));
  await page.getByTestId("standard.b").fill(String(PASS_PAIR.standard[2]));
  await page.getByTestId("sample.L").fill(String(PASS_PAIR.sample[0]));
  await page.getByTestId("sample.a").fill(String(PASS_PAIR.sample[1]));
  await page.getByTestId("sample.b").fill(String(PASS_PAIR.sample[2]));
  await page.getByTestId("compare-button").click();
  await expect(page.getByTestId("result-panel")).toHaveAttribute(
    "data-passed",
    "true",
  );

  // 标签核验失败：色差结论与输入原样保留
  await page.getByTestId("label-raw").fill(READABLE_BAD_CHECK);
  await page.getByTestId("label-verify").click();
  await expect(page.getByTestId("label-status")).toHaveText("已拒绝");
  await expect(page.getByTestId("result-panel")).toBeVisible();
  await expect(page.getByTestId("verdict")).toContainText("放行");
  await expect(page.getByTestId("standard.L")).toHaveValue(
    String(PASS_PAIR.standard[0]),
  );

  // 标签核验区处于已拒绝状态时，色差主流程仍可独立再次完成
  await page.getByTestId("compare-button").click();
  await expect(page.getByTestId("result-panel")).toHaveAttribute(
    "data-passed",
    "true",
  );
  await expect(page.getByTestId("metric-delta")).toContainText("1.26");
});

test.describe("尾随空格：变长字段身份逐字符保留（两种格式）", () => {
  test("可读格式：批号尾随空格保留，与无空格批号得到不同批次值", async ({
    page,
  }) => {
    const withSpace = "(01)09506000134352(17)280930(10)INK2407 ";
    const r1 = await verifyAndCapture(page, withSpace);
    expect(r1.status).toBe(200);
    expect(r1.body.batch.lot).toBe("INK2407 ");
    await expect(page.getByTestId("label-status")).toHaveText("已识别");
    // 页面可见文本：尾随空格显形为 ␠，不再不可辨认
    await expect(page.getByTestId("label-lot")).toHaveText("INK2407␠");
    await expect(page.getByTestId("label-lot-exact")).toHaveAttribute(
      "data-value",
      "INK2407 ",
    );

    const r2 = await verifyAndCapture(
      page,
      "(01)09506000134352(17)280930(10)INK2407",
    );
    expect(r2.status).toBe(200);
    expect(r2.body.batch.lot).toBe("INK2407");
    expect(r1.body.batch.lot).not.toBe(r2.body.batch.lot);
    await expect(page.getByTestId("label-lot")).toHaveText("INK2407");
  });

  test("扫码格式：批号尾随空格 + 扫码行尾 CRLF：空格保留、行尾容忍", async ({
    page,
  }) => {
    const { status, body } = await verifyAndCapture(
      page,
      "01095060001343521728093010INK2407 \r\n",
    );
    expect(status).toBe(200);
    expect(body.format).toBe("scan");
    expect(body.batch.lot).toBe("INK2407 ");
    await expect(page.getByTestId("label-lot")).toHaveText("INK2407␠");
  });
});

test.describe("控制字符：字段中部与末尾一致拒绝，页面可见", () => {
  test("TAB 在批号中部与末尾均拒绝：422 / unsupported_character / 定位 / ␉ 可见（两种格式）", async ({
    page,
  }) => {
    test.setTimeout(120_000); // 本用例串行提交 4 次，低端 CI 下单次较慢
    const cases = [
      { raw: "(01)09506000134352(10)AB\tCD(17)280930", pos: 24 },
      { raw: "(01)09506000134352(17)280930(10)ABCD\t", pos: 36 },
      { raw: "010950600013435210AB\tCD17280930", pos: 20 },
      { raw: "01095060001343521728093010ABCD\t", pos: 30 },
    ];
    for (const { raw, pos } of cases) {
      const { status, body } = await verifyAndCapture(page, raw);
      expect(status).toBe(422);
      expect(body.ok).toBe(false);
      expect(body.code).toBe("unsupported_character");
      expect(body.position).toBe(pos);
      expect("batch" in body).toBe(false);

      await expect(page.getByTestId("label-status")).toHaveText("已拒绝");
      await expect(page.getByTestId("label-error-code")).toContainText(
        "unsupported_character",
      );
      await expect(page.getByTestId("label-error-position")).toContainText(
        `第 ${pos + 1} 个字符`,
      );
      await expect(page.getByTestId("label-error-char")).toHaveText("␉");
      await expect(page.getByTestId("label-result")).toHaveCount(0);
      // 原文保留在输入框
      await expect(page.getByTestId("label-raw")).toHaveValue(raw);
    }
  });

  test("NUL 与换行进入响应后可见化为 ␀ / ␊，不隐身不折行", async ({ page }) => {
    test.setTimeout(90_000); // 串行提交 2 次，低端 CI 下单次较慢
    const nul = "010950600013435210AB\x00CD17280930";
    let r = await verifyAndCapture(page, nul);
    expect(r.status).toBe(422);
    expect(r.body.code).toBe("unsupported_character");
    expect(r.body.position).toBe(20);
    await expect(page.getByTestId("label-error-char")).toHaveText("␀");

    const lf = "010950600013435210AB\nCD17280930";
    r = await verifyAndCapture(page, lf);
    expect(r.status).toBe(422);
    expect(r.body.code).toBe("unsupported_character");
    expect(r.body.position).toBe(20);
    await expect(page.getByTestId("label-error-char")).toHaveText("␊");
    // 换行以图形符号呈现，错误块内部不产生真实断行错位
    await expect(page.getByTestId("label-raw-highlight")).toContainText("AB␊CD");
  });
});

test.describe("多字节字符：GS1 字符集外，稳定拒绝且码点定位", () => {
  for (const [name, ch] of [
    ["emoji 😀", "😀"],
    ["中文", "墨"],
    ["全角空格", "　"],
  ] as const) {
    test(`${name}：两种格式均 422 / unsupported_character`, async ({ page }) => {
      test.setTimeout(90_000); // 串行提交 2 次，低端 CI 下单次较慢
      const readable = `(01)09506000134352(10)LOT${ch}(17)280930`;
      const pos = Array.from(readable).indexOf(ch);
      let r = await verifyAndCapture(page, readable);
      expect(r.status).toBe(422);
      expect(r.body.code).toBe("unsupported_character");
      expect(r.body.position).toBe(pos);
      await expect(page.getByTestId("label-error-char")).toHaveText(ch);

      const scan = `010950600013435210LOT${ch}17280930`;
      const posScan = Array.from(scan).indexOf(ch);
      r = await verifyAndCapture(page, scan);
      expect(r.status).toBe(422);
      expect(r.body.code).toBe("unsupported_character");
      expect(r.body.position).toBe(posScan);
      await expect(page.getByTestId("label-error-char")).toHaveText(ch);
    });
  }

  test("序列号 21 与企业内部 90/99 字段含 emoji 同样拒绝", async ({ page }) => {
    test.setTimeout(120_000); // 串行提交 3 次，低端 CI 下单次较慢
    const cases = [
      "010950600013435221SER😀10L17280930",
      "010950600013435210L90内部😀17280930",
      "(01)09506000134352(10)L(90)内部(17)280930",
    ];
    for (const raw of cases) {
      const { status, body } = await verifyAndCapture(page, raw);
      expect(status).toBe(422);
      expect(body.code).toBe("unsupported_character");
      expect("batch" in body).toBe(false);
      await expect(page.getByTestId("label-status")).toHaveText("已拒绝");
    }
  });
});

test.describe("合法 ASCII 标签与 AIM 前缀、FNC1、行尾后缀保持兼容", () => {
  const legalLabels = [
    ["FNC1 扫描格式", SCAN_OK, "scan"],
    ["带括号可读格式", READABLE_OK, "readable"],
    ["AIM 前缀 ]d2", "]d2" + SCAN_OK, "scan"],
    ["扫描格式 + CRLF 行尾", SCAN_OK + "\r\n", "scan"],
    ["额外序列号字段", "010950600013435210INK240721SER99817280930", "scan"],
  ] as const;

  for (const [name, raw, fmt] of legalLabels) {
    test(name, async ({ page }) => {
      const { status, body } = await verifyAndCapture(page, raw);
      expect(status).toBe(200);
      expect(body.format).toBe(fmt);
      expect(body.batch).toEqual({
        gtin: "09506000134352",
        lot: "INK2407",
        expires: "2028-09-30",
      });
      await expect(page.getByTestId("label-status")).toHaveText("已识别");
      // 合法 ASCII 值不出现空格可见化提示
      await expect(page.getByTestId("label-space-note")).toHaveCount(0);
    });
  }
});

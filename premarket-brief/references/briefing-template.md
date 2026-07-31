# Briefing template & output rules

The output spec for premarket-brief's step 5 (synthesize). SKILL.md is the
process; this is how the finished brief should look and read.

2026-08-01 redesign: the old 9-section English long-form averaged ~300 dense
lines and the reader stopped reading it. The brief now leads with a glyph
dashboard and a plain-Chinese interpretation; everything below them is capped.
A briefing nobody reads is worth exactly as much as one nobody grades.

## Language & length contract

- **Write the briefing in 简体中文** for a reader with **no finance
  background**. Tickers, index names, and data-source names stay in their
  original form. Times in ET with Beijing time alongside (header + event rows).
- **Translate every term of art in place, the moment it first appears**:
  "VIX 17.3(市场紧张指数,20 以下算平静)"、"contango(近期合约比远期便宜,
  说明市场不觉得眼下有事)"、"breadth(还有多少只股票在涨)"、"防御轮动
  (钱躲进水电、超市、制药这类避险股)"、"鹰派(主张加息压通胀)"。A term
  the reader must look up is a term that didn't get communicated.
- **Hard length caps: quiet day ≤ 60 lines total, heavy day ≤ 120.** The caps
  bind — cutting detail to fit is correct, padding to reach them is not. Depth
  lives in the packet JSON and the caches; the brief is the readable layer.
- **Every block leads with its conclusion in plain language.** Numbers support
  the sentence; they don't replace it.
- **Glyphs over prose for state**: 🟢/🟡/🔴 for the call and regime,
  ↑↗→↘↓ for direction, ⭐ for backtest-validated-pocket names, ⚠️ for a
  suspect/unverified number. Same conventions as the sister scans.

## Template

Four layers, in order — dashboard first, plain-language read second, the
gradeable playbook third, capped reference detail last. Layers ① + ② must
stand alone: a reader who stops after them has the day.

```markdown
# 盘前简报 — <YYYY-MM-DD>(美东)· 生成 <HH:MM> ET / 北京 <HH:MM>

## ① 今日一眼

**<🟢 可参与 / 🟡 先看再动 / 🔴 别动> · 信心<高/中/低>** — <一句人话说清今天的性格>

| 信号 | 读数 | 人话 |
|---|---|---|
| 隔夜方向 | QQQ +0.85% ↑ · SPY +0.19% ↗ | 科技股明显高开,大盘微高开 |
| 紧张指数 VIX | 17.3 → · 结构平静 | 市场不慌(20 以下算平静) |
| 情绪温度 | 恐惧 39/100(昨 39 →) | 大家还在怕,没人追高 |
| 大环境 | 🟢 RISK-ON(2 日确认) | 结构健康,允许参与 |
| 今天的事 | 08:30 ECI · 10:00 通胀预期(北京 20:30 / 22:00) | 数据日:10 点前别急着动 |
| 今日考题 | QQQ 收盘站上 684.23? | 站上 = 修复确认,可以加仓 |

## ② 人话解读

<3–8 句连贯中文:昨晚发生了什么 → 今天是什么日子 → 最大的风险在哪。
术语就地翻译;存疑数字带 ⚠️ 和一句为什么。>

**要不要动:**<明确的一句,例如"可以小仓位买 ⭐ 口袋里的 ODFL/VTR;
别追已经 +18% 的芯片股;10 点数据落地前什么都别做"。
没有动作时明说:"今天不用动"。>

## ③ 今日剧本(如果…就…)

- **如果** <可观察的触发,如"QQQ 收在 684.23 上方"> → **就** <动作>。
  **作废条件:**<什么情况这条不算数>。
<≤ 5 条,每条 ≤ 2 行;挂在持仓或观察名单上,不做裸方向判断。>

## ④ 备查细节

### 隔夜盘面
<小表,≤ 7 行:期货 / 亚洲 / 欧洲 / 利率 / 美元与油金 / BTC 各一行,
每行结尾一小句人话。>

### 今日催化剂 ⭐
<econ 数据 + 财报 + 隔夜新闻合计 ≤ 8 条,每条一行,时间 ET(+北京)。
自家持仓或观察名单名字点名。全是噪音就整段省略,不硬凑。>

### 板块与指数位
<领涨 / 领跌各一行;SPY/QQQ/IWM 关键位小表:前收 · 盘前 · 缺口 · 上方压力 /
下方支撑。>

### 焦点名字
<≤ 6 条,每条一行:名字 — 今天为什么值得看。⭐ 标记回测验证口袋里的名字;
分析师评级只留真升降级和堆墙信号(见 doctrine)。>

### 坑(watch-outs)
<≤ 5 条:事件时点陷阱、盘前流动性、存疑数字、缓存过期、数据盲区。>

---

*Sources: <one-line provenance + freshness footer>*
```

The closing `*Sources: …*` footer is **required** and keeps its old convention
(may stay in English): a single italic line after a `---` rule naming which
sources fed this run, which came back empty/unavailable, the regime/momentum/MR
cache dates with staleness, and the `errors` count plus any `data_quality`
flags. It complements the inline ⚠️ caveats, it doesn't replace them.

Reconciliation sections appended by step 1 keep the **literal English header
`## Reconciliation`** (step 1 greps for it) but the body is written in the same
plain Chinese as the briefing. `calibration.md` rows keep their existing terse
convention — that file is the model-facing grading log, not the reader's.

## Writing ③ 今日剧本 — read this carefully

This is the section that can do harm if written lazily, so frame it honestly:

- **Conditional and event-gated, not directional.** On a CPI/FOMC/NFP day the
  tape before the print is a coin flip — the useful instruction is "数据落地前
  减仓/别动;落地后如果 SPY 守住 X 且利率回落,动量名单开绿灯;跌破 X 则
  避险股接管". Write 如果 → 就 → 作废条件, never a bare "买 NVDA". Explain
  the *why* so the reader can adapt when reality diverges from the scenarios.
- **Anchored to the user, not the market in the abstract.** Tie every line to
  a **position** (event risk on a held name, a stop exposed to the gap) or a
  **watchlist name**. When `positions.md` is empty, the playbook is watchlist-
  and regime-level only — say so plainly rather than inventing position advice.
- **P&L-aware when cost basis is available.** With `avg_cost` you can be
  specific: "+35% 进今晚财报 — 部分止盈锁住事件风险,免得利润坐一轮过山车;
  作废条件:…". Without it, stay at event-risk flagging. Never fabricate a basis.
- **Respect the regime.** If the structural read plus the overnight tape say
  risk-off with stacking divergences, the plan is "what's holding up, sized
  small, defense" — not chasing. Thread the regime into sizing.
- The honest test: would the playbook still read as sound *after* the day plays
  out either direction? If it only looks smart in one outcome, it's a
  prediction dressed as a plan — rewrite it as scenarios.

## Standing doctrine (promoted from calibration — keep dates when citing)

- **AMC-earnings settle artifact (07-30).** Futures fold a big after-hours
  print into the 17:00 ET settle, so next-morning ES/NQ % can understate or
  invert the true gap (07-30: NQ +0.99% vs QQQ +1.86% after MSFT/META). On
  those mornings grade gap *size* off index ETFs vs official closes; futures
  keep only the risk-tone vote. Default otherwise: futures are the cleaner
  overnight read (ETF premarket prints are thin).
- **Lone-VIX-spike trap.** A big VIX % move with futures ±0.3% and Europe flat
  is a thin/stale print — flag it ⚠️, don't headline it. Let the VIX/VIX3M
  *ratio* lead over the level: > 1(倒挂)= 急性紧张, < 1(contango)= 平静;
  on a fast overnight move the live ratio beats the end-of-day cache.
- **Raise-wall no-chase (TSM 07-17, AMD 07-27, FTNT 07-30 — all
  round-tripped).** Multiple same-morning analyst price-target raises stacked
  on an already-large premarket gap is a crowding gauge, not confirmation:
  stalk the pullback, don't chase. Weight genuine upgrades/downgrades over a
  routine "maintains + PT nudge".
- **Grade catalyst claims by % of gap kept at the close, per name.** > 70%
  kept = the catalyst is real for that name; < 30% = it traded like sympathy
  (07-20 SIMO inverted inside the catalyst bucket; 07-30 FTNT kept 10% of its
  own print's gap). Close-graded tests need an explicit middle branch — a
  finish within ~0.2% of the line is "未确认,明天再判", not a verdict.
- **Premarket single-stock prints are thin.** Weight the futures gap, Europe,
  and sector ETFs over individual moves; respect the gappers' volume floor —
  pre-8:00 ET thin prints are noise.

## Output honesty rules

- **Run window first (`session`).** If `session.valid` is false you should have
  stopped at SKILL.md step 3. The only time you reach this template
  out-of-window is on an **explicit user-requested** read; then lead with
  `session.warning`, treat **every** premarket block as void (fall back to
  futures + overnight tape), and do not archive it as the day's briefing.
- **Check the premarket `as_of` date.** A run before ~4:00 ET (or Monday
  pre-dawn) sees the *prior* session's after-hours print, not live premarket —
  label those numbers as such or you'll mistake yesterday's move for today's.
- **Don't pad.** A quiet day should produce a *short* brief — dashboard + "今天
  不用动" + a thin appendix is a valid, useful output. Length tracks how much
  is actually happening, never the cap.
- **State what was missing — and what disagreed.** Surface `errors`, stale
  caches, and every `data_quality` flag in the relevant block with an inline
  ⚠️ + one plain-language reason ("这个数字两个数据源对不上,先别信"). A
  briefing that hides its blind spots is worse than one that names them.
- **Times are ET** (Beijing alongside where the reader acts on them), and the
  header carries the "生成于" timestamp so freshness is visible.

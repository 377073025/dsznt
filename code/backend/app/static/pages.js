// Web 控制台其余页面（02 号文档 7.2 节）：闭环总览、选品中心、任务看板、数据中心、规则配置、成本、商品与素材、用户与权限、接入状态。
// 与工作台共用 app.js 里的工具函数（$、esc、api、badge、yuan、timeStr、state、canSubmit）。
"use strict";

const isOwner = () => state.me && state.me.role === "owner";
const pct = (x, d = 0) => (x * 100).toFixed(d) + "%";
const SUG_STATUS = { reported: "待拍板", revising: "修改中", approved: "已同意", rejected: "已拒绝", dispatched: "执行中",
  done: "已完成", reviewed: "已复盘", expired: "已过期", generated: "已生成", discarded: "未入选" };
const CAND_STATUS = { discovered: "已发现", filtered: "已过滤", verified: "已审查", scored: "已打分", recommended: "待拍板",
  revising: "修改中", approved: "已同意", preparing: "准备上架", listed: "已上架", testing: "测款中", kept: "留款",
  optimizing: "优化中", delisted: "已下架", rejected: "已淘汰" };
const TASK_STATE = { pending: "待执行", running: "执行中", produced: "已产出", awaiting_write_confirm: "待写入确认",
  writing: "写入中", submitted: "待验收", accepted: "已验收", failed: "失败", escalated: "已上报" };
const FETCH_STATUS = { success: "成功", scheduled: "待采集", running: "采集中", manual_requested: "等人工导出",
  waiting_human: "验证码 / 登录", circuit_open: "熔断", missing: "缺失", ai_repairing: "AI 认路中", failed: "失败" };
const EXECUTOR = { copy: "文案", image: "图片", listing: "上架物料" };
const SEED = { extension: "延伸", opportunity: "机会", replace: "补位", adhoc: "单独任务" };
const VERDICT = { keep: "留款", optimize: "优化后再观察", drop: "下架" };
const ITEM_LABEL = { goods_snapshot: "商品快照", goods_daily_traffic: "分款流量", order_stats: "订单汇总", promotion_daily: "推广" };

// 拍板类操作：与飞书按钮同一条路径（/api/{entity}/{id}/actions/{event}）
async function act(entity, id, event, label) {
  if (!confirm(`${label}？`)) return;
  try {
    await api(`/api/${entity}/${id}/actions/${event}`, { method: "POST" });
    toast(`已提交「${label}」，稍后刷新可见结果`);
    setTimeout(refresh, 1500);
  } catch (e) { alert(e.message); }
}

async function reviseFlow(entity, id) {
  const fb = prompt("修改意见（例如：售价改成 39.9 / 换个货源 / 理由写具体点）");
  if (!fb || !fb.trim()) return;
  try {
    await api(`/api/${entity}/${id}/revise`, { method: "POST", body: { feedback: fb.trim() } });
    toast("已提交修改意见，改写完成后回到待拍板");
    setTimeout(refresh, 1500);
  } catch (e) { alert(e.message); }
}

function toast(text) {
  let el = $("#toast");
  if (!el) { el = document.createElement("div"); el.id = "toast"; el.className = "toast"; document.body.append(el); }
  el.textContent = text; el.classList.add("show");
  clearTimeout(el._t); el._t = setTimeout(() => el.classList.remove("show"), 3000);
}

// 页面内按钮统一用 data-do 分发，避免每次重绘都重新绑定
const HANDLERS = {};
document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-do]");
  if (b && HANDLERS[b.dataset.do]) { e.preventDefault(); HANDLERS[b.dataset.do](b.dataset, b); }
});
HANDLERS.act = (d) => act(d.entity, d.id, d.event, d.label);
HANDLERS.revise = (d) => reviseFlow(d.entity, d.id);

const ownerBtns = (entity, id, items) => (isOwner() ? items.map(([event, label, cls]) =>
  event === "revise" ? `<button class="ghost" data-do="revise" data-entity="${entity}" data-id="${id}">改一改</button>`
    : `<button class="${cls || ""}" data-do="act" data-entity="${entity}" data-id="${id}" data-event="${event}" data-label="${esc(label)}">${esc(label)}</button>`).join("") : "");

// ---------- 闭环总览 ----------
const overview = {
  async load(el) {
    const o = await api("/api/overview");
    const f = o.funnel, steps = [["sourced", "货源"], ["filtered", "过滤通过"], ["verified", "同款与合规"],
      ["recommended", "推荐"], ["testing", "测款中"], ["kept", "留款"]];
    const max = Math.max(1, f.sourced);
    const detail = (d) => Object.entries(d).map(([k, v]) => `<span class="chip">${esc(SUG_STATUS[k] || FETCH_STATUS[k] || TASK_STATE[k] || k)} ${v}</span>`).join("") || `<span class="muted small">无</span>`;
    const r = o.report;
    el.innerHTML = `
      <section class="panel"><div class="section-head"><h2>运营闭环 · ${esc(o.day)}</h2>
        <span class="muted small">数据日期为已入库的最新一天</span></div>
        <div class="loop">${o.loop.map((s, i) => `<div class="loop-step ${s.ok ? "ok" : "warn"}">
          <div class="loop-name"><span class="loop-no">${i + 1}</span>${esc(s.step)}<span class="loop-flag">${s.ok ? "✓" : "!"}</span></div>
          <div class="chips">${detail(s.detail)}</div></div>`).join("")}</div>
        ${r ? `<div class="kv small report"><span class="k">早报</span><span><b>${esc(r.headline)}</b></span>
          <span class="k">最好卖</span><span>${esc(r.best)}</span><span class="k">正常</span><span>${esc(r.normal)}</span>
          <span class="k">掉在哪</span><span>${esc(r.dropping)}</span><span class="k">数据</span><span>${esc(r.data_status)}</span>
          ${(r.reviews || []).length ? `<span class="k">复盘</span><span>${r.reviews.map(esc).join("<br>")}</span>` : ""}</div>` : `<p class="muted small">这一天还没有早报</p>`}
      </section>
      <section class="cols">
        <div class="panel"><div class="section-head"><h2>选品漏斗</h2><span class="muted small">${esc(o.week || "还没有选品数据")}</span></div>
          ${steps.map(([k, label]) => `<div class="funnel-row"><span class="funnel-label">${label}</span>
            <span class="funnel-bar"><i style="width:${(f[k] / max) * 100}%"></i></span><span class="num">${f[k]}</span></div>`).join("")}
          <h2 class="sub">数据通道</h2>
          <div class="chips">${o.capabilities.map((c) => `<span class="chip s-${esc(c.status)}" title="${esc(c.status)}"><i class="dot"></i>${esc(c.title)}</span>`).join("")}</div>
        </div>
        <div class="panel grow"><div class="section-head"><h2>待拍板建议</h2><span class="muted small">${o.pending_suggestions.length} 条
          ${isOwner() ? "" : " · 只有店主可以拍板"}</span></div>
          ${o.pending_suggestions.map((x) => `<div class="task"><div class="head"><span class="title">#${x.id} ${esc(x.action)}</span>
            ${badge(x.status, SUG_STATUS)}<span class="muted small">置信度 ${esc(x.confidence)}${x.needs_write ? " · 需写店铺" : ""}</span>
            <div class="actions">${x.status === "reported" ? ownerBtns("suggestion", x.id, [["approve", "同意", ""], ["revise"], ["reject", "不同意", "danger"]]) : ""}</div></div>
            <div class="meta">${esc(x.reason)}</div>
            ${x.evidence.length ? `<ul class="small">${x.evidence.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>` : ""}</div>`).join("") || `<p class="muted">没有待拍板的建议</p>`}
        </div>
      </section>`;
  },
};

// ---------- 选品中心 ----------
const selection = {
  week: "", filter: "passed", open: new Set(),
  async load(el) {
    const [d, trials] = await Promise.all([api("/api/candidates" + (this.week ? `?week=${encodeURIComponent(this.week)}` : "")), api("/api/test-runs")]);
    this.week = d.week;
    const passed = (c) => c.status !== "rejected" && !["discovered", "filtered", "verified"].includes(c.status);
    const list = d.candidates.filter((c) => this.filter === "all" || (this.filter === "passed" ? passed(c) : c.status === "rejected"));
    const f = d.funnel;
    el.innerHTML = `
      <section class="panel"><div class="section-head"><h2>本周候选</h2>
        <select data-sel="week">${d.weeks.map((w) => `<option ${w === d.week ? "selected" : ""}>${esc(w)}</option>`).join("") || "<option>无</option>"}</select>
        <div class="seg">${[["passed", "通过"], ["rejected", "被淘汰"], ["all", "全部"]].map(([k, l]) => `<button data-do="selFilter" data-v="${k}" class="${this.filter === k ? "on" : ""}">${l}</button>`).join("")}</div></div>
        <p class="muted small">货源 ${f.sourced} → 过滤通过 ${f.filtered} → 同款与合规 ${f.verified} → 推荐 ${f.recommended} → 测款 ${f.testing} → 留款 ${f.kept}。点一行看打分明细。</p>
        <div class="table-wrap tall"><table><tr><th>货源</th><th>需求</th><th class="num">售价</th><th class="num">单件利润</th><th class="num">毛利率</th><th class="num">得分</th><th>状态</th></tr>
        ${list.map((c) => this.row(c)).join("") || `<tr><td colspan="7" class="muted">没有符合条件的候选</td></tr>`}</table></div>
      </section>
      <section class="panel"><div class="section-head"><h2>测款看板</h2><span class="muted small">首轮 14 天；判定「优化」后运营智能体诊断，再观察 7 天</span></div>
        <div class="table-wrap"><table><tr><th>商品</th><th>开始</th><th>轮次</th><th>进度</th><th class="num">访客</th><th class="num">转化率</th><th>系统判断</th><th>状态</th><th></th></tr>
        ${trials.map((t) => `<tr><td>${esc(t.title)}<div class="muted small">${esc(t.goods_id)}</div></td><td>${esc(t.start_date)}</td>
          <td>${t.trial_round > 1 ? "优化后" : "首轮"}</td><td><span class="meter"><i style="width:${(t.observed / t.days) * 100}%"></i></span> ${t.observed}/${t.days} 天</td>
          <td class="num">${t.verdict ? t.visitors : "—"}</td><td class="num">${t.verdict ? pct(t.cvr, 1) : "—"}</td>
          <td>${esc(VERDICT[t.verdict] || "观察中")}</td><td>${badge(t.candidate_status, CAND_STATUS)}</td>
          <td class="actions">${t.verdict && t.candidate_status === "testing" ? ownerBtns("candidate", t.candidate_id,
            [["keep", "留款", ""], ...(t.trial_round > 1 ? [] : [["optimize", "优化后再观察", "ghost"]]), ["drop", "下架", "danger"]]) : ""}</td></tr>`).join("")
          || `<tr><td colspan="9" class="muted">还没有测款中的商品</td></tr>`}</table></div>
      </section>`;
  },
  row(c) {
    const pp = c.price_plan || {}, open = this.open.has(c.id);
    const main = `<tr class="click ${open ? "sel" : ""}" data-do="candOpen" data-id="${c.id}">
      <td>${esc(c.offer.title)}<div class="muted small">${esc(c.offer.id)}</div></td>
      <td>${esc(c.demand.keyword)} <span class="chip">${esc(SEED[c.demand.seed_type] || c.demand.seed_type)}</span></td>
      <td class="num">${pp.sell_price_fen ? yuan(pp.sell_price_fen) : "—"}</td><td class="num">${pp.profit_fen != null ? yuan(pp.profit_fen) : "—"}</td>
      <td class="num">${pp.margin != null ? pct(pp.margin) : "—"}</td><td class="num">${c.score || "—"}</td><td>${badge(c.status, CAND_STATUS)}</td></tr>`;
    if (!open) return main;
    const weights = Object.entries(c.breakdown || {});
    const comp = c.compliance || {};
    return main + `<tr class="detail-row"><td colspan="7"><div class="cand-detail">
      <div>${weights.length ? `<h3>打分明细（满分 100）</h3>${weights.map(([k, v]) => `<div class="funnel-row"><span class="funnel-label">${esc(SCORE_LABEL[k] || k)}</span>
        <span class="funnel-bar"><i style="width:${Math.min(100, v * 4)}%"></i></span><span class="num">${v}</span></div>`).join("")}` : ""}
        <div class="kv small"><span class="k">供应商</span><span>经营 ${c.offer.supplier_years} 年 · 48 小时发货 ${pct(c.offer.ship_48h_rate)}</span>
        <span class="k">同款置信度</span><span>${c.same_conf ? pct(c.same_conf) : "—"}</span>
        <span class="k">合规</span><span>${comp.passed === undefined ? "—" : comp.passed ? "通过" : "未通过"}${(comp.issues || []).length ? "：" + esc(comp.issues.join("；")) : ""}</span>
        ${c.backups.length ? `<span class="k">备选货源</span><span>${c.backups.map((b) => esc(b.title)).join("<br>")}</span>` : ""}
        ${c.revise_rounds ? `<span class="k">已修改</span><span>${c.revise_rounds} 轮</span>` : ""}</div></div>
      <div>${c.reason ? `<h3>推荐理由</h3><p>${esc(c.reason)}</p>` : ""}
        ${c.risks.length ? `<h3>风险</h3><ul>${c.risks.map((r) => `<li>${esc(r)}</li>`).join("")}</ul>` : ""}
        ${c.reject_reason ? `<h3>淘汰原因</h3><p class="err">${esc(c.reject_reason)}</p>` : ""}
        <div class="actions">${c.status === "recommended" ? ownerBtns("candidate", c.id, [["approve", "同意上新", ""], ["revise"], ["reject", "不要", "danger"]]) : ""}</div></div>
    </div></td></tr>`;
  },
};
const SCORE_LABEL = { profit: "利润空间", demand: "市场需求", competition: "竞争程度", supplier: "供应商质量",
  dropship_auto: "代发自动化", fit: "店铺契合度", assets: "素材可用性" };
HANDLERS.selFilter = (d) => { selection.filter = d.v; refresh(); };
HANDLERS.candOpen = (d) => { const id = +d.id; selection.open.has(id) ? selection.open.delete(id) : selection.open.add(id); refresh(); };
document.addEventListener("change", (e) => {
  if (e.target.matches("[data-sel=week]")) { selection.week = e.target.value; refresh(); }
});

// ---------- 任务看板 ----------
const board = {
  async load(el) {
    const d = await api("/api/board");
    el.innerHTML = `<section><div class="section-head"><h2>任务看板</h2><span class="muted small">最近 14 天有变化的任务</span></div>
      <div class="board">${d.columns.map((c) => `<div class="board-col"><div class="board-head">${esc(c.title)} <span class="muted">${c.tasks.length}</span></div>
        ${c.tasks.map((t) => this.card(t)).join("") || `<p class="muted small">无</p>`}</div>`).join("")}</div></section>`;
  },
  card(t) {
    const out = t.output || {};
    const lines = [];
    if (out.patch) lines.push("改动：" + Object.entries(out.patch).map(([k, v]) => `${k} → ${v}`).join("；"));
    if (out.package) lines.push(`物料：${out.package.title} · ${out.package.price_fen ? yuan(out.package.price_fen) : ""}`);
    if (out.write_ref) lines.push("写入回执：" + out.write_ref);
    return `<div class="board-card"><div class="head"><b>#${t.id} ${esc(EXECUTOR[t.executor] || t.executor)}</b>${badge(t.status, TASK_STATE)}</div>
      <div class="small">${esc(t.ref)}</div>
      ${t.instruction ? `<div class="muted small">${esc(t.instruction)}</div>` : ""}
      ${lines.map((l) => `<div class="small">${esc(l)}</div>`).join("")}
      ${t.rework_count ? `<div class="small err">返工 ${t.rework_count} 次</div>` : ""}
      <div class="muted small">${t.needs_write ? "需写店铺 · " : ""}${esc(timeStr(t.updated_at))}</div>
      ${t.status === "awaiting_write_confirm" ? `<div class="actions">${ownerBtns("task", t.id, [["confirm", "确认写入", ""], ["cancel", "取消写入", "danger"]])}</div>` : ""}</div>`;
  },
};

// ---------- 数据中心 ----------
const dataCenter = {
  async load(el) {
    const [f, ev] = await Promise.all([api("/api/data/fetch-jobs?days=14"), api("/api/data/events")]);
    const cell = {};
    for (const j of f.jobs) cell[`${j.item}|${j.day}`] = j;
    const days = [...f.days].reverse();
    const todo = f.jobs.filter((j) => ["manual_requested", "waiting_human", "circuit_open"].includes(j.status));
    const canOp = state.me && ["owner", "operator"].includes(state.me.role);
    el.innerHTML = `
      <section class="panel"><div class="section-head"><h2>数据完整性</h2><span class="muted small">最近 14 天，悬停看详情</span></div>
        <div class="table-wrap"><table class="calendar"><tr><th>数据项</th>${days.map((d) => `<th>${d.slice(5)}</th>`).join("")}</tr>
        ${f.items.map((it) => `<tr><td>${esc(ITEM_LABEL[it.key] || it.key)}</td>${days.map((d) => {
          const j = cell[`${it.key}|${d}`];
          if (!j) return `<td><span class="cal s-none" title="${d} 没有采集任务"></span></td>`;
          return `<td><span class="cal s-${esc(j.status)}" title="${esc(`${d} ${FETCH_STATUS[j.status] || j.status}${j.channel ? " · " + j.channel : ""}${j.rows ? " · " + j.rows + " 行" : ""}${j.error ? "\n" + j.error : ""}`)}"></span></td>`;
        }).join("")}</tr>`).join("")}</table></div>
        <div class="legend small">${["success", "manual_requested", "waiting_human", "circuit_open", "missing"].map((s) =>
          `<span><span class="cal s-${s}"></span>${FETCH_STATUS[s]}</span>`).join("")}<span><span class="cal s-none"></span>没有任务</span></div>
      </section>
      <section class="cols">
        <div class="panel"><h2>需要人工处理</h2>
          ${todo.map((j) => `<div class="task"><div class="head"><span class="title">${esc(j.day)} ${esc(ITEM_LABEL[j.item] || j.item)}</span>${badge(j.status, FETCH_STATUS)}
            <div class="actions">${canOp && j.status !== "manual_requested" ? `<button data-do="resume" data-id="${j.id}">已处理，恢复采集</button>` : ""}</div></div>
            <div class="meta">${esc(j.error)}</div></div>`).join("") || `<p class="muted">没有待处理的采集</p>`}
          <h2 class="sub">手动上传</h2>
          <form id="uploadForm" class="upload"><input type="file" name="file" accept=".csv,.xlsx,.xls" ${canOp ? "" : "disabled"}>
            <button type="submit" ${canOp ? "" : "disabled"}>上传导入</button></form>
          <p class="muted small">${canOp ? "支持拼多多后台导出的表格；导入后相关分析会自动重新开始。" : "店主或运营才能上传。"}</p>
          <div id="uploadMsg" class="small"></div>
        </div>
        <div class="panel grow"><h2>改动事件</h2>
          <div class="table-wrap"><table><tr><th>日期</th><th>商品</th><th>类型</th><th>改前 → 改后</th><th>来源</th></tr>
          ${ev.changes.map((c) => `<tr><td>${esc(c.day)}</td><td>${esc(c.name)}</td><td>${esc(c.type)}</td><td class="small">${esc(c.before)} → ${esc(c.after)}</td><td>${esc(c.source)}</td></tr>`).join("")
            || `<tr><td colspan="5" class="muted">没有改动</td></tr>`}</table></div>
        </div>
      </section>
      <section class="cols">
        <div class="panel grow"><h2>风控事件</h2><div class="table-wrap"><table><tr><th>时间</th><th>平台</th><th>类型</th><th>页面</th></tr>
          ${ev.risks.map((r) => `<tr><td>${esc(timeStr(r.at))}</td><td>${esc(r.platform)}</td><td>${esc(r.type)}</td><td class="small">${esc(r.page)}</td></tr>`).join("")
            || `<tr><td colspan="4" class="muted">最近 30 天没有风控事件</td></tr>`}</table></div></div>
        <div class="panel grow"><h2>导入记录</h2><div class="table-wrap"><table><tr><th>时间</th><th>文件</th><th>数据项</th><th class="num">行</th><th>状态</th></tr>
          ${ev.imports.map((m) => `<tr><td>${esc(timeStr(m.at))}</td><td>${esc(m.file)}</td><td>${esc(ITEM_LABEL[m.item] || m.item)}</td><td class="num">${m.rows}</td>
            <td>${esc(m.status)}${m.errors.length ? `<div class="err small">${m.errors.map((x) => esc(`第 ${x.row} 行：${x.error}`)).join("<br>")}</div>` : ""}</td></tr>`).join("")
            || `<tr><td colspan="5" class="muted">没有导入记录</td></tr>`}</table></div></div>
      </section>`;
  },
};
HANDLERS.resume = async (d) => {
  if (!confirm("确认已经处理好（验证码 / 登录 / 熔断原因），恢复采集？")) return;
  try { await api(`/api/fetch_job/${d.id}/resume`, { method: "POST" }); toast("已提交，worker 会重新采集"); setTimeout(refresh, 1500); }
  catch (e) { alert(e.message); }
};
document.addEventListener("submit", async (e) => {
  if (e.target.id !== "uploadForm") return;
  e.preventDefault();
  const fd = new FormData(e.target), msg = $("#uploadMsg");
  if (!fd.get("file") || !fd.get("file").name) { msg.textContent = "请选择文件"; return; }
  msg.className = "small muted"; msg.textContent = "上传中…";
  try {
    const res = await fetch("/api/imports", { method: "POST", headers: { "X-Open-Id": openId() }, body: fd });
    const r = await res.json();
    if (!res.ok) throw new Error(r.detail || "上传失败");
    msg.className = r.status === "imported" ? "small" : "small err";
    msg.textContent = r.status === "imported" ? `已导入 ${r.rows} 行（${r.parser}）` :
      r.status === "unrecognized" ? "这份文件不认识，请确认是拼多多后台「商品数据」导出" : "有数据不对，没有导入：" + r.errors.map((x) => `第 ${x.row} 行 ${x.error}`).join("；");
  } catch (err) { msg.className = "small err"; msg.textContent = err.message; }
});

// ---------- 规则配置 ----------
const RULE_LABEL = {
  selection: "选品", ops: "运营诊断", hard_filters: "硬性过滤", score_weights: "打分权重（合计 100）", profit: "利润参数",
  test_run: "测款", compliance: "合规审查", require_dropship: "必须支持一件代发", min_margin: "最低毛利率（0.25 = 25%）",
  min_profit_per_unit: "单件最低利润（元）", min_ship_48h_rate: "48 小时发货率下限", max_return_rate_vs_category: "退货率 ≤ 类目均值 ×",
  banned_categories: "禁售 / 需资质类目", max_weight_kg: "最大重量（kg）", max_longest_side_cm: "最长边（cm）",
  min_supplier_years: "供应商最少经营年限", min_stock: "最低库存", profit_w: "利润空间", demand: "市场需求", competition: "竞争程度",
  supplier: "供应商质量", dropship_auto: "代发自动化", fit: "店铺契合度", assets: "素材可用性", candidate_min_score: "进入候选的最低分",
  weekly_max_recommend: "每周最多推荐", platform_fee_rate: "平台扣点", promotion_rate: "推广费率", after_sales_loss_rate: "售后损耗率",
  tool_fee_per_order: "每单工具费（元）", days: "测款天数", keep_min_visitors: "留款最少访客", keep_min_cvr_ratio: "留款转化率 ≥ 店均 ×",
  drop_max_visitors: "下架：访客少于", drop_max_cvr_ratio: "下架：转化率 < 店均 ×", optimize_days: "优化后再观察天数",
  brand_words: "品牌 / IP 词库", llm_review: "词库没命中时交模型审查", min_weekly_visitors_for_cvr: "周访客少于此数不判断转化率",
  cvr_drop_alert_ratio: "转化率异常：近 7 天 < 前 7 天 ×", max_suggestions_per_report: "早报最多建议条数", review_window_days: "复盘观察天数",
  monthly_tuning: "月度调权", min_samples: "有结论的测款至少几款才调", max_step: "每个维度每月最多加减",
  offer_watch: "在售款货源复查", price_rise_ratio: "拿货价涨幅告警线（0.05 = 5%）", max_backups: "告警里列几个备选货源",
};
const WEIGHT_LABEL = { profit: "利润", demand: "需求", competition: "竞争", supplier: "供应商", dropship_auto: "自动下单", fit: "契合度", assets: "素材" };
const PROPOSAL_STATUS = { pending: "待店主确认", applied: "已采纳", rejected: "未采纳", expired: "已作废" };

function proposals(list) {
  if (!list.length) return "";
  return `<section class="panel"><div class="section-head"><h2>调权提议</h2><span class="muted small">每月 1 日按测款结果比较各维度，店主同意后保存为新版本</span></div>
    ${list.map((p) => {
      const sm = p.summary;
      return `<div class="task"><div class="head"><b>${esc(p.period)}</b>${badge(p.status, PROPOSAL_STATUS)}
      <span class="muted small">测款 ${sm.samples} 款：留款 ${sm.kept}、下架 ${sm.delisted}${p.rule_version ? ` · 已存为版本 ${p.rule_version}` : ""}</span>
      <div class="actions">${p.status === "pending" ? ownerBtns("rule_proposal", p.id, [["apply", "同意调整", ""], ["reject", "不调整", "danger"]]) : ""}</div></div>
      <div class="table-wrap"><table><tr><th>维度</th><th class="num">得分率 · 留款</th><th class="num">下架</th><th class="num">权重</th></tr>
      ${Object.entries(sm.stats).map(([k, s]) => `<tr><td>${esc(WEIGHT_LABEL[k] || k)}</td><td class="num">${pct(s.win)}</td><td class="num">${pct(s.lose)}</td>
        <td class="num">${sm.old[k]}${sm.new[k] !== sm.old[k] ? ` → <b>${sm.new[k]}</b>` : ""}</td></tr>`).join("")}</table></div>
      ${sm.error ? `<p class="small err">没能保存：${esc(sm.error)}</p>` : ""}</div>`;
    }).join("")}</section>`;
}
const rules = {
  auto: false, data: null,
  async load(el) {
    this.data = await api("/api/rules");
    const owner = isOwner();
    el.innerHTML = `<section class="panel"><div class="section-head"><h2>规则配置</h2>
        <span class="muted small">当前版本 ${this.data.version || "基线（rules.yaml）"}${owner ? "" : " · 只有店主可以修改"}</span>
        <button data-do="rulesReload" class="ghost">放弃修改</button><button data-do="rulesSave" ${owner ? "" : "disabled"}>保存为新版本</button></div>
        <p class="muted small">保存后 worker 从下一个作业起使用新规则；每次保存都留版本，下面可以看到改了什么。</p>
        <div id="rulesMsg" class="small"></div>
        <form id="rulesForm" class="rules">${this.fields(this.data.rules, "", owner)}</form></section>
      ${proposals(this.data.proposals)}
      <section class="panel"><h2>修改记录</h2>${this.data.history.map((h) => `<div class="task"><div class="head"><b>版本 ${h.version}</b>
        <span class="muted small">${esc(h.by)} · ${esc(timeStr(h.at))}</span></div>
        <ul class="small">${h.changes.map((c) => `<li>${esc(c)}</li>`).join("") || "<li class='muted'>没有改动</li>"}</ul></div>`).join("") || `<p class="muted">还没有修改过</p>`}</section>`;
    this.sum();
  },
  fields(obj, path, owner) {
    const scalars = [], groups = [];
    for (const [k, v] of Object.entries(obj)) {
      const p = path ? `${path}.${k}` : k;
      const label = RULE_LABEL[path.endsWith("score_weights") && k === "profit" ? "profit_w" : k] || k;
      if (v && typeof v === "object" && !Array.isArray(v)) {
        groups.push(`<fieldset${k === "score_weights" ? ` id="weights"` : ""}><legend>${esc(label)}${k === "score_weights" ? ` <span id="wsum" class="small"></span>` : ""}</legend>${this.fields(v, p, owner)}</fieldset>`);
        continue;
      }
      const dis = owner ? "" : "disabled";
      let input;
      if (typeof v === "boolean") input = `<input type="checkbox" data-path="${p}" data-kind="bool" ${v ? "checked" : ""} ${dis}>`;
      else if (Array.isArray(v)) input = `<textarea data-path="${p}" data-kind="list" rows="2" ${dis}>${esc(v.join("、"))}</textarea>`;
      else if (typeof v === "number") input = `<input type="number" step="any" min="0" data-path="${p}" data-kind="num" value="${v}" ${dis}>`;
      else input = `<input data-path="${p}" data-kind="str" value="${esc(v)}" ${dis}>`;
      scalars.push(`<label class="${typeof v === "boolean" ? "check" : ""}">${esc(label)}${input}</label>`);
    }
    return (scalars.length ? `<div class="rule-grid">${scalars.join("")}</div>` : "") + groups.join("");
  },
  patch() {
    const out = {};
    let changed = 0;
    for (const el of document.querySelectorAll("#rulesForm [data-path]")) {
      const keys = el.dataset.path.split(".");
      const old = keys.reduce((o, k) => o[k], this.data.rules);
      const v = el.dataset.kind === "bool" ? el.checked : el.dataset.kind === "num" ? parseFloat(el.value)
        : el.dataset.kind === "list" ? el.value.split(/[、,，\n]/).map((x) => x.trim()).filter(Boolean) : el.value;
      if (JSON.stringify(v) === JSON.stringify(old)) continue;
      if (el.dataset.kind === "num" && Number.isNaN(v)) throw new Error(`${el.dataset.path} 不是数字`);
      let o = out;
      keys.slice(0, -1).forEach((k) => { o = o[k] = o[k] || {}; });
      o[keys[keys.length - 1]] = v; changed++;
    }
    return changed ? out : null;
  },
  sum() {
    const els = [...document.querySelectorAll("#weights input[data-kind=num]")];
    const s = els.reduce((a, el) => a + (parseFloat(el.value) || 0), 0);
    const w = $("#wsum"); if (w) { w.textContent = `当前合计 ${s}`; w.className = s === 100 ? "small muted" : "small err"; }
  },
};
HANDLERS.rulesReload = () => { rules.loaded = false; refresh(); };
HANDLERS.rulesSave = async () => {
  const msg = $("#rulesMsg");
  try {
    const p = rules.patch();
    if (!p) { msg.className = "small muted"; msg.textContent = "没有改动"; return; }
    const r = await api("/api/rules", { method: "PUT", body: p });
    toast(`已保存为版本 ${r.version}`);
    rules.loaded = false; refresh();
  } catch (e) { msg.className = "small err"; msg.textContent = e.message; }
};
document.addEventListener("input", (e) => { if (e.target.closest("#weights")) rules.sum(); });

// ---------- 成本 ----------
const costs = {
  async load(el) {
    const c = await api("/api/costs?days=30");
    const b = c.budget;
    const meter = (label, used, limit) => {
      const r = limit ? used / limit : 0, cls = r >= 1 ? "s-failed" : r >= b.warn_ratio ? "s-deferred" : "s-succeeded";
      return `<div class="budget"><div class="small">${label}</div><div class="hero">¥${used.toFixed(2)} <span class="muted small">/ ¥${limit}</span></div>
        <span class="meter ${cls}"><i style="width:${Math.min(100, r * 100)}%"></i></span>
        <div class="small ${r >= b.warn_ratio ? "err" : "muted"}">${pct(r)}${r >= 1 ? " · 已用完" : r >= b.warn_ratio ? " · 已超提醒线" : ""}</div></div>`;
    };
    const max = Math.max(0.01, ...c.days.map((d) => d.main + d.adhoc));
    const table = (rows) => `<div class="table-wrap"><table><tr><th></th><th class="num">调用</th><th class="num">token</th><th class="num">费用</th></tr>
      ${rows.map((r) => `<tr><td>${esc(r.key)}</td><td class="num">${r.calls}</td><td class="num">${Math.round(r.tokens).toLocaleString()}</td><td class="num">¥${r.cost.toFixed(2)}</td></tr>`).join("")
        || `<tr><td colspan="4" class="muted">没有调用</td></tr>`}</table></div>`;
    el.innerHTML = `
      <section class="panel"><div class="section-head"><h2>预算</h2>${b.over ? `<span class="badge s-failed">${esc(b.over)}，非关键环节已推迟</span>` : ""}</div>
        <div class="budgets">${meter("今日 · 主流程", b.today, b.daily)}${meter("本月 · 主流程", b.month, b.monthly)}${meter("今日 · 单独任务", b.adhoc_today, b.adhoc_daily)}</div>
        <p class="muted small">达到 ${pct(b.warn_ratio)} 时飞书提醒一次；用完后 ${esc(b.critical.join("、"))} 照常执行，其余推迟到次日。Batch 已省 ¥${c.batch_saved.toFixed(2)}。预算在 config/settings.yaml 的 budget 里调整。</p>
      </section>
      <section class="panel"><div class="section-head"><h2>近 30 天每日花费</h2>
        <div class="legend small"><span><i class="sw sw-main"></i>主流程</span><span><i class="sw sw-adhoc"></i>单独任务</span></div></div>
        <div class="chart" role="img" aria-label="近 30 天每日 LLM 花费，按主流程与单独任务堆叠">
          <div class="chart-y small muted"><span>¥${max.toFixed(2)}</span><span>¥0</span></div>
          <div class="bars">${c.days.map((d) => `<div class="bar" tabindex="0" data-tip="${esc(`${d.day}\n主流程 ¥${d.main.toFixed(2)}\n单独任务 ¥${d.adhoc.toFixed(2)}`)}">
            ${d.adhoc ? `<i class="seg-adhoc" style="height:${(d.adhoc / max) * 100}%"></i>` : ""}${d.main ? `<i class="seg-main" style="height:${(d.main / max) * 100}%"></i>` : ""}</div>`).join("")}</div>
          <div class="chart-x small muted"><span>${esc(c.days[0].day.slice(5))}</span><span>${esc(c.days[c.days.length - 1].day.slice(5))}</span></div>
          <div class="tip" hidden></div>
        </div>
      </section>
      <section class="cols"><div class="panel grow"><h2>按智能体</h2>${table(c.by_agent)}</div><div class="panel grow"><h2>按模型</h2>${table(c.by_model)}</div></section>`;
  },
};
// 柱子悬停：显示当天两项花费
document.addEventListener("pointerover", (e) => showTip(e.target.closest(".bar")));
document.addEventListener("focusin", (e) => showTip(e.target.closest(".bar")));
document.addEventListener("pointerleave", (e) => { if (e.target.classList && e.target.classList.contains("bars")) hideTip(); }, true);
function showTip(bar) {
  const tip = bar && bar.closest(".chart") && bar.closest(".chart").querySelector(".tip");
  if (!tip) return;
  tip.textContent = bar.dataset.tip; tip.hidden = false;
  const box = bar.closest(".chart").getBoundingClientRect(), r = bar.getBoundingClientRect();
  tip.style.left = Math.min(box.width - 150, Math.max(0, r.left - box.left - 60)) + "px";
}
function hideTip() { document.querySelectorAll(".chart .tip").forEach((t) => { t.hidden = true; }); }

// ---------- 商品与素材 ----------
const ORIGIN = { supplier: "货源图", ai: "AI 生成", edited: "人工修改" };
const ASSET_KIND = { main: "主图", detail: "详情图", scene: "场景图", sku: "SKU 图" };
const offerUrl = (id) => `https://detail.1688.com/offer/${encodeURIComponent(id.replace(/^1688_/, ""))}.html`;
const catalog = {
  auto: false,
  async load(el) {
    const rows = await api("/api/catalog");
    const edit = canSubmit();
    const src = (p) => (p.source ? `<a href="${offerUrl(p.source.offer_id)}" target="_blank" rel="noopener">${esc(p.source.title || p.source.offer_id)}</a>`
      : `<span class="muted">未登记</span>`);
    const trial = (t) => (t ? `候选 #${t.candidate_id}${t.round > 1 ? " · 优化后" : ""}${t.verdict ? " · " + esc(VERDICT[t.verdict] || t.verdict) : ""}` : "");
    el.innerHTML = `<section class="panel"><div class="section-head"><h2>在售商品与货源</h2>
        <span class="muted small">登记了货源的商品每天 10:00 复查：下架、涨价、库存不足、按现售价不赚钱时飞书告警并列出备选货源</span></div>
      <div class="table-wrap"><table><tr><th>商品</th><th class="num">售价</th><th>货源</th><th class="num">拿货价</th><th class="num">库存</th><th>测款</th>${edit ? "<th></th>" : ""}</tr>
      ${rows.map((p) => `<tr><td><b>${esc(p.name)}</b><div class="muted small">${esc(p.goods_id)}</div></td><td class="num">${yuan(p.price_fen)}</td>
        <td>${src(p)}</td><td class="num">${p.source && p.source.price_fen != null ? yuan(p.source.price_fen) : "—"}</td>
        <td class="num">${p.source && p.source.stock != null ? p.source.stock : "—"}</td><td class="small">${trial(p.trial)}</td>
        ${edit ? `<td><button class="ghost" data-do="setSource" data-goods="${esc(p.goods_id)}" data-name="${esc(p.name)}" data-cur="${esc(p.source ? p.source.offer_id : "")}">${p.source ? "改货源" : "登记货源"}</button></td>` : ""}</tr>`).join("")
        || `<tr><td colspan="7" class="muted">还没有商品数据</td></tr>`}</table></div></section>
      <section class="panel"><div class="section-head"><h2>素材库</h2><span class="muted small">每次出图都留版本，旧版本用于「换回原图」</span></div>
      ${rows.filter((p) => p.assets.length).map((p) => `<div class="task"><div class="head"><b>${esc(p.name)}</b><span class="muted small">当前主图 ${esc(p.main_image)}</span></div>
        <div class="table-wrap"><table><tr><th>类型</th><th class="num">版本</th><th>来源</th><th>文件</th><th>说明</th><th>时间</th></tr>
        ${p.assets.map((a) => `<tr><td>${esc(ASSET_KIND[a.kind] || a.kind)}</td><td class="num">v${a.version}</td><td>${esc(ORIGIN[a.origin] || a.origin)}</td>
          <td class="small">${esc(a.path)}</td><td class="small">${esc(a.note)}</td><td class="small muted">${esc(timeStr(a.at))}</td></tr>`).join("")}</table></div></div>`).join("")
        || `<p class="muted">还没有素材记录。图片执行器出的图、上架物料包用到的货源图会自动登记在这里。</p>`}</section>`;
  },
};
HANDLERS.setSource = async (d) => {
  const v = prompt(`「${d.name}」的 1688 货源链接或 offer id（清空表示不再复查）`, d.cur);
  if (v === null) return;
  try {
    await api(`/api/products/${encodeURIComponent(d.goods)}/source`, { method: "PUT", body: { offer: v } });
    toast("已保存，下次复查按新货源");
    catalog.loaded = false; refresh();
  } catch (e) { alert(e.message); }
};

// ---------- 用户与权限 ----------
const users = {
  auto: false,
  async load(el) {
    const d = await api("/api/users");
    const owner = isOwner();
    const roleSel = (u) => `<select data-user-role="${u.id}">${Object.entries(d.roles).map(([k, v]) =>
      `<option value="${k}" ${k === u.role ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
    el.innerHTML = `<section class="panel"><div class="section-head"><h2>用户与权限</h2>
        <span class="muted small">第一次在群里 @ 机器人或私聊的人自动登记为「查看」${owner ? "" : " · 只有店主可以调整"}</span></div>
      <p class="muted small">店主：拍板、写入确认、改规则、恢复熔断；运营：提交单独任务、导入数据、改货源；查看：只能看。店主也可以在飞书里发「设为运营 @某人」。</p>
      <div class="table-wrap"><table><tr><th>名字</th><th>飞书 open_id</th><th>角色</th><th>登记时间</th></tr>
      ${d.users.map((u) => `<tr><td>${owner ? `<input data-user-name="${u.id}" value="${esc(u.name)}" placeholder="填个名字">` : esc(u.name || "—")}</td>
        <td class="small">${esc(u.open_id)}</td><td>${owner ? roleSel(u) : esc(d.roles[u.role] || u.role)}</td>
        <td class="small muted">${esc(timeStr(u.created_at))}</td></tr>`).join("")}</table></div></section>`;
  },
};
async function patchUser(id, body) {
  try { await api(`/api/users/${id}`, { method: "PATCH", body }); toast("已保存"); } catch (e) { alert(e.message); }
  users.loaded = false; refresh();
}

// ---------- 接入状态 ----------
const APP_STATUS = { todo: "未开始", doing: "办理中", waiting: "等待审核", done: "已完成", rejected: "被拒", skip: "不需要" };
const integrations = {
  auto: false,
  async load(el) {
    const [cs, apps] = await Promise.all([api("/api/capabilities"), api("/api/applications")]);
    const owner = isOwner(), edit = canSubmit();
    const appName = Object.fromEntries(apps.map((a) => [a.id, a.name]));
    const statusSel = (a) => `<select data-app-status="${esc(a.id)}">${Object.entries(APP_STATUS).map(([k, v]) =>
      `<option value="${k}" ${k === a.status ? "selected" : ""}>${v}</option>`).join("")}</select>`;
    el.innerHTML = `<section class="panel"><div class="section-head"><h2>数据与服务通道</h2>
        <span class="muted small">每 30 分钟健康检查；不可用时自动降级（API → 网页 → 人工）</span></div>
      <div class="table-wrap"><table><tr><th>能力</th><th>状态</th><th>对应申请</th><th>最近错误</th>${owner ? "<th></th>" : ""}</tr>
      ${cs.map((c) => `<tr><td><b>${esc(c.title)}</b><div class="muted small">${esc(c.key)}</div></td>
        <td><span class="chip s-${esc(c.status)}"><i class="dot"></i>${esc(CAP_STATUS[c.status] || c.status)}</span></td>
        <td class="small">${c.applications.map((a) => `${esc(a)} ${esc(appName[a] || "")}`).join("<br>") || "—"}</td>
        <td class="small">${esc(c.last_error || "")}</td>
        ${owner ? `<td><button class="ghost" data-do="credential" data-key="${esc(c.key)}" data-title="${esc(c.title)}">录入凭证</button></td>` : ""}</tr>`).join("")}</table></div>
      <p class="muted small">凭证只存进本机凭据管理器，不落库、不写日志。录入后状态为「待检查」，下次健康检查通过即为「正常」；真实模式还要把 config/settings.yaml 的 adapter_mode 改为 real 并重启 worker。</p></section>
      <section class="panel"><div class="section-head"><h2>人工申请进度</h2><span class="muted small">08 号文档清单</span></div>
      <div class="table-wrap"><table><tr><th>编号</th><th>事项</th><th>级别</th><th>状态</th><th>备注</th></tr>
      ${apps.map((a) => `<tr><td>${esc(a.id)}</td><td>${esc(a.name)}</td><td class="small">${esc(a.level)}</td>
        <td>${edit ? statusSel(a) : esc(APP_STATUS[a.status] || a.status)}</td>
        <td>${edit ? `<input data-app-note="${esc(a.id)}" value="${esc(a.note)}" placeholder="进度备注">` : esc(a.note)}</td></tr>`).join("")}</table></div></section>`;
  },
};
HANDLERS.credential = async (d) => {
  const ref = prompt(`「${d.title}」的凭证名称（例如 anthropic_api_key、feishu_app_secret）`);
  if (!ref || !ref.trim()) return;
  const value = prompt(`${ref.trim()} 的值（只存本机凭据管理器）`);
  if (!value) return;
  try {
    await api(`/api/capabilities/${encodeURIComponent(d.key)}/credential`, { method: "POST", body: { ref: ref.trim(), value } });
    toast("已录入，等待下次健康检查");
    integrations.loaded = false; refresh();
  } catch (e) { alert(e.message); }
};

// 表格里的下拉框 / 输入框：改完即保存
document.addEventListener("change", async (e) => {
  const t = e.target;
  if (t.dataset.userRole) return patchUser(t.dataset.userRole, { role: t.value });
  if (t.dataset.userName !== undefined) return patchUser(t.dataset.userName, { name: t.value });
  const id = t.dataset.appStatus || t.dataset.appNote;
  if (!id) return;
  try {
    await api(`/api/applications/${encodeURIComponent(id)}`, { method: "PATCH", body: t.dataset.appStatus ? { status: t.value } : { note: t.value } });
    toast("已保存");
  } catch (err) { alert(err.message); }
});

window.PAGES = { overview, selection, board, data: dataCenter, rules, costs, catalog, users, integrations };

// 智能体工作台（02 号文档 7.2 节、07 号文档 17.6 节）。只用轮询：每 5 秒刷新一次。
"use strict";

const POLL_MS = 5000;
const AGENT_STATE = { running: "运行中", waiting_human: "等待人工", paused: "已暂停", queued: "排队中", idle: "空闲" };
const RUN_STATUS = { running: "运行中", succeeded: "成功", failed: "失败", cancelled: "已取消" };
const TASK_STATUS = { queued: "排队中", deferred: "已顺延", running: "运行中", done: "已完成", failed: "失败",
  cancelled: "已取消", promoted: "已转化" };
const CAP_STATUS = { healthy: "正常", degraded: "降级", circuit_open: "熔断", unavailable: "不可用", disabled: "未启用",
  pending: "待接入" };
const FIELD_LABELS = { goods_id: "商品", category: "类目", keyword: "关键词", week: "周次", competitor_url: "竞品链接",
  limit: "数量上限", offer: "1688 链接或 offer id", heat: "热度", trend: "趋势", competition: "竞争度",
  start_date: "测款开始日期", days: "天数", day: "数据日期", since_day: "起始日期", question: "问题", name: "商品名称",
  instruction: "要求" };
const RESULT_LABELS = { reply: "回答", suggestions: "建议", passed: "是否通过", reason: "理由", reject_reasons: "未通过原因",
  risks: "风险", score: "得分", breakdown: "打分明细", price_plan: "价格方案", title: "标题", selling_points: "卖点",
  detail: "详情", verdict: "结论", summary: "小结", offers: "货源", demands: "需求", path: "文件", goods_id: "商品",
  action: "动作", evidence: "依据", confidence: "把握", needs_write: "需写店铺", type: "类型", keyword: "需求词" };

const state = { agents: [], caps: [], tasks: [], selAgent: null, runsSource: "", selRun: null,
  open: new Set(), picks: {}, me: null };

// ---------- 工具 ----------
const $ = (sel) => document.querySelector(sel);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
const yuan = (fen) => "¥" + (fen / 100).toFixed(2);
const utc = (iso) => (iso ? new Date(iso.endsWith("Z") ? iso : iso + "Z") : null);
const timeStr = (iso) => { const d = utc(iso); return d ? d.toLocaleString("zh-CN", { hour12: false }) : ""; };
function ago(iso) {
  const d = utc(iso); if (!d) return "";
  const s = Math.max(0, Math.round((Date.now() - d) / 1000));
  if (s < 60) return `${s} 秒`;
  if (s < 3600) return `${Math.floor(s / 60)} 分 ${s % 60} 秒`;
  return `${Math.floor(s / 3600)} 小时 ${Math.floor((s % 3600) / 60)} 分`;
}
const badge = (status, map) => `<span class="badge s-${esc(status)}">${esc(map[status] || status)}</span>`;

function openId() { try { return localStorage.getItem("openId") || ""; } catch { return ""; } }

async function api(path, opts = {}) {
  const headers = { "X-Open-Id": openId(), ...(opts.body ? { "Content-Type": "application/json" } : {}) };
  const res = await fetch(path, { ...opts, headers, body: opts.body ? JSON.stringify(opts.body) : undefined });
  const data = await res.json().catch(() => null);
  if (!res.ok) {
    let msg = data && data.detail;
    if (Array.isArray(msg)) msg = msg.map((e) => `${(e.loc || []).slice(-1)[0] || ""} ${e.msg}`).join("；");
    throw new Error(msg || `请求失败（${res.status}）`);
  }
  return data;
}

// ---------- 顶栏：健康与身份 ----------
async function loadHealth() {
  try {
    const h = await api("/api/health");
    const caps = Object.entries(h.capabilities).map(([k, v]) =>
      `<span class="chip s-${esc(v)}" title="${esc(CAP_STATUS[v] || v)}"><i class="dot"></i>${esc(k)}</span>`).join("");
    $("#health").innerHTML = `<span class="chip">${h.mode === "fake" ? "模拟模式" : "真实模式"}</span>
      <span class="chip">今日 LLM ¥${h.llm_cost_today_cny.toFixed(2)}</span>
      ${h.jobs_failed ? `<span class="chip s-failed"><i class="dot"></i>失败作业 ${h.jobs_failed}</span>` : ""}${caps}`;
  } catch (e) { $("#health").innerHTML = `<span class="chip s-failed"><i class="dot"></i>${esc(e.message)}</span>`; }
}

async function loadMe() {
  state.me = null;
  $("#role").textContent = "";
  if (!openId()) { $("#role").textContent = "（未填写，只能查看）"; return; }
  try {
    state.me = await api("/api/me");
    $("#role").textContent = { owner: "店主", operator: "运营", viewer: "只读" }[state.me.role] || state.me.role;
  } catch (e) { $("#role").textContent = e.message; }
  renderTasks();
}
const canSubmit = () => state.me && ["owner", "operator"].includes(state.me.role);

// ---------- 智能体卡片 ----------
async function loadAgents() {
  state.agents = await api("/api/agents");
  $("#refreshed").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour12: false });
  $("#agents").innerHTML = state.agents.map(agentCard).join("");
  if (!$("#fAgent").options.length) fillAgentSelect();
}

function agentCard(a) {
  const list = (items) => (items.length ? `<ul>${items.slice(0, 4).map((x) => `<li>${esc(x)}</li>`).join("")}
    ${items.length > 4 ? `<li class="muted">还有 ${items.length - 4} 项</li>` : ""}</ul>` : "");
  let body = "";
  for (const r of a.running) {
    body += `<div class="k">${esc(r.label || r.capability)} · 已运行 ${ago(r.started_at)}</div>
      <div class="step">${esc(r.current_step || "…")}</div>
      <div class="k">${r.turns} 轮 · ${r.tokens} token · ${yuan(r.cost_fen)}</div>`;
  }
  if (a.waiting.length) body += `<div class="k">等待人工</div>${list(a.waiting)}`;
  if (a.paused.length) body += `<div class="k">已暂停</div>${list(a.paused)}`;
  if (a.queued.length) body += `<div class="k">排队 ${a.queued.length}</div>${list(a.queued)}`;
  if (!a.running.length && a.last_run) {
    const r = a.last_run;
    body += `<div class="k">上次：${esc(r.label || r.capability)} ${badge(r.status, RUN_STATUS)}
      ${esc(timeStr(r.finished_at || r.started_at))}</div>`;
  } else if (!a.running.length && !body) body = `<div class="k">还没有运行记录</div>`;
  return `<div class="agent s-${esc(a.state)} ${state.selAgent === a.agent ? "sel" : ""}" data-agent="${esc(a.agent)}">
    <div class="head"><span class="name">${esc(a.title)}</span>${badge(a.state, AGENT_STATE)}</div>${body}</div>`;
}

// ---------- 运行记录与步骤 ----------
async function loadRuns() {
  if (!state.selAgent) return;
  const q = state.runsSource ? `?source=${state.runsSource}` : "";
  const runs = await api(`/api/agents/${state.selAgent}/runs${q}`);
  $("#runsTable").innerHTML = `<tr><th>#</th><th>来源</th><th>环节</th><th>状态</th><th>开始</th>
    <th class="num">轮</th><th class="num">token</th><th class="num">费用</th></tr>` +
    (runs.map((r) => `<tr class="click ${state.selRun === r.id ? "sel" : ""}" data-run="${r.id}">
      <td>${r.id}</td><td>${esc(r.label)}</td><td>${esc(r.capability)}</td><td>${badge(r.status, RUN_STATUS)}</td>
      <td>${esc(timeStr(r.started_at))}</td><td class="num">${r.turns}</td><td class="num">${r.tokens}</td>
      <td class="num">${yuan(r.cost_fen)}</td></tr>`).join("") || `<tr><td colspan="8" class="muted">没有记录</td></tr>`);
}

async function loadRunDetail() {
  if (!state.selRun) return;
  const r = await api(`/api/runs/${state.selRun}`);
  const dur = r.finished_at ? Math.round((utc(r.finished_at) - utc(r.started_at)) / 1000) + " 秒" : "已运行 " + ago(r.started_at);
  $("#runDetail").classList.remove("muted");
  $("#runDetail").innerHTML = `<div class="section-head"><h2>运行 #${r.id}</h2>${badge(r.status, RUN_STATUS)}</div>
    <div class="kv small">
      <span class="k">来源</span><span>${esc(r.label)}</span>
      <span class="k">环节</span><span>${esc(r.capability)}</span>
      <span class="k">模型</span><span>${esc(r.model)}</span>
      <span class="k">用量</span><span>${r.turns} 轮 · ${r.tokens} token · ${yuan(r.cost_fen)}</span>
      <span class="k">时长</span><span>${esc(dur)}</span>
      ${r.status === "running" ? `<span class="k">当前</span><span>${esc(r.current_step)}</span>` : ""}
    </div>
    ${r.error ? `<p class="err small">${esc(r.error)}</p>` : ""}
    <ol class="steps">${r.steps.map((x) => `<li><span class="kind">${esc(x.kind)}</span><b>${esc(x.name)}</b>
      ${x.tokens ? `<span class="muted small"> · ${x.tokens} token</span>` : ""}
      <div class="muted small">${esc(x.summary)}</div></li>`).join("") || `<li class="muted">没有步骤（只有一轮模型调用）</li>`}</ol>`;
}

function selectAgent(agent) {
  state.selAgent = agent; state.selRun = null;
  const a = state.agents.find((x) => x.agent === agent);
  $("#runsTitle").textContent = `${a ? a.title : agent} · 运行记录`;
  $("#runDetail").className = "detail muted"; $("#runDetail").textContent = "点一条运行记录查看步骤";
  $("#runsPanel").hidden = false;
  document.querySelectorAll(".agent").forEach((el) => el.classList.toggle("sel", el.dataset.agent === agent));
  loadRuns().catch(showErr);
  $("#runsPanel").scrollIntoView({ behavior: "smooth", block: "nearest" });
}

// ---------- 提交表单（按参数 JSON Schema 生成） ----------
function fillAgentSelect() {
  const agents = state.agents.filter((a) => state.caps.some((c) => c.agent === a.agent));
  $("#fAgent").innerHTML = agents.map((a) => `<option value="${esc(a.agent)}">${esc(a.title)}</option>`).join("");
  fillCapSelect();
}

function fillCapSelect() {
  const caps = state.caps.filter((c) => c.agent === $("#fAgent").value);
  $("#fCap").innerHTML = caps.map((c) => `<option value="${esc(c.key)}">${esc(c.title)}</option>`).join("");
  renderParams();
}

function fieldType(p) {
  if (p.type) return p;
  const opt = (p.anyOf || []).find((x) => x.type !== "null");
  return opt ? { ...p, ...opt, optional: true } : p;
}

function renderParams() {
  const cap = state.caps.find((c) => c.key === $("#fCap").value);
  if (!cap) { $("#fParams").innerHTML = ""; $("#fCapDesc").textContent = ""; return; }
  const extra = [cap.uses_web.length ? `需要网页读取（${cap.uses_web.join("、")}），06:00–08:30 会顺延` : "",
    cap.promote_to ? `结果可转为${cap.promote_to === "candidate" ? "候选" : "建议"}` : ""].filter(Boolean).join("；");
  $("#fCapDesc").textContent = [cap.description, extra].filter(Boolean).join(" · ");
  const schema = cap.params_schema, required = new Set(schema.required || []);
  $("#fParams").innerHTML = Object.entries(schema.properties || {}).map(([key, raw]) => {
    const p = fieldType(raw), fen = key.endsWith("_fen");
    let label = FIELD_LABELS[key] || (fen ? "目标售价" : p.title || key);
    if (fen) label += "（元）";
    const req = required.has(key);
    const hint = p.description ? ` title="${esc(p.description)}"` : "";
    const dflt = p.default ?? "";
    let input;
    if (p.type === "boolean") input = `<input type="checkbox" name="${key}" ${dflt ? "checked" : ""}>`;
    else if (p.enum) input = `<select name="${key}">${p.enum.map((v) => `<option ${v === dflt ? "selected" : ""}>${esc(v)}</option>`).join("")}</select>`;
    else {
      const type = p.format === "date" ? "date" : (p.type === "integer" || p.type === "number") ? "number" : "text";
      const step = fen ? "0.01" : p.type === "number" ? "any" : "1";
      const val = fen ? (dflt ? dflt / 100 : "") : (dflt === 0 && !req ? dflt : dflt);
      const list = key === "goods_id" ? ` list="goodsList"` : "";
      const ph = p.description && type === "text" ? ` placeholder="${esc(p.description)}"` : "";
      input = `<input type="${type}" name="${key}" step="${step}" value="${esc(val)}"${list}${ph}
        ${p.minimum != null ? `min="${p.minimum}"` : ""} ${p.maximum != null ? `max="${p.maximum}"` : ""} ${req ? "required" : ""}>`;
    }
    return `<label${hint}>${esc(label)}${req ? " *" : ""}${input}</label>`;
  }).join("") || `<p class="muted small">这项能力不需要参数</p>`;
}

function collectParams(cap) {
  const out = {};
  for (const [key, raw] of Object.entries(cap.params_schema.properties || {})) {
    const p = fieldType(raw), el = $("#fParams").querySelector(`[name="${key}"]`);
    if (!el) continue;
    if (p.type === "boolean") { out[key] = el.checked; continue; }
    const v = el.value.trim();
    if (v === "") continue; // 留空用服务端默认值
    if (key.endsWith("_fen")) out[key] = Math.round(parseFloat(v) * 100);
    else if (p.type === "integer") out[key] = parseInt(v, 10);
    else if (p.type === "number") out[key] = parseFloat(v);
    else out[key] = v;
  }
  return out;
}

async function submitForm(ev) {
  ev.preventDefault();
  const cap = state.caps.find((c) => c.key === $("#fCap").value);
  const msg = $("#fMsg");
  if (!cap) return;
  if (!canSubmit()) { msg.className = "small err"; msg.textContent = "请先在右上角填写店主或运营的 open_id"; return; }
  try {
    const t = await api("/api/adhoc", { method: "POST", body: { capability: cap.key, params: collectParams(cap), note: $("#fNote").value } });
    msg.className = "small muted"; msg.textContent = `已提交：单独任务 #${t.id}`;
    $("#fNote").value = "";
    state.open.add(t.id);
    await refresh();
  } catch (e) { msg.className = "small err"; msg.textContent = e.message; }
}

// ---------- 单独任务列表 ----------
async function loadTasks() {
  const f = $("#adhocFilter").value;
  state.tasks = await api("/api/adhoc" + (f ? `?status=${f}` : ""));
  renderTasks();
}

function renderTasks() {
  const html = state.tasks.map((t) => {
    const cap = state.caps.find((c) => c.key === t.capability);
    const params = Object.entries(t.params).filter(([, v]) => v !== "" && v !== null)
      .map(([k, v]) => `${FIELD_LABELS[k] || k}=${k.endsWith("_fen") ? yuan(v) : v}`).join("，");
    const mine = state.me && (state.me.role === "owner" || state.me.open_id === t.submitted_by);
    const acts = [];
    if (t.status === "done" || t.status === "promoted") acts.push(`<button class="ghost" data-act="toggle" data-id="${t.id}">${state.open.has(t.id) ? "收起" : "结果"}</button>`);
    if (mine && ["queued", "deferred", "running"].includes(t.status)) acts.push(`<button class="danger" data-act="cancel" data-id="${t.id}">取消</button>`);
    if (canSubmit() && t.can_promote && cap)
      acts.push(`<button data-act="promote" data-id="${t.id}">转为${cap.promote_to === "candidate" ? "候选" : "建议"}</button>`);
    if (canSubmit() && ["failed", "cancelled"].includes(t.status)) acts.push(`<button class="ghost" data-act="resubmit" data-id="${t.id}">重新提交</button>`);
    if (t.run_id) acts.push(`<button class="ghost" data-act="run" data-id="${t.id}">运行记录</button>`);
    const showResult = state.open.has(t.id) && (t.status === "done" || t.status === "promoted");
    return `<div class="task" data-task="${t.id}">
      <div class="head"><span class="title">#${t.id} ${esc(t.title)}</span>${badge(t.status, TASK_STATUS)}
        <div class="actions">${acts.join("")}</div></div>
      <div class="meta">${esc(params || "无参数")}${t.note ? " · " + esc(t.note) : ""} · ${esc(t.submitted_by)} · ${esc(timeStr(t.created_at))}</div>
      ${t.error ? `<div class="small ${t.status === "deferred" ? "muted" : "err"}">${esc(t.error)}</div>` : ""}
      ${t.promoted_to ? `<div class="small muted">已转为 ${esc(t.promoted_to)}</div>` : ""}
      ${showResult ? `<div class="result">${renderResult(t)}</div>` : ""}</div>`;
  }).join("");
  $("#adhocList").innerHTML = html || `<p class="muted">还没有单独任务</p>`;
}

function renderResult(t) {
  const r = t.result || {};
  // 诊断结果可以只挑几条转为建议
  if (t.capability === "ops.diagnose" && Array.isArray(r.suggestions)) {
    const picks = state.picks[t.id] || new Set(r.suggestions.map((_, i) => i));
    state.picks[t.id] = picks;
    const rest = { ...r }; delete rest.suggestions;
    return (Object.keys(rest).length ? renderValue(rest) : "") + (r.suggestions.length ? r.suggestions.map((s, i) =>
      `<label class="pick"><input type="checkbox" data-pick="${t.id}" value="${i}" ${picks.has(i) ? "checked" : ""}
        ${t.status !== "done" ? "disabled" : ""}><span><b>${esc(s.goods_id || "整店")}</b> ${esc(s.action)}
        <span class="muted">· ${esc(s.reason)}</span></span></label>`).join("") : `<p class="muted">没有发现需要调整的地方</p>`);
  }
  return renderValue(r);
}

function renderValue(v, depth = 0) {
  if (v === null || v === undefined || v === "") return `<span class="muted">—</span>`;
  if (typeof v === "boolean") return v ? "是" : "否";
  if (typeof v !== "object") return `<span class="reply">${esc(v)}</span>`;
  if (Array.isArray(v)) {
    if (!v.length) return `<span class="muted">无</span>`;
    if (depth > 2) return `<pre>${esc(JSON.stringify(v, null, 1))}</pre>`;
    return `<ul>${v.map((x) => `<li>${renderValue(x, depth + 1)}</li>`).join("")}</ul>`;
  }
  if (depth > 2) return `<pre>${esc(JSON.stringify(v, null, 1))}</pre>`;
  const shown = Object.entries(v).filter(([, x]) => x !== null && x !== undefined && x !== "");
  return `<div class="kv">${shown.map(([k, x]) =>
    `<span class="k">${esc(RESULT_LABELS[k] || k)}</span><span>${k.endsWith("_fen") && typeof x === "number" ? yuan(x) : renderValue(x, depth + 1)}</span>`).join("")}</div>`;
}

async function taskAction(act, id) {
  const t = state.tasks.find((x) => x.id === id);
  try {
    if (act === "toggle") { state.open.has(id) ? state.open.delete(id) : state.open.add(id); renderTasks(); return; }
    if (act === "run") {
      selectAgent(t.agent); state.selRun = t.run_id; await loadRunDetail(); return;
    }
    if (act === "cancel") {
      if (!confirm(`取消单独任务 #${id}？`)) return;
      await api(`/api/adhoc/${id}/cancel`, { method: "POST" });
    }
    if (act === "promote") {
      const body = {};
      if (t.capability === "ops.diagnose") {
        const picks = [...(state.picks[id] || [])].sort((a, b) => a - b);
        if (!picks.length) { alert("至少勾选一条建议"); return; }
        body.indexes = picks;
      }
      const res = await api(`/api/adhoc/${id}/promote`, { method: "POST", body });
      alert(`已转为 ${res.promoted_to}，已推送飞书等待拍板`);
    }
    if (act === "resubmit") {
      const n = await api("/api/adhoc", { method: "POST", body: { capability: t.capability, params: t.params, note: t.note } });
      state.open.add(n.id);
    }
    await refresh();
  } catch (e) { alert(e.message); }
}

// ---------- 刷新与事件 ----------
function showErr(e) { $("#refreshed").textContent = "刷新失败：" + e.message; }
function pageErr(v, e) {
  const el = document.getElementById("v-" + v);
  if (el && !el.querySelector(".page-err")) el.insertAdjacentHTML("afterbegin", `<p class="err page-err">${esc(e.message)}</p>`);
}

// ---------- 页面切换（#/agents、#/overview …）。只刷新当前页 ----------
const VIEWS = ["agents", "overview", "selection", "board", "data", "rules", "costs"];
const currentView = () => { const v = location.hash.replace(/^#\/?/, "").split("?")[0]; return VIEWS.includes(v) ? v : "agents"; };

function route() {
  const v = currentView();
  document.querySelectorAll("main[data-view]").forEach((m) => { m.hidden = m.dataset.view !== v; });
  document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("on", a.dataset.view === v));
  refresh();
}

async function refresh() {
  const v = currentView();
  if (v !== "agents") {
    loadHealth();
    // 规则页不自动刷新，免得冲掉正在填写的内容
    if (window.PAGES && PAGES[v] && (PAGES[v].auto !== false || !PAGES[v].loaded)) {
      try { await PAGES[v].load(document.getElementById("v-" + v)); PAGES[v].loaded = true; } catch (e) { pageErr(v, e); }
    }
    return;
  }
  try {
    await Promise.all([loadAgents(), loadTasks(), loadHealth()]);
    if (state.selAgent) await loadRuns();
    const run = state.selRun && document.querySelector(`#runDetail .s-running`);
    if (run) await loadRunDetail();
  } catch (e) { showErr(e); }
}

function bind() {
  $("#openId").value = openId();
  $("#openId").addEventListener("change", (e) => {
    try { localStorage.setItem("openId", e.target.value.trim()); } catch { /* 无痕模式下不保存 */ }
    loadMe();
  });
  $("#agents").addEventListener("click", (e) => { const el = e.target.closest(".agent"); if (el) selectAgent(el.dataset.agent); });
  $("#runsClose").addEventListener("click", () => { state.selAgent = null; state.selRun = null; $("#runsPanel").hidden = true;
    document.querySelectorAll(".agent.sel").forEach((el) => el.classList.remove("sel")); });
  $("#runsSource").addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    state.runsSource = b.dataset.v;
    $("#runsSource").querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b));
    loadRuns().catch(showErr);
  });
  $("#runsTable").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-run]"); if (!tr) return;
    state.selRun = +tr.dataset.run;
    $("#runsTable").querySelectorAll("tr").forEach((x) => x.classList.toggle("sel", x === tr));
    loadRunDetail().catch(showErr);
  });
  $("#fAgent").addEventListener("change", fillCapSelect);
  $("#fCap").addEventListener("change", renderParams);
  $("#adhocForm").addEventListener("submit", submitForm);
  $("#adhocFilter").addEventListener("change", () => loadTasks().catch(showErr));
  $("#adhocList").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-act]"); if (b) taskAction(b.dataset.act, +b.dataset.id);
  });
  $("#adhocList").addEventListener("change", (e) => {
    const cb = e.target.closest("input[data-pick]"); if (!cb) return;
    const set = state.picks[+cb.dataset.pick]; const i = +cb.value;
    cb.checked ? set.add(i) : set.delete(i);
  });
}

async function start() {
  bind();
  try {
    const [caps, products] = await Promise.all([api("/api/agent-capabilities"), api("/api/products")]);
    state.caps = caps;
    $("#goodsList").innerHTML = products.map((p) => `<option value="${esc(p.goods_id)}">${esc(p.name)}</option>`).join("");
  } catch (e) { showErr(e); }
  await loadMe();
  window.addEventListener("hashchange", route);
  route();
  setInterval(refresh, POLL_MS);
}

start();

"use strict";
const $ = (id) => document.getElementById(id);
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
let state = {
  cases: [],
  rubric: [],
  history: { runs: [], reviews: [] },
  current: null,
  run: null,
  busy: false,
};
let toastTimer;
function toast(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 6000);
}
async function api(path, body) {
  const res = await fetch(
    path,
    body === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        },
  );
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "请求失败");
  return data;
}
function download(name, value, type = "application/json") {
  const url = URL.createObjectURL(new Blob([value], { type }));
  const a = document.createElement("a");
  a.href = url;
  a.download = name;
  a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
function switchTab(history) {
  $("workspace").hidden = history;
  $("history").hidden = !history;
  $("tab-work").classList.toggle("active", !history);
  $("tab-history").classList.toggle("active", history);
  $("tab-work").setAttribute("aria-selected", String(!history));
  $("tab-history").setAttribute("aria-selected", String(history));
  if (history) renderHistory();
}
function nav() {
  $("case-count").textContent = state.cases.length.toString().padStart(2, "0");
  $("case-list").innerHTML = state.cases
    .map(
      (c, i) =>
        `<button data-case="${i}" class="${c.id === state.current?.id ? "selected" : ""}"><small>${String(i + 1).padStart(2, "0")}</small>${esc(c.title)}</button>`,
    )
    .join("");
  $("case-list")
    .querySelectorAll("[data-case]")
    .forEach(
      (b) =>
        (b.onclick = () => {
          if (state.busy) return toast("当前检查完成后可切换案例");
          loadCase(state.cases[Number(b.dataset.case)]);
        }),
    );
}
function loadCase(c, run = null) {
  state.current = structuredClone(c);
  state.run = run;
  $("case-title").textContent = c.title;
  $("question").value = c.question;
  $("answer").value = c.answer;
  $("answered-at").value = c.answered_at || "";
  $("network").value = c.network_mode;
  $("audience").innerHTML = [
    ...c.audience.level,
    ...c.audience.identity,
    ...c.audience.campus,
  ]
    .map((x) => `<span class="tag">${esc(x)}</span>`)
    .join("");
  $("case-extra").value = JSON.stringify(
    {
      id: c.id,
      title: c.title,
      time_sensitive: c.time_sensitive,
      audience: c.audience,
      expected_points: c.expected_points,
      claims: c.claims,
    },
    null,
    2,
  );
  $("sources-json").value = JSON.stringify(c.sources, null, 2);
  renderSources(c.sources, run?.evidence);
  nav();
  switchTab(false);
  if (run) renderResult(run);
  else
    $("result").innerHTML =
      '<section class="empty-state"><span class="large-symbol">↗</span><h2>等待检查</h2><p>选择案例或编辑问答，查看来源冲突、有效范围和逐项评价。</p></section>';
}
const statusNames = {
  usable: "可用于核对",
  unverified: "待核验",
  unknown_time: "回答时间未知",
  not_yet_valid: "尚未生效",
  expired: "单次事项已结束",
  superseded: "已被明确替代",
  out_of_scope: "不适用当前对象",
  future_publication: "晚于回答时间",
};
function renderSources(sources, evidence) {
  $("source-count").textContent = sources.length + " SOURCES";
  $("sources").innerHTML = sources.length
    ? sources
        .map((s) => {
          const status = evidence?.source_status[s.id];
          return `<article class="source"><div class="source-meta">${esc(s.channel)} · ${esc(s.id)} <span class="tag ${status && status !== "usable" ? "warn" : ""}">${esc(status ? statusNames[status] : s.verified ? "正文已核验" : "正文待核验")}</span></div><h3>${esc(s.title)}</h3><p>${esc(s.text)}</p><div class="source-meta">发布 ${esc(s.published_at || "未知")} · ${esc(s.validity_scope)}${s.valid_until ? " · 截止 " + esc(s.valid_until) : ""}</div></article>`;
        })
        .join("")
    : '<p class="hint">尚无参考正文。链接本身不能证明回答正确。</p>';
}
function readCase() {
  const extra = JSON.parse($("case-extra").value);
  return {
    ...extra,
    question: $("question").value,
    answer: $("answer").value,
    answered_at: $("answered-at").value || null,
    network_mode: $("network").value,
    sources: JSON.parse($("sources-json").value),
  };
}
function riskCheck(title, reason, status = "待核实") {
  return `<div class="check"><span class="tag ${status === "冲突" || status === "错误" ? "bad" : "warn"}">${esc(status)}</span><div class="check-title">${esc(title)}</div><p>${esc(reason)}</p></div>`;
}
function renderResult(run) {
  const d = run.decision,
    ev = run.evidence,
    j = run.judgment;
  const checks = [];
  ev.conflicts.forEach((c) =>
    checks.push(
      riskCheck(
        "来源事实冲突 · " + c.key,
        c.values.map((v) => v.source_id + "：" + v.value).join(" / "),
        "冲突",
      ),
    ),
  );
  ev.claims.forEach((c) => {
    if (!["supported", "conflict"].includes(c.verdict))
      checks.push(
        riskCheck(
          "回答事实 · " + c.key,
          `${c.quote}；${c.evidence.map((x) => x.source_id + "：" + x.value).join(" / ") || "缺少对应事实依据"}`,
          c.verdict === "contradicted" ? "错误" : "待核实",
        ),
      );
  });
  run.signals.forEach((s) =>
    checks.push(
      riskCheck(
        {
          sensitive_candidate: "指令与安全边界",
          reputation_candidate: "实体舆情",
          defensive_candidate: "转介与知识缺口",
          knowledge_candidate: "知识缺口候选",
          timeliness_candidate: "时效性线索",
          boundary_candidate: "权限与操作承诺",
          privacy_candidate: "隐私保护",
        }[s.code] || s.code,
        s.reason,
        "候选",
      ),
    ),
  );
  d.holds
    .filter(
      (h) =>
        !(ev.conflicts.length && h === "来源存在未消除的事实冲突") &&
        !(
          ev.claims.some((c) => c.verdict === "conflict") &&
          h === "回答中的关键事实尚无一致依据"
        ),
    )
    .forEach((h) => checks.push(riskCheck("待确认事项", h)));
  const scoreRows = state.rubric
    .map((r) => {
      const s = j.scores[r.id];
      return `<div class="score-row"><div><strong>${esc(r.name)}</strong> <small>权重 ${r.weight}</small><p>${esc(s.reason)}</p>${s.source_quote ? `<div class="evidence-quote">${esc(s.source_ids.join(" / "))}：${esc(s.source_quote)}</div>` : ""}</div><div class="score-num">${s.level === null ? "—" : ((s.level / 5) * r.weight).toFixed(0)}<small> / ${r.weight}</small></div></div>`;
    })
    .join("");
  $("result").innerHTML =
    `<section class="panel"><div class="panel-heading"><h2>检查结果</h2><span class="small-label">${run.mode === "llm" ? "AI REVIEW" : "RULE SCREEN"}</span></div><div class="result-top"><div><div class="score-large">${d.final_total === null ? "—" : d.final_total}<span>/ 100</span></div><div class="verdict">${esc(d.label)}</div></div><div class="hint">${d.unknown_dimensions.length ? d.unknown_dimensions.length + " 项待评分" : "九项评分已齐全"}<br>${esc(run.mode === "llm" ? run.meta.model : "规则引擎")} · ${run.meta.elapsed_ms} ms</div></div><p class="hint">${esc(j.summary)}</p>${d.cap < 100 ? `<p class="tag bad">风险封顶 ${d.cap} 分</p>` : ""}${checks.join("") || '<p class="hint">未发现规则候选。完整评价仍需核对九项标准。</p>'}</section><section class="panel"><div class="panel-heading"><h2>九项评分</h2><span class="small-label">WEIGHTED / 100</span></div>${scoreRows}${j.missing_points.length ? `<p class="hint">缺失要点：${esc(j.missing_points.join("；"))}</p>` : ""}</section>${reviewForm(run)}`;
  $("save-review").onclick = saveReview;
  $("source-select").onchange = () => {
    const s = run.case.sources.find((x) => x.id === $("source-select").value);
    $("review-source-quote").value = s?.text || "";
  };
  const reviews = state.history.reviews.filter((r) => r.run_id === run.run_id);
  if (reviews.length)
    $("review-saved").innerHTML =
      `已有 ${reviews.length} 次复核，最近结论：${esc(reviews.at(-1).decision.label)}。机器初评已保留。`;
}
function reviewForm(run) {
  return `<section class="panel"><div class="panel-heading"><h2>人工复核</h2><span class="small-label">HUMAN REVIEW</span></div><p class="hint">逐项选择等级。修正材料或回答后，应重新检查并保留新旧记录。</p><div class="review-grid">${state.rubric.map((r) => `<div><label for="review-${r.id}">${esc(r.name)} · ${r.weight}</label><select id="review-${r.id}"><option value="">待核实</option>${[0, 1, 3, 5].map((v) => `<option value="${v}" ${run.judgment.scores[r.id].level === v ? "selected" : ""}>${v} / 5 · ${esc(r.anchors[v])}</option>`).join("")}</select></div>`).join("")}</div><label for="source-select">事实与引用评分依据</label><select id="source-select"><option value="">尚无明确依据</option>${run.case.sources
    .filter((s) => run.evidence.active_source_ids.includes(s.id))
    .map(
      (s) =>
        `<option value="${esc(s.id)}">${esc(s.id + " · " + s.title)}</option>`,
    )
    .join(
      "",
    )}</select><textarea id="review-source-quote" rows="2" placeholder="填写支持评分的来源原句；无证据时，事实与引用项保留待核实" aria-label="复核引用原句"></textarea><div class="risk-list">${Object.entries(
    {
      privacy_leak: "确认真实隐私泄露",
      dangerous_steps: "确认危险操作",
      unsupported_accusation: "确认无依据指控",
      fabrication: "确认虚构内容",
      latest_without_source: "最新政策缺来源",
      boundary_violation: "确认越权或泄露内部内容",
    },
  )
    .map(
      ([k, v]) =>
        `<label><input type="checkbox" name="review-risk" value="${k}">${v}</label>`,
    )
    .join(
      "",
    )}</div><div class="two-col"><div><label for="reviewer">复核人代号</label><input id="reviewer" placeholder="如：reviewer-a" maxlength="80"></div><div><label for="resolution">问题解决状态</label><select id="resolution"><option>待核实</option><option>未解决</option><option>部分解决</option><option>已解决</option></select></div></div><label for="review-reason">复核理由与修改建议</label><textarea id="review-reason" rows="3" placeholder="说明判断依据、候选风险是否成立、哪些内容需要修改"></textarea><div class="review-footer"><button id="save-review" class="primary">保存人工复核</button><button id="export-case" class="outline">导出本条</button></div><div id="review-saved" class="hint"></div></section>`;
}
async function saveReview() {
  const run = state.run;
  if (!run) return;
  try {
    const reason = $("review-reason").value.trim();
    if (!reason || !$("reviewer").value.trim())
      return toast("请填写复核人代号和具体理由");
    const sourceId = $("source-select").value,
      sourceQuote = $("review-source-quote").value;
    const scores = {};
    state.rubric.forEach((r) => {
      const v = $("review-" + r.id).value;
      const cited = ["accuracy", "citations"].includes(r.id) && v !== "";
      scores[r.id] = {
        level: v === "" ? null : Number(v),
        reason,
        source_ids: cited && sourceId ? [sourceId] : [],
        answer_quote: "",
        source_quote: cited ? sourceQuote : "",
      };
    });
    const body = {
      run_id: run.run_id,
      reviewer: $("reviewer").value,
      reason,
      resolution: $("resolution").value,
      confirmed_risks: Array.from(
        document.querySelectorAll('[name="review-risk"]:checked'),
        (x) => x.value,
      ),
      judgment: {
        scores,
        findings: [],
        missing_points: run.judgment.missing_points,
        summary: reason,
      },
    };
    $("save-review").disabled = true;
    const review = await api("/api/review", body);
    state.history.reviews.push(review);
    $("review-saved").innerHTML =
      `<div class="review-confirmation">已保存 · ${esc(review.decision.label)} · ${review.decision.final_total ?? "待评分"} / 100<br>机器初评与人工结论分别保留。</div>`;
    toast("人工复核已保存");
  } catch (e) {
    toast(e.message);
  } finally {
    if ($("save-review")) $("save-review").disabled = false;
  }
}
async function run() {
  if (state.busy) return;
  try {
    const c = readCase();
    state.busy = true;
    $("run").disabled = true;
    $("engine-badge").textContent = "RUNNING";
    $("run-note").textContent =
      $("mode").value === "llm"
        ? "正在调用已配置模型并核验返回字段…"
        : "正在核对来源、时间、适用范围与风险线索…";
    const result = await api("/api/run", { case: c, mode: $("mode").value });
    state.history.runs.push(result);
    state.current = result.case;
    state.run = result;
    renderSources(result.case.sources, result.evidence);
    renderResult(result);
    $("history-count").textContent = state.history.runs.length;
    toast("检查完成，结果已保存在本机");
  } catch (e) {
    state.run = null;
    $("result").innerHTML =
      `<div class="error-box"><strong>本次检查未完成</strong><p>${esc(e.message)}</p></div>`;
    toast(e.message);
  } finally {
    state.busy = false;
    $("run").disabled = false;
    $("engine-badge").textContent = "READY";
    $("run-note").textContent =
      "规则候选与AI初评均可人工复核；失败请求不生成分数。";
  }
}
function renderHistory() {
  const data = state.history;
  $("history").innerHTML =
    `<div class="history-stats"><div><strong>${data.runs.length}</strong><span>运行记录</span></div><div><strong>${data.reviews.length}</strong><span>人工复核</span></div><div><strong>${new Set(data.reviews.map((x) => x.run_id)).size}</strong><span>已复核运行</span></div></div><button id="export-csv" class="outline">导出汇总 CSV ↗</button>${
      data.runs.length
        ? data.runs
            .toReversed()
            .map((r) => {
              const reviews = data.reviews.filter((x) => x.run_id === r.run_id);
              return `<article class="history-item"><h3>${esc(r.case.title)} <span class="tag">${esc(r.decision.label)}</span></h3><p>${esc(r.case.question)}</p><p>${esc(new Date(r.created_at).toLocaleString("zh-CN"))} · ${esc(r.mode === "llm" ? r.meta.model : "规则引擎")} · ${reviews.length} 次人工复核</p>${reviews.map((v) => `<div class="evidence-quote">${esc(v.reviewer)} · ${esc(v.decision.label)} · ${esc(v.resolution)}<br>${esc(v.reason)}</div>`).join("")}<button class="text-button" data-run="${esc(r.run_id)}">查看完整记录 →</button></article>`;
            })
            .join("")
        : '<p class="hint">尚无运行记录。</p>'
    }`;
  $("export-csv").onclick = async () => {
    const r = await fetch("/api/export.csv");
    download("dandao-review.csv", await r.text(), "text/csv;charset=utf-8");
  };
  document.querySelectorAll("[data-run]").forEach(
    (b) =>
      (b.onclick = () => {
        const run = data.runs.find((r) => r.run_id === b.dataset.run);
        loadCase(run.case, run);
      }),
  );
}
async function init() {
  try {
    const data = await api("/api/bootstrap");
    state = { ...state, ...data, current: null, run: null, busy: false };
    $("rule-version").textContent = data.rule_version;
    $("history-count").textContent = data.history.runs.length;
    $("model-state").textContent = data.settings.configured
      ? "模型接口已配置"
      : "本地规则 · 模型待配置";
    $("mode").options[1].disabled = !data.settings.configured;
    $("rubric-table").innerHTML =
      `<table><thead><tr><th>维度 / 权重</th><th>0</th><th>1</th><th>3</th><th>5</th></tr></thead><tbody>${data.rubric.map((r) => `<tr><td>${esc(r.name)} / ${r.weight}</td>${[0, 1, 3, 5].map((k) => `<td>${esc(r.anchors[k])}</td>`).join("")}</tr>`).join("")}</tbody></table>`;
    loadCase(data.cases[0]);
    $("run").onclick = run;
    $("show-rules").onclick = () => $("rules-dialog").showModal();
    $("close-rules").onclick = () => $("rules-dialog").close();
    $("tab-work").onclick = () => switchTab(false);
    $("tab-history").onclick = () => switchTab(true);
    $("export-json").onclick = async () => {
      try {
        download(
          "dandao-review-records.json",
          JSON.stringify(await api("/api/export"), null, 2),
        );
      } catch (e) {
        toast(e.message);
      }
    };
    $("result").addEventListener("click", (e) => {
      if (e.target.id === "export-case" && state.run)
        download(
          "dandao-" + state.run.case.id + ".json",
          JSON.stringify(
            {
              run: state.run,
              reviews: state.history.reviews.filter(
                (v) => v.run_id === state.run.run_id,
              ),
            },
            null,
            2,
          ),
        );
    });
    $("import-btn").onclick = () => $("import-file").click();
    $("import-file").onchange = async (e) => {
      try {
        const f = e.target.files[0];
        if (!f) return;
        if (f.size > 2e6) throw new Error("文件超过2MB，请分批导入");
        const result = await api("/api/import", {
          cases: JSON.parse(await f.text()),
        });
        const ids = new Set(result.cases.map((c) => c.id));
        state.cases = [
          ...state.cases.filter((c) => !ids.has(c.id)),
          ...result.cases,
        ];
        loadCase(result.cases[0]);
        toast(
          `已导入${result.cases.length}条，合并为${result.groups.length}组相似问法`,
        );
      } catch (e) {
        toast(e.message);
      } finally {
        e.target.value = "";
      }
    };
    $("new-case").onclick = () => {
      if (state.busy) return;
      const c = structuredClone(data.cases[0]);
      c.id = "case-" + Date.now();
      c.title = "新问答";
      c.question = "请填写问题";
      c.answer = "";
      c.answered_at = new Intl.DateTimeFormat("en-CA", {
        timeZone: "Asia/Shanghai",
      }).format(new Date());
      c.sources = [];
      c.claims = [];
      c.expected_points = [];
      c.time_sensitive = false;
      state.cases.push(c);
      loadCase(c);
    };
  } catch (e) {
    $("result").innerHTML = `<div class="error-box">${esc(e.message)}</div>`;
  }
}
init();

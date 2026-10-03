"use strict";
const $ = (s) => document.querySelector(s),
  $$ = (s) => [...document.querySelectorAll(s)];
const labels = {
  event: "日常经历",
  fact: "关键事实",
  preference: "偏好习惯",
  commitment: "约定任务",
  relationship: "人物关系",
  profile: "人物画像",
  resource: "资源线索",
  self: "自我认知",
};
const fields = {
  compress_input_chars: "每批压缩输入预算（字符）",
  auto_migrate: "自动安全迁移旧记忆",
  mutual_exclusion: "迁移成功后互斥旧插件",
  migration_max_chars: "旧记忆迁移字符上限",
  migration_decay_half_life_days: "导入时的记忆年龄折算（天）",
  enabled: "启用记忆系统",
  bootstrap_seed: "旧历史播种",
  inject_recent_raw: "注入最近原文",
  capture_enabled: "记录对话与感知",
  auto_inject: "持续上下文注入",
  fact_view: "事实视图",
  expand_query: "窄查询自动扩词召回",
  compress_persona: "压缩带人设",
  audit_persona: "审计带人设",
  inject_mode: "注入形态",
  proactive_jitter: "主动感知随机偏移（秒）",
  proactive_min_sessions: "每轮最少会话数",
  proactive_max_sessions: "每轮最多会话数",
  proactive_rotate: "轮流挑选会话",
  memorize_cover_check: "保存永久记忆前先查事实覆盖",
  permanent_tidy_enabled: "永久记忆定期整理",
  permanent_tidy_on_write: "记住后立即整理",
  permanent_cap: "永久记忆条数上限",
  permanent_budget_chars: "永久记忆字符预算",
  permanent_tidy_batch: "每次整理条数",
  permanent_tidy_days: "同一条整理间隔（天）",
  fact_merge_cross_threshold: "跨类别重复阈值",
  fact_merge_evidence: "去重判定附原文证据",
  rotate_enabled: "轮换召回槽位（总开关）",
  rotate_count: "轮换槽位条数（事实槽）",
  rotate_keep_rounds: "轮换保留轮数",
  rotate_min_hits: "轮换命中阈值",
  rotate_cooldown_rounds: "轮换冷却轮数",
  rotate_archive_enabled: "档案槽独立开关（不关事实槽）",
  rotate_archive_chars: "档案槽每批正文字数上限",
  rotate_archive_count: "档案槽每批条数",
  cold_archive_enabled: "冷归档",
  cold_archive_days: "冷归档天数",
  cold_archive_path: "冷归档库文件路径",
  cold_archive_auto: "冷归档自动执行",
  inject_budget_ms: "每轮注入时间预算（毫秒）",
  permanent_dedupe_cross_threshold: "永久记忆跨会话去重阈值",
  fact_recall_min_score: "事实内容匹配门槛",
  recall_skip_media: "召回跳过表情/图片-only 消息",
  compress_input_max_chars: "每批压缩的原文上限（字符）",
  compress_stale_after_days: "陈旧记忆几天后开始消化",
  fact_sink_threshold: "常驻下沉阈值",
  compress_idle_after_hours: "会话闲置几小时后收尾",
  compress_idle_cooldown_min: "收尾压缩的冷却（分钟）",
  compress_batches_per_job: "单个压缩任务最多连压几批",
  compress_batch_mode: "压缩分批方式",
  compress_rounds: "每批压缩轮数",
  threshold: "首层压缩阈值",
  batch_size: "首层每批条数",
  probability: "自动压缩概率",
  max_level: "最大压缩层级",
  compress_model: "压缩与合并模型",
  audit_model: "审计模型",
  embedding_model: "可选向量模型（启用后可能计费）",
  semantic_enabled: "启用向量检索（默认关闭）",
  audit_enabled: "后台审计",
  audit_interval: "审计间隔（秒）",
  audit_batch: "每批审计事实数",
  audit_recheck_days: "审计冷却（天）",
  audit_daily_calls: "审计每日调用上限",
  model_timeout: "模型超时（秒）",
  model_retries: "模型失败重试次数",
  worker_count: "后台并发数",
  context_chars: "上下文字符预算",
  token_warning: "估算 Token 警告线",
  recall_keywords: "回忆提示词",
  recall_scope: "Bot可访问范围",
  top_k: "感知事实批量（×10）",
  proactive_enabled: "定时主动感知",
  proactive_interval: "主动感知间隔（秒）",
  proactive_sessions: "主动感知会话",
  compress_instruction: "压缩补充要求",
  fact_merge_enabled: "写入时自动合并事实",
  fact_merge_threshold: "事实合并触发阈值",
  fact_merge_soft_chars: "事实合并字数（提示词）",
  fact_merge_max_chars: "事实合并字数（硬上限）",
  fact_merge_soft_reason_chars: "事实合并理由（提示词）",
  fact_merge_reason_chars: "事实合并理由（硬上限）",
  fact_merge_batch_clusters: "事实合并每批簇数",
  fact_merge_prompt: "事实合并提示词",
  cross_session_merge: "跨会话合并（身份类）",
  merge_pending_hide: "合并前不参与检索",
  boot_enabled: "打开面板时播放载入动画",
  boot_replay_seconds: "载入动画重播冷却（秒）",
  session_affinity: "全局召回优先当前会话",
  permanent_dedupe: "自动合并相似永久记忆",
  dedupe_force_merge: "检测到相似就强制合并",
  dedupe_threshold: "永久记忆相似度阈值",
  record_merge_soft_chars: "永久记忆合并字数（提示词）",
  record_merge_max_chars: "永久记忆合并字数（硬上限）",
  record_merge_soft_reason_chars: "永久记忆合并理由（提示词）",
  record_merge_reason_chars: "永久记忆合并理由（硬上限）",
  record_merge_prompt: "永久记忆合并提示词",
  profile_summary_count: "画像摘要条数",
  search_active_only: "检索默认只搜常驻",
  cold_after_days: "归档转入冷归档天数",
  tidy_rebuild_bot_enabled: "允许 Bot 强制重提取单条事实",
  tidy_rebuild_bot_cooldown_minutes: "Bot 强制重提取的间隔（分钟）",
};
let ctx = null,
  tab = "home",
  status = null,
  asset = "",
  config = null,
  configDirty = false,
  offset = 0,
  total = 0,
  factOffset = 0,
  current = null,
  autoRefresh = localStorage.getItem("alife-auto-refresh") !== "off",
  refreshTimer = null,
  models = [];
const esc = (s) =>
  String(s ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const date = (t) =>
  new Date(t * 1000).toLocaleString("zh-CN", { hour12: false });
function toast(t) {
  $("#toast").textContent = t;
  $("#toast").classList.remove("hide");
  setTimeout(() => $("#toast").classList.add("hide"), 3500);
}
function fail(e) {
  const dialog = $("dialog[open]");
  if (dialog) {
    let box = dialog.querySelector(".dialog-error");
    if (!box) {
      box = document.createElement("p");
      box.className = "dialog-error";
      box.setAttribute("role", "alert");
      dialog.prepend(box);
    }
    box.textContent = e.message;
  }
  toast(e.message);
  $("#error").textContent = e.message;
  $("#error").classList.remove("hide");
}
async function api(path, body) {
  const token = window.PluginPageContext?.getToken?.();
  const r = await fetch(
    "/api/plugin/" +
      encodeURIComponent(ctx?.pluginId || "alife_memory_z") +
      path,
    {
      method: body === undefined ? "GET" : "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: {
        "Content-Type": "application/json",
        ...(token ? { Authorization: "Bearer " + token } : {}),
      },
      ...(body === undefined ? {} : { body: JSON.stringify(body) }),
    },
  );
  if (!r.ok) {
    if (r.status === 409) {
      // v2.18.12：文案把"发生了什么 / 你的输入还在 / 两条出路"讲清楚 ✓
      // （旧文案说"重新打开最新版本后再保存" ✗ 照做仍会冲突 ✗ → 是错误指引 ✓）
      const err = Error(
        // ★ 2026-09-27：这条 409 是**通用**的（设置/记忆都会走到这里）
        //   旧文案一开口就说"设置"✗ ⇒ 编辑记忆的人看到会懵 ✓ ⇒ 改成中性表述 ✓
        "这条内容已在别处被改过（另一个页面保存过，或后台整理/插件重载更新过）。" +
          "你这边有未保存的修改，为避免覆盖那边的改动，我没有直接保存 —— 你的输入没有丢。",
      );
      err.status = 409;
      throw err;
    }
    const detail = await r.json().catch(() => ({}));
    throw Error(
      r.status === 422
        ? "输入不符合要求：请检查必填项、范围，以及关系是否表达具体含义。" +
            (typeof detail.detail === "string" &&
            /[\u3400-\u9fff]/.test(detail.detail)
              ? detail.detail
              : "")
        : "操作失败（" + r.status + "），请检查连接或登录状态。",
    );
  }
  return r.json();
}
function empty(t) {
  return (
    '<div class="empty">◇<strong>' +
    esc(t) +
    "</strong>开始对话，或添加一段珍贵的永久记忆。</div>"
  );
}
function saveDraft() {
  try {
    sessionStorage.setItem(
      "alife-draft",
      JSON.stringify({
        tab,
        configDirty,
        config: configDirty ? readConfig() : null,
        configRevision: config?.revision,
        nameDraft: $("#nameEditor").open
          ? {
              row: selectedName,
              name: $("#nameValue").value,
              reason: $("#nameReason").value,
            }
          : null,
        editor: $("#editor").open
          ? {
              current,
              text: $("#editText").value,
              factPatch: factPatch(),
              newSid: $("#newSid")?.value,
            }
          : null,
      }),
    );
  } catch {}
}
function factPatch() {
  const out = {};
  $$("#factFields [data-field]").forEach((e) => {
    out[e.dataset.field] = e.value;
  });
  return out;
}
function renderMigration(m) {
  if (!m) return;
  const names = {
    kira_plugin_simple_memory: "默认记忆",
    kira_plugin_kiraos: "KiraOS",
    kira_plugin_hippocampus_memory: "海马体记忆",
  };
  const reasons = {
    too_long: "超过字符上限",
    empty: "空内容",
    placeholder: "占位内容",
    invalid_text: "文本类型错误",
    invalid_fields: "字段不规范",
    markup_or_serialized_output: "代码/标记/序列化输出",
    control_characters: "控制字符",
    repetition: "重复乱码",
  };
  $("#migrationReport").innerHTML =
    "<p>" +
    esc(
      m.conflicts?.length
        ? "检测到已启用的旧插件；互斥开启时 Alife 暂停，避免争抢记忆。"
        : m.note || "自动迁移未运行",
    ) +
    "</p><p>累计接续 <strong>" +
    m.total_imported +
    "</strong> 条唯一记忆</p>" +
    m.reports
      .map(
        (r) =>
          '<div class="task"><strong>' +
          esc(names[r.source] || r.source) +
          "</strong><span>本次新增 " +
          r.imported +
          " · 已处理 " +
          r.duplicate +
          " · 跳过 " +
          r.skipped +
          " · 待确认归属 " +
          r.unscoped +
          " · 读取错误 " +
          r.errors.length +
          "</span></div>" +
          r.errors
            .map(
              (e) =>
                '<p class="danger">' +
                esc(e.file) +
                " · " +
                esc(e.reason) +
                (e.detail ? " · " + esc(e.detail) : "") +
                "</p>",
            )
            .join(""),
      )
      .join("") +
    m.skips
      .map(
        (r) =>
          "<small>" +
          esc(names[r.source] || r.source) +
          "：" +
          esc(reasons[r.reason] || r.reason) +
          " " +
          r.count +
          " 条</small><br>",
      )
      .join("");
}
function tasksHtml(jobs) {
  return jobs.length
    ? jobs
        .map(
          (j) =>
            '<div class="task"><div><strong>' +
            esc(JOB_KINDS[j.kind] || j.kind) +
            '</strong><div class="muted">' +
            esc(j.sid) +
            "</div><small>" +
            esc(
              j.detail === "TimeoutError"
                ? "旧任务超时：可减小批次、提高超时或更换模型后重新排队。"
                : humanDetail(j.detail) || date(j.created),
            ) +
            '</small></div><div class="actions"><button data-jobdetail="' +
            esc(j.id) +
            '">明细</button></div><span class="state-' +
            esc(j.state) +
            '">' +
            esc(
              {
                queued: "排队中",
                running: "处理中",
                completed: "已完成",
                failed: "失败",
              }[j.state] || j.state,
            ) +
            "</span></div>",
        )
        .join("")
    : empty("暂时没有后台任务");
}
const JOB_KINDS = {
  compress: "分层压缩",
  audit: "事实审计",
  reindex: "语义索引",
  classify: "记忆归类",
  dedupe: "永久记忆合并",
  rewrite: "重整理事实",
  fact_merge: "事实合并",
  tidy: "永久记忆整理",
  proactive: "主动感知",
};
const JOB_ACTIONS = {
  archive: "记忆存档",
  compressed: "已并入存档",
  merged: "并入",
  rewrite: "重做合并",
  keep: "保留",
  correct: "修正",
  merge: "合并",
  retract: "撤回",
  retract_failed: "未撤回",
  classify: "分类",
  keep: "保留",
  extract: "提炼成事实",
  split: "拆分保留",
};
function bindJobButtons() {
  // 注意：只绑明细按钮。手动排队按钮用的是 data-job，混用会把它们的点击覆盖掉。
  // 属性名必须与模板写出的 data-jobdetail 一致（曾写成 dataset.job → 请求 /job/undefined → 404）。
  $$("[data-jobdetail]").forEach(
    (b) => (b.onclick = (e) => {
      e.stopPropagation();
      guard(() => openJob(b.dataset.jobdetail));
    }),
  );
}
// 任务的 detail 有两种形态：**运行中/排队中**是参数 JSON（force/ids ✓），
// **跑完后**是人话结论 ✓。直接显示原始 JSON 对用户没意义 ✗
// （2026-09-18 用户反馈：排队中的「永久记忆整理」卡片显示 {"force": true, "ids": []} ✓）
function humanDetail(detail) {
  if (!detail) return "";
  if (detail[0] !== "{") return detail;      // 已经是人话 ✓ 原样用 ✓
  try {
    const d = JSON.parse(detail);
    const bits = [];
    if (d.force) bits.push("无视冷却");
    if (Array.isArray(d.ids) && d.ids.length) bits.push("指定 " + d.ids.length + " 条");
    if (d.reason) bits.push(String(d.reason));
    return bits.length ? bits.join(" · ") + " · 排队中" : "排队中";
  } catch (e) {
    return detail;      // 解析不了就原样显示 ✓（宁可难看，也别把信息吞掉 ✓）
  }
}

async function openJob(id) {
  if (!id) {
    // 防御：属性名写错时曾经发出 GET /job/undefined 这种请求，只会换来一个 404
    toast("这条任务没有可查看的明细");
    return;
  }
  const data = await api("/job/" + encodeURIComponent(id));
  const names = data.names || {};
  const label = (value) => (names[value] ? names[value] + " · " + value : value);
  const job = data.job;
  $("#jobTitle").textContent = "任务明细 · " + (JOB_KINDS[job.kind] || job.kind);
  $("#jobMeta").textContent =
    [job.sid, humanDetail(job.detail), date(job.created)].filter(Boolean).join(" · ");
  const byTarget = {};
  data.items.forEach((item) => {
    if (item.fact) byTarget[item.fact.id] = item.fact.content;
    else if (item.record) byTarget[item.record.id] = item.record.summary;
  });
  $("#jobItems").innerHTML = data.items.length
    ? data.items
        .map((item, i) => {
          const target = item.record || item.fact;
          const text = item.record
            ? item.record.summary
            : item.fact
              ? item.fact.content
              : "（已不可读取）";
          // 修正 / 并入：把「改前 → 改后」直接摆出来
          const shifted =
            item.action === "merged"
              ? [item.before, byTarget[item.note] || item.note]
              : item.action === "rewrite" && item.before
                ? [item.before, text]
                : item.action === "correct" && item.before && item.before !== text
                  ? [item.before, text]
                  : item.action === "extract" && item.kind === "record" && item.before
                    ? [item.before, "已提炼为事实（原条移出常驻）"]
                    : item.action === "archive" && item.kind === "record" && item.before
                      ? [item.before, "已归档（移出常驻）"]
                      : null;
          const arrow = shifted
            ? '<div class="arrow"><span class="from">' +
              esc(shifted[0]) +
              '</span><i>→</i><strong>' +
              esc(shifted[1]) +
              "</strong></div>"
            : "<strong>" + esc(text || "（空）") + "</strong>";
          const meta = item.record
            ? [
                item.record.permanent ? "永久记忆" : "L" + item.record.level,
                date(item.record.start) + " — " + date(item.record.end),
                label(item.record.sid),
                item.record.users.map(label).join(" · "),
              ]
                .filter(Boolean)
                .join(" · ")
            : item.fact
              ? [
                  label(item.fact.subject),
                  labels[item.fact.category] || item.fact.category,
                  "重要度 " + item.fact.importance,
                ].join(" · ")
              : "";
          return (
            '<div class="task"><div><span class="tag">' +
            esc(JOB_ACTIONS[item.action] || item.action) +
            (item.fact &&
            item.fact.deleted &&
            item.action !== "merged" &&
            item.action !== "rewrite"
              ? '<span class="tag off">已撤回</span>'
              : "") +
            "</span> " +
            arrow +
            '<div class="muted">' +
            esc(meta) +
            (item.note && item.kind === "fact" && item.action !== "merged"
              ? "<br>依据：" + esc(item.note)
              : "") +
            "</div></div>" +
            (target
              ? '<button data-jobitem="' + i + '">查看与编辑</button>'
              : "") +
            "</div>"
          );
        })
        .join("")
    : empty("这次任务没有留下明细");
  $$("[data-jobitem]").forEach(
    (b) =>
      (b.onclick = () =>
        guard(() => {
          const item = data.items[Number(b.dataset.jobitem)];
          $("#jobDialog").close();
          if (item.kind === "fact" && item.fact) return openFact({ id: item.fact.id });
          if (item.record) return openRecord(item.record.id);
        })),
  );
  $("#jobDialog").showModal();
}
$("#closeJob").onclick = () => $("#jobDialog").close();

/* v2.18.7：容量 / 召回用量的标签**写在 index.html 里**（#capTag / #recallTag）✓
   不再运行时 createElement 插入 ✗ —— 那种做法一旦那块 DOM 重渲染就丢 ✓ */

async function poll() {
  try {
    const next = await api("/status");
    // v2.18.7：L0 容量 / 召回用量 —— 元素**写在 index.html 里** ✓ 这里只更新文字 ✓
    // 原来用 createElement 插到 #searchIndex 旁 ✗ 是运行时插入 ✓ 一旦该区域被重渲染就丢 ✓
    // 更关键：capacity_stats 拿连接的方式在本项目里不存在 ✗ → 恒返回 {} → 永远「L0 容量 —」✓
    const cap = next.capacity || {};
    const levels = cap.levels || {};
    const years = cap.years || {};
    const total = Object.values(levels).reduce((sum, n) => sum + (n || 0), 0);
    const capParts = [];
    if (cap.db_bytes) capParts.push("库 " + (cap.db_bytes / 1048576).toFixed(1) + " MB");
    if (total) capParts.push("记录 " + total);
    if (cap.fts_rows) capParts.push("索引 " + cap.fts_rows);
    const ys = Object.keys(years).sort().slice(-2);
    if (ys.length) capParts.push(ys.map((y) => y.slice(2) + "年 " + years[y]).join(" · "));
    const capEl = $("#capTag");
    if (capEl) capEl.textContent = capParts.length ? "L0 " + capParts.join(" | ") : "L0 容量 —";
    const u = next.recall_usage || {};
    const recallEl = $("#recallTag");
    if (recallEl) {
      recallEl.textContent = u.total_calls
        ? "召回 " + u.total_calls + " 次 | " + ((u.total_chars || 0) / 1024).toFixed(1) + "k 字符"
        : "召回 —";
    }
    const conn = next.enabled
      ? autoRefresh
        ? "已连接 · 实时同步"
        : "已连接 · 手动同步"
      : "已连接 · 已暂停";
    // 带上版本与资源指纹：一眼能看出前端是不是旧版
    // （旧版页面缺新功能时，先看这里对不对得上插件版本）
    if (next.search_index) {
      const labels = {
        ready: "索引 已启用",
        building: "索引 回填中",
        unavailable: "索引 不可用（全表检索）",
      };
      const badge = $("#searchIndex");
      if (badge) badge.textContent = labels[next.search_index] || "索引 —";
    }
    // ★ 2026-09-29：JEV 状态小灯（就绪 / 熔断中剩余秒 / 未就绪原因 ✓）
    //   后端 /status 的 jev 字段是**只读快照** ✓ 这里只渲染 ✗ 不触发任何调用 ✓
    const jv = next.jev || {};
    const jevEl = $("#jevTag");
    if (jevEl) {
      if (!jv.enabled) {
        jevEl.classList.add("hide");
      } else {
        jevEl.classList.remove("hide");
        const cd = Number(jv.cooldown_left || 0);
        if (jv.ready) {
          jevEl.textContent = "JEV 就绪" + (jv.model ? " | " + jv.model : "");
        } else if (cd > 0) {
          jevEl.textContent = "JEV 熔断中 | 剩余 " + cd + "s";
        } else {
          jevEl.textContent = "JEV 未就绪";
        }
        jevEl.title = [
          jv.why || "",
          jv.last_error ? "上次失败：" + jv.last_error : "",
          "就绪=可用 ✓ 熔断中=连续失败被冷却（期间一律走原逻辑 ✓）",
        ].filter(Boolean).join(" ｜ ");
      }
    }
    $("#connection").textContent = next.version
      ? conn + " · v" + next.version
      : conn;
    if (asset && asset !== next.assets) {
      // 插件文件变了：带版本参数跳转，绕开 WebView 的静态缓存
      // （普通 reload() 会把缓存的 index.html/app.js 再拿一遍，于是"看不到新按钮"）
      saveDraft();
      const url = location.pathname + "?v=" + encodeURIComponent(next.assets);
      location.replace(url);
      return;
    }
    asset = next.assets;
    const changed = !status || next.revision !== status.revision;
    status = next;
    if (next.boot) {
      bootConfig = {
        enabled: next.boot.enabled !== false,
        replay_seconds: Number(next.boot.replay_seconds) || 0,
      };
      localStorage.setItem("alife-boot", JSON.stringify(bootConfig));
      if (!bootConfig.enabled && bootPlaying) endBoot();
    }
    renderMigration(next.migration);
    renderBootstrapNotice(next.bootstrap_review);
    $$('[data-job="reindex"]').forEach((e) => {
      e.disabled = !next.semantic_enabled;
      e.title = next.semantic_enabled
        ? "将调用所选向量模型，可能计费"
        : "向量检索默认关闭；本地检索仍可使用";
    });
    $("#stats").innerHTML = [
      ["所有存档", next.records, "含完整原始记录"],
      ["归类事实", next.facts, "每条均有来源"],
      ["已知实体", next.users, "画像与关系的起点"],
      [
        "后台运行",
        next.jobs.filter((j) => j.state === "running").length,
        "压缩 / 审计 / 索引",
      ],
    ]
      .map(
        (x) =>
          '<div class="stat"><span class="muted">' +
          x[0] +
          "</span><b>" +
          x[1] +
          "</b><small>" +
          x[2] +
          "</small></div>",
      )
      .join("");
    const max = Math.max(1, ...next.levels.map((l) => l.count));
    $("#levels").innerHTML = next.levels.length
      ? next.levels
          .map(
            (l) =>
              '<div class="level"><strong>' +
              (l.level === 100 ? "永久" : "L" + l.level) +
              '</strong><div class="bar"><i style="width:' +
              Math.max(3, (100 * l.count) / max) +
              '%"></i></div><small>' +
              l.active +
              " 活跃记忆 / " +
              l.count +
              "</small></div>",
          )
          .join("")
      : empty("记忆空间已准备好");
    $("#recent").innerHTML = tasksHtml(next.jobs.slice(0, 3));
    bindJobButtons();
    $("#jobs").innerHTML = tasksHtml(next.jobs);
    bindJobButtons();
    for (const id of ["session", "jobSession"]) {
      const e = $("#" + id),
        value = e.value;
      e.innerHTML =
        (id === "session" ? '<option value="">全部会话</option>' : "") +
        next.sessions
          .map(
            (s) =>
              '<option value="' +
              esc(s) +
              '">' +
              esc(
                next.session_names?.[s] ? next.session_names[s] + " · " + s : s,
              ) +
              "</option>",
          )
          .join("");
      if ([...e.options].some((o) => o.value === value)) e.value = value;
    }
    if (changed && !$("#editor").open) {
      if (tab === "archives") await loadArchives();
      if (tab === "profiles") await loadFacts();
      if (tab === "names" && !$("#nameEditor").open) await loadNames();
    }
    if (config && next.config_revision !== config.revision && !configDirty)
      await loadConfig();
  } catch (e) {
    $("#connection").textContent = "连接中断 · 自动重试";
    fail(e);
  }
}
async function selectTab(name) {
  tab = name;
  if (name !== "profiles") {
    resetGraphZoom(document.querySelector("#relations"));
    clearGraphFocus();
  }
  $$(".view").forEach((e) => e.classList.toggle("hide", e.id !== name));
  $$("[data-tab]").forEach((e) =>
    e.setAttribute("aria-current", e.dataset.tab === name ? "page" : "false"),
  );
  const titles = {
    home: ["记忆，在这里生长", "保留经历的细节，也记住彼此的联结。"],
    archives: ["每段经历，都有来处", "检索、编辑，或沿着存档逐层回到最初。"],
    profiles: ["记住你，也认识自己", "关系、偏好、约定，汇成有依据的画像。"],
    tasks: ["让记忆，慢慢沉淀", "在后台压缩、审计与合并，不打断当下的对话。"],
    trash: ["离开上下文的，也还在这里", "回收站与冷归档：还原、查看或编辑。"],
    names: ["名字会改变，彼此仍相识", "以稳定ID连接现在与曾经的称呼。"],
    health: ["谁会先被下沉", "分数、阈值与上浮下沉的理由，一目了然。"],
    settings: ["按你的节奏，整理记忆", "每一项配置，保存即生效。"],
  };
  $("#title").textContent = titles[name][0];
  $("#subtitle").textContent = titles[name][1];
  if (name === "names") {
    await loadNames();
    await maybeAskNameBatch();
  }
  if (name === "archives") await loadArchives();
  if (name === "profiles") await loadFacts();
  if (name === "trash") await loadTrash();
  if (name === "settings" && !configDirty) await loadConfig();
  saveDraft();
}
// v2.18.19：给档案浏览加一个"显示工具步"开关 ✓（默认关 ✓）
// bot 主被动召回**永远**看不到工具步 ✗ —— 这里只是让**你**能翻出来看 ✓
function ensureToolToggle() {
  if (document.querySelector("#includeTools")) return;
  const anchor = document.querySelector("#includeGlobal");
  const box = anchor && anchor.closest("label");
  if (!box) return;
  const wrap = document.createElement("label");
  wrap.className = box.className;
  wrap.innerHTML =
    '<input type="checkbox" id="includeTools"> <span>显示工具步</span>';
  box.insertAdjacentElement("afterend", wrap);
  wrap.querySelector("input").addEventListener("change", () => loadArchives());
}
// v2.18.64：再给档案浏览加一个"显示历史存档"开关 ✓（**默认开** ✓）
// 它负责显示/隐藏「历史存档」与「冷归档 · 仅按ID可读」这两类卡片 ✓
function ensureHistoryToggle() {
  if (document.querySelector("#includeHistory")) return;
  const anchor = document.querySelector("#includeTools");
  const box = anchor && anchor.closest("label");
  if (!box) return;
  const wrap = document.createElement("label");
  wrap.className = box.className;
  wrap.innerHTML =
    '<input type="checkbox" id="includeHistory" checked> <span>显示历史存档</span>';
  box.insertAdjacentElement("afterend", wrap);
  wrap.querySelector("input").addEventListener("change", () => {
    offset = 0;
    loadArchives();
  });
}
async function loadArchives() {
  ensureToolToggle();
  ensureHistoryToggle();
  const selectedSid = $("#session").value;
  const q = {
    sid: selectedSid,
    include_global: $("#includeGlobal").checked,
    include_tools: !!(document.querySelector("#includeTools") || {}).checked,
    // v2.18.64：默认**显示**历史存档 ✓（元素尚未创建时也按 true 走 ✓）
    include_history: (
      document.querySelector("#includeHistory") || { checked: true }
    ).checked,
    keyword: $("#keyword").value,
    prompt: $("#semantic").value,
    offset,
    limit: 20,
  };
  if ($("#level").value !== "") q.level = Number($("#level").value);
  if ($("#after").value) q.start = new Date($("#after").value).getTime() / 1000;
  if ($("#before").value) q.end = new Date($("#before").value).getTime() / 1000;
  const data = await api("/search", q);
  total = data.total;
  Object.assign(displayNames, data.names || {});
  $("#archiveCards").innerHTML = data.items.length
    ? data.items
        .map(
          (r) =>
            '<article class="card"><div class="row"><span class="tag">' +
            (r.permanent ? "永久记忆" : "L" + r.level) +
            "</span>" +
            // ⚠️ 别在这里再加"状态类"标签 ✗✓ —— 卡片 footer 已经在显示它了 ✓
            // （冷归档 · 仅按ID可读 / 活跃记忆 / 历史存档 ✓ 既有措辞 ✓ 别再发明同义词 ✗）
            (selectedSid && r.sid !== selectedSid
              ? '<span class="tag">跨会话</span>'
              : "") +
            "<small>" +
            date(r.end) +
            "</small></div><p>" +
            // v2.18.65：不能只靠 summary ✗（摘要为空的记录卡片会是一片空白 ✓
            // 实测 /search 同时返回 content ⇒ 回退即可）
            esc(r.summary || r.content || r.preview || "") +
            '</p><div class="meta">' +
            esc(displayLabel(r.sid)) +
            "<br>" +
            esc(r.users.map(displayLabel).join(" · ")) +
            "</div><footer><small>" +
            (r.cold
              ? "冷归档 · 仅按ID可读"
              : r.active
                ? "活跃记忆"
                : "历史存档") +
            '</small><button data-open="' +
            esc(r.id) +
            '">查看与编辑 ↗</button></footer></article>',
        )
        .join("")
    : empty("没有找到这段记忆");
  $$("[data-open]").forEach(
    (e) => (e.onclick = () => guard(() => openRecord(e.dataset.open))),
  );
  $("#pageInfo").textContent =
    (total ? offset + 1 : 0) +
    "–" +
    Math.min(offset + 20, total) +
    " / " +
    total;
  $("#prev").disabled = offset === 0;
  $("#next").disabled = offset + 20 >= total;
}
let trashKind = "facts";
let trashOffset = 0;
const TRASH_KINDS = {
  facts: { button: "#trashFacts", cards: "#trashCards" },
  records: { button: "#trashRecords", cards: "#trashCards" },
  cold: { button: "#trashCold", cards: "#trashCards" },
};
function setTrashKind(kind) {
  trashKind = kind;
  trashOffset = 0;
  Object.entries(TRASH_KINDS).forEach(([key, value]) =>
    $(value.button).setAttribute("aria-pressed", String(key === kind)),
  );
  $("#trashKeyword").placeholder =
    kind === "facts" ? "按事实内容搜索…" : "按存档摘要搜索…";
}
async function loadTrash() {
  const q = new URLSearchParams({
    kind: trashKind,
    keyword: $("#trashKeyword").value,
    offset: trashOffset,
  });
  const data = await api("/trash?" + q);
  Object.assign(displayNames, data.names || {});
  // v2.18.65：把"什么时候算冷归档"讲清楚（以前靠猜 ✗）
  const totals = data.totals || {};
  $("#trashFacts").textContent = "事实" + (totals.facts ? " " + totals.facts : "");
  $("#trashRecords").textContent =
    "存档（已删除）" + (totals.records ? " " + totals.records : "");
  $("#trashCold").textContent =
    "冷归档（仍生效）" + (totals.cold ? " " + totals.cold : "");
  const note = $("#trashNote");
  if (note)
    note.textContent =
      {
        facts: "被审计撤回或合并掉的事实；「还原」后会重新参与检索。",
        records:
          "这些记录已经删掉（离开上下文），原文还在库里；点「还原到上下文」就回到检索。",
        cold: "这些记录没有被删，只是正文搬去了独立冷库；「查看与编辑」照常能读全文，召回也不受影响。",
      }[trashKind] || "";
  // 冷归档那四个按钮是**全局操作** ⇒ 只在冷归档页签时显示（以前常驻 ⇒ 看着像属于当前列表 ✗）
  const coldTools = $("#coldTools");
  if (coldTools) coldTools.classList.toggle("hide", trashKind !== "cold");
  $("#trashCards").innerHTML = data.items.length
    ? data.items
        .map((row, i) => {
          const facts = trashKind === "facts";
          const cold = trashKind === "cold";
          const tag = facts
            ? labels[row.category] || row.category
            : cold
              ? "冷归档"
              : row.permanent
                ? "永久记忆"
                : "L" + row.level;
          const head = facts
            ? esc(displayLabel(row.subject))
            : esc(displayLabel(row.sid));
          const body = facts
            ? row.content
            : row.content || row.summary || row.preview;
          // v2.18.65：状态词统一（以前"存档"页签的卡片写着"历史存档"✗ 看不出区别）
          const status = facts
            ? null
            : cold
              ? "冷归档 · 正文在冷库"
              : row.deleted
                ? "已删除 · 可还原"
                : "历史存档";
          const meta = [
            facts ? "重要度 " + row.importance : null,
            facts ? esc(row.reason ? "依据：" + row.reason : "") : null,
            status,
            date(row.start || row.created),
            row.removed_at ? "离开上下文 " + date(row.removed_at) : null,
          ]
            .filter(Boolean)
            .join(" · ");
          return (
            '<article class="card"><div class="row"><span class="tag">' +
            esc(tag) +
            '</span><strong>' +
            head +
            "</strong></div><p>" +
            esc(body) +
            '</p><p class="muted">' +
            meta +
            "</p><footer>" +
            (cold
              ? '<button class="primary" data-trashrestore="' +
                i +
                '">取回上下文</button>'
              : '<button class="primary" data-trashrestore="' +
                i +
                '">还原到上下文</button>') +
            '<button data-trashtarget="' +
            i +
            '">查看与编辑</button>' +
            '<button class="danger" data-trashpurge="' +
            i +
            '">彻底删除</button></footer></article>'
          );
        })
        .join("")
    : empty("回收站是空的");
  $$("[data-trashrestore]").forEach(
    (b) =>
      (b.onclick = () =>
        guard(async () => {
          const row = data.items[Number(b.dataset.trashrestore)];
          await api("/trash/restore", {
            kind:
              trashKind === "facts"
                ? "fact"
                : trashKind === "cold"
                  ? "cold"
                  : "record",
            target: row.id,
          });
          toast(trashKind === "cold" ? "已取回上下文" : "已从回收站还原");
          await loadTrash();
        })),
  );
  $$("[data-trashtarget]").forEach(
    (b) =>
      (b.onclick = () =>
        guard(() => {
          const row = data.items[Number(b.dataset.trashtarget)];
          return trashKind === "facts"
            ? openFact({ id: row.id })
            : openRecord(row.id);
        })),
  );
  $$("[data-trashpurge]").forEach(
    (b) =>
      (b.onclick = () =>
        guard(async () => {
          const row = data.items[Number(b.dataset.trashpurge)];
          const kind = trashKind === "facts" ? "fact" : "record";
          const summary =
            trashKind === "facts"
              ? row.content
              : row.content || row.summary || row.preview;
          await askPurge(kind, row.id, summary);
        })),
  );
  $("#trashPage").textContent =
    (data.total ? trashOffset + 1 : 0) +
    "–" +
    Math.min(trashOffset + 50, data.total) +
    " / " +
    data.total;
  $("#trashPrev").disabled = trashOffset === 0;
  $("#trashNext").disabled = trashOffset + 50 >= data.total;
}
for (const [key, value] of Object.entries(TRASH_KINDS))
  $(value.button).onclick = () =>
    guard(() => {
      setTrashKind(key);
      return loadTrash();
    });
$("#trashSearch").onclick = () =>
  guard(() => {
    trashOffset = 0;
    return loadTrash();
  });
$("#trashKeyword").onkeydown = (e) => {
  if (e.key === "Enter") $("#trashSearch").click();
};
$("#trashPrev").onclick = () =>
  guard(() => {
    trashOffset = Math.max(0, trashOffset - 50);
    return loadTrash();
  });
$("#trashNext").onclick = () =>
  guard(() => {
    trashOffset += 50;
    return loadTrash();
  });

function askPurge(kind, target, summary) {
  return new Promise((resolve) => {
    const dialog = $("#purgeDialog");
    $("#purgeTitle").textContent = "彻底删除？";
    $("#purgeBody").textContent = summary || target;
    $("#purgeNext").classList.remove("hide");
    $("#purgeConfirm").classList.add("hide");
    $("#purgeNext").onclick = () => {
      // 第二次确认：这一步才真正执行
      $("#purgeTitle").textContent = "最后确认：不可撤销";
      $("#purgeNext").classList.add("hide");
      $("#purgeConfirm").classList.remove("hide");
    };
    $("#purgeCancel").onclick = () => {
      dialog.close();
      resolve(false);
    };
    $("#purgeConfirm").onclick = () =>
      guard(async () => {
        await api("/trash/purge", { kind, target });
        dialog.close();
        toast("已彻底删除");
        resolve(true);
        await loadTrash();
      });
    dialog.showModal();
  });
}

/* ★ 2026-09-18：补上体检页渲染 ✓（含用户反馈的两点修复 ✓）
   ① 卡死：原来一次渲染**全部 300 条** ✗ 每次点 ± / 删除后都要重建 300 张卡 ⇒ 浏览器未响应
      ⇒ 改为**分页 50 条**（与「记忆存档」页一致 ✓）
   ② 信息太少：原来只有 重要度/分数/年龄 + 删除 ✗ 用户不好判断
      ⇒ 逐字对齐**正常事实卡片**的结构（类别 tag / 显示名 / 内容 / 元信息）
        并**点击卡片 = 打开编辑器**（= openFact ✓ 可改内容/重要度/标签/删除 ✓）
   后端接口：GET /fact_health ✓ → {threshold, count, now, rows:[{id,sid,subject,content,
   category,importance,rotate_used,age_days,score,sunk,never_sink}]} ✓ */
const HEALTH_PAGE = 50;
let healthRows = [];
let healthPage = 0;
let healthTotal = 0;

async function loadHealth(page) {
  const meta = $("#healthMeta"), box = $("#healthList");
  if (!meta || !box) return;
  const p = Math.max(0, Number(page) || 0);
  meta.textContent = "正在加载…";
  // ★ 2026-09-19（用户实测）：先点体检页时卡片只显示 `qq:2141951927` ✗
  //   根因：昵称表 displayNames 是**懒加载**的，只有"名字页/关系页"会补载 ✓
  //   ⇒ 这里先补一次 ✓（静默 ✓ 拿不到也不影响主流程 ✓）
  //   ★★ 2026-09-19 二改：必须给**超时上限** ✗ —— ensureNames 有可能卡住不发请求
  //      实测症状就是：整页永远停在"正在加载…"，切到别的页再切回来才出内容 ✓
  await Promise.race([
    Promise.resolve().then(() => ensureNames()).catch(() => {}),
    new Promise((r) => setTimeout(r, 1500)),
  ]);
  let d;
  try {
    // ★ 服务端分页：只拉**本页 50 条**（原来一次 300 行 = 189.5 KB ✗ 用户实测"加载太久"）
    d = await api("/fact_health?limit=" + HEALTH_PAGE + "&offset=" + p * HEALTH_PAGE);
  } catch (e) {
    meta.textContent = "加载失败：" + e.message;
    return;
  }
  healthRows = (d && d.rows) || [];
  healthTotal = d && d.count != null ? d.count : healthRows.length;
  healthPage = p;
  const thr = d && d.threshold != null ? d.threshold : 12;
  const sunk = healthRows.filter((f) => f.sunk).length;
  meta.textContent =
    "共 " + healthTotal + " 条事实；阈值 " + thr + "，本页 " + sunk + " 条会下沉。" +
    "分数低的排在前面；点卡片即可编辑（内容 / 重要度 / 标签 / 删除），重要度 ±1 是上浮手段。" +
    "每页 " + HEALTH_PAGE + " 条。";
  paintHealth();
}

function paintHealth() {
  const box = $("#healthList");
  if (!box) return;
  const pages = Math.max(1, Math.ceil(healthTotal / HEALTH_PAGE));
  const start = healthPage * HEALTH_PAGE;
  box.innerHTML = healthRows.length
    ? healthRows.map((f, i) => healthCard(f, start + i)).join("") +
      '<div class="pager"><button data-hpage="prev">上一页</button>' +
      "<small>" + (healthPage + 1) + " / " + pages + "</small>" +
      '<button data-hpage="next">下一页</button></div>'
    : '<p class="muted">暂无事实 ✓</p>';
  $$("#healthList [data-hidx]").forEach((e) => {
    e.onclick = (ev) => {
      if (ev.target.closest("button")) return;
      const row = healthRows[Number(e.dataset.hidx) - start];
      if (row) guard(() => openFact(row));
    };
  });
  $$("#healthList [data-hpage]").forEach((e) => {
    e.onclick = () => guard(() => loadHealth(healthPage + (e.dataset.hpage === "next" ? 1 : -1)));
  });
}

function healthCard(f, idx) {
  // ★ 与「事实卡片」显示**完全一样的东西** ✓ 只多出本页特有的：
  //   分数 / 被用次数 / 年龄 / 本轮下沉·永不沉 标签 / 重要度 ±1 快捷上浮 ✓
  const imp = f.importance != null ? f.importance : 5;
  // ★ 2026-09-23：被用次数按 60 天半衰期衰减 ⇒ 顺带把"最近被用"标出来 ✓
  const usedAgo = f.rotate_used_at
    ? "（最近 " + Math.max(0, Math.round((Date.now() / 1000 - f.rotate_used_at) / 86400)) + " 天前）"
    : "";
  // ★ 2026-09-19（用户）：拿不到时间就**什么都不显示** ✓
  //   不要写"时间未知"这种占位 ✓（占地方又没信息）
  const age = f.age_days != null ? f.age_days + " 天前" : "";
  const sinkTags =
    (f.sunk ? '<span class="tag">本轮下沉</span>' : "") +
    (f.never_sink ? '<span class="tag">永不沉</span>' : "");
  const cat = f.category != null ? labels[f.category] || f.category : "其他";
  const tagLine = (f.tags || []).join(" · ");
  const notice =
    (f.rewrite_pending
      ? '<div class="notice">待整理 ' + esc(String(f.rewrite_attempts || 0)) +
        "/3：这条是「模型输出不可用 → 按时间拼接」的产物。下一轮审计会还原来源、重新合并；" +
        '也可以点维护面板的「重整理待处理事实」立刻排队。</div>'
      : "") +
    ((f.relation_warnings || []).length
      ? '<div class="notice">待审校：' +
        esc((f.relation_warnings || []).map((w) => w.reason).join("；")) +
        "。该连线未用于关系召回。</div>"
      : "");
  const reason = (f.reason ? "事实依据：" + f.reason : "") +
    (f.scenario ? " · 场景：" + f.scenario : "");
  const hist = (f.edit_history || []).length
    ? '<p class="muted">最近审校：' + esc(f.edit_history[0].reason) + " · " +
      date(f.edit_history[0].created) + "</p>"
    : "";
  return (
    '<article class="card" data-hidx="' + idx + '" title="点击编辑这条事实">' +
    '<div class="row"><span class="tag">' + esc(cat) + "</span><strong>" +
    esc(displayLabel(f.subject)) + "</strong></div>" +
    "<p>" + esc(f.content) + "</p>" +
    (tagLine ? "<small>" + esc(tagLine) + "</small>" : "") +
    notice +
    (reason ? '<p class="muted">' + esc(reason) + "</p>" : "") +
    hist +
    "<small>重要度 " + imp + " · 分数 " + f.score + " · 被用 " + (f.rotate_used || 0) +
    " 次" + usedAgo + (age ? " · " + age : "") + " " + sinkTags + "</small>" +
    "<footer><small>" + ((f.sources || []).length) + " 个来源</small>" +
    '<button data-imp="' + f.id + '" data-delta="-1">－</button>' +
    '<button data-imp="' + f.id + '" data-delta="1">＋</button>' +
    '<button data-del="' + f.id + '">删除</button>' +
    '<button data-hidx="' + idx + '">编辑事实</button>' +
    "</footer></article>"
  );
}

async function loadFacts() {
  const byContent = $("#factContent").checked;
  const q = new URLSearchParams({
    subject: byContent ? "" : $("#subject").value,
    keyword: byContent ? $("#subject").value : "",
    category: $("#category").value,
    offset: factOffset,
  });
  shownFacts = await api("/facts?" + q);
  for (const f of shownFacts) Object.assign(displayNames, f.names || {});
  renderFactCards(shownFacts);
  renderGraph();
  $("#factPage").textContent = "第 " + (factOffset / 100 + 1) + " 页";
  $("#factPrev").disabled = factOffset === 0;
  $("#factNext").disabled = shownFacts.length < 100;
}
function renderFactCards(list) {
  $("#factCards").innerHTML = list.length
    ? list
        .map(
          (f, i) =>
            `<article class="card"><div class="row"><span class="tag">${esc(labels[f.category])}</span><strong>${esc(displayLabel(f.subject))}</strong></div><p>${esc(f.content)}</p><small>${esc((f.tags || []).join(" · "))}</small>${f.rewrite_pending ? '<div class="notice">待整理 ' + esc(String(f.rewrite_attempts || 0)) + '/3：这条是「模型输出不可用 → 按时间拼接」的产物。下一轮审计会还原来源、重新合并；也可以点维护面板的「重整理待处理事实」立刻排队。</div>' : ""}${f.relation_warnings?.length ? '<div class="notice">待审校：' + esc(f.relation_warnings.map((w) => w.reason).join("；")) + "。该连线未用于关系召回。</div>" : ""}<p class="muted">${esc(f.reason ? "事实依据：" + f.reason : "")}${esc(f.scenario ? " · 场景：" + f.scenario : "")}</p>${f.edit_history?.length ? '<p class="muted">最近审校：' + esc(f.edit_history[0].reason) + " · " + date(f.edit_history[0].created) + "</p>" : ""}<footer><small>${(f.sources || []).length} 个来源</small><button data-fact="${i}">编辑事实</button></footer></article>`,
        )
        .join("")
    : empty("画像还在形成");
  $$("[data-fact]").forEach(
    (e) =>
      (e.onclick = () => guard(() => openFact(list[Number(e.dataset.fact)]))),
  );
}
function showEditor() {
  $$(".dialog-error").forEach((e) => e.remove());
  $("#deleteConfirm").classList.add("hide");
  if (!$("#editor").open) $("#editor").showModal();
  $("#editText").focus();
}
async function openRecord(id) {
  current = {
    kind: "record",
    row: await api("/memory/" + encodeURIComponent(id)),
  };
  renderRecord();
  showEditor();
}
function renderRecord() {
  const r = current.row;
  $("#editorTitle").textContent =
    "记忆详情 · " + (r.permanent ? "永久" : "L" + r.level);
  $("#editorMeta").textContent =
    displayLabel(r.sid) +
    " · " +
    r.users.map(displayLabel).join(" / ") +
    " · " +
    date(r.start) +
    " — " +
    date(r.end);
  $("#editLabel").textContent = "可编辑摘要（原文保留：只剥掉协议外壳与思考块）";
  $("#editText").value = r.summary;
  // v2.18.65：没有摘要的记录 ⇒ 给一句说明（只改提示 ✓ **不动数据** ✓）
  // 不拿 content 预填 ✗ —— 那会让"保存"把原文误写成摘要 ✗
  $("#editText").placeholder = r.summary
    ? ""
    : "这条没有摘要（卡片与列表会自动显示原文）…";
  $("#factFields").classList.add("hide");
  $("#sources").classList.remove("hide");
  $("#sourceText").textContent =
    r.content +
    "\n\n旧插件来源\n" +
    JSON.stringify(r.legacy_sources || [], null, 2);
  // ★ 2026-09-29（用户实测）：记录详情此前**不管理** #delete 显隐 ✗
  //   ⇒ 从「新增记忆」流程后进记录详情，可能没有删除按钮 ✗
  //   （openFact/任务明细是 remove ✓ newMemory 是 hide ✓ 这里补齐同一套 ✓）
  $("#delete").classList.remove("hide");
  renderVersions("record", r.id, r.revision, r.versions || []);
  $("#sourceLinks").innerHTML = r.children
    .map(
      (id) =>
        '<button data-child="' +
        esc(id) +
        '">读取子存档 ' +
        esc(id.slice(0, 14)) +
        "…</button>",
    )
    .join("");
  $$("[data-child]").forEach(
    (e) => (e.onclick = () => guard(() => openRecord(e.dataset.child))),
  );
  $("#forget").classList.toggle("hide", !r.permanent);
  $("#forget").textContent = r.active ? "移出活跃记忆" : "恢复到活跃记忆";
  // v2.18.19：**常驻的永久记忆**多给一个按钮 ✓ —— 无视 14 天冷却，只对这一条重跑整理 ✓
  // 用途：用户觉得不准、或想再提取一次事实 ✓（整理动作里含 extract=用 facts 提炼 ✓）
  // 按钮**动态创建** ✗ 不动 HTML ✓（避免改 HTML 结构 ✓ 与工具步开关同一套做法 ✓）
  let re = $("#reextract");
  if (!re && $("#forget")) {
    re = document.createElement("button");
    re.id = "reextract";
    re.className = "quiet";
    re.textContent = "重新提取事实";
    $("#forget").insertAdjacentElement("afterend", re);
  }
  // ★ 2026-09-19（用户要求）：同风格弹窗 ⇒ 让用户选"按规则"还是"完全重新提取"
  //   完全重新提取 = 本次**不允许 keep** ⇒ 一定会给出动作（不会再"点了没反应"）
  //   用仓库既有的 <dialog class="dialog"> + 既有 class ✓ 保持风格一致
  const askReextractMode = () =>
    new Promise((resolve) => {
      const d = document.createElement("dialog");
      d.className = "dialog";
      d.innerHTML =
        '<div class="panel-title"><h2>重新提取事实</h2>' +
        '<button id="reClose" aria-label="关闭">×</button></div>' +
        '<p class="muted">这会针对这一条常驻记忆重新核对它与原文的关系。</p>' +
        '<label class="field tidy-opt"><input type="radio" name="remode" value="rule">' +
        '<span>按规则整理<small class="muted">模型可以判定"这条不用动"，token 花得更少</small></span></label>' +
        '<label class="field tidy-opt"><input type="radio" name="remode" value="rebuild" checked>' +
        '<span>完全重新提取<small class="muted">本次不允许保留原样，一定会给出处理动作；token 花得更多。这条一定会离开活跃记忆（信息会落成事实保留）；想让它继续活跃请选「按规则整理」。</small></span></label>' +
        '<div class="actions"><button id="reCancel">取消</button>' +
        '<button id="reOk" class="primary">开始</button></div>';
      document.body.appendChild(d);
      const done = (v) => {
        d.close();
        d.remove();
        resolve(v);
      };
      d.querySelector("#reClose").onclick = () => done(null);
      d.querySelector("#reCancel").onclick = () => done(null);
      d.querySelector("#reOk").onclick = () => {
        const picked = d.querySelector('input[name="remode"]:checked');
        done(picked ? picked.value : "rebuild");
      };
      d.addEventListener("cancel", (ev) => {
        ev.preventDefault();
        done(null);
      });
      d.showModal();
    });

  if (re) {
    // ★ 2026-09-19（用户实测）：冷归档不参与整理（引擎侧 cold=0 才可整理 ✓）
    //   按钮留着只会点了得到"本次跳过"✗ ⇒ 冷归档时一并隐藏 ✓（卡片自带「冷归档 · 仅按ID可读」= 说明 ✓）
    re.classList.toggle("hide", !r.permanent || !!r.cold);
    re.onclick = () =>
      guard(async () => {
        const mode = await askReextractMode();
        if (!mode) return;
        await api("/jobs", {
          kind: "tidy",
          sid: r.sid,
          force: true,                 // 无视冷却 ✓
          ids: [r.id],                 // 只这一条 ✓
          rebuild: mode === "rebuild", // 完全重新提取 ⇒ 本次不允许 keep ✓
        });
        toast(
          mode === "rebuild"
            ? "已开始完全重新提取（这条会离开活跃记忆，信息落成事实保留）· 稍后看任务明细"
            : "已开始重新提取（按规则）· 稍后看任务明细",
        );
        // ★ 用户要求：确认后**不要停留在编辑界面** ⇒ 关弹窗 + 回永久记忆列表并刷新 ✓
        $("#editor").close();
        await poll();
        // 与"保存后"同一套约定 ✓（tab 为模块级变量 ✓ 与既有代码一致 ✓）
        if (tab === "archives") await loadArchives();
        if (tab === "profiles") await loadFacts();
      });
  }
  $("#delete").classList.remove("hide");
}
async function openFact(row) {
  const detail = row.versions
    ? row
    : await api("/fact/" + encodeURIComponent(row.id));
  row = detail;
  current = { kind: "fact", row };
  $("#editorTitle").textContent = "编辑画像事实";
  $("#editorMeta").textContent = row.subject + " · " + row.sid;
  $$("#factFields .revoked").forEach((e) => e.remove());
  if (row.deleted) {
    const hint = document.createElement("p");
    hint.className = "notice revoked";
    hint.textContent =
      "这条事实已不在上下文中（被审计撤回或合并掉了）。可在下方历史版本里点「恢复此版本」还原。";
    $("#factFields").prepend(hint);
  }
  $("#editLabel").textContent = "事实内容";
  $("#editText").value = row.content;
  // ★ 2026-09-29（用户实测）：#reextract 由 renderRecord **动态创建** ⇒ openFact/newMemory
  //   此前完全不碰它 ✗ ⇒ 先开过常驻永久记录、再看事实/新增，按钮会残留，
  //   且它的 onclick 仍是 record 分支的闭包 ⇒ 点了会发错 job ✗（这里统一收起 ✓）
  const reextractEl = $("#reextract");
  if (reextractEl) reextractEl.classList.add("hide");
  $("#factFields").classList.remove("hide");
  $("#factFields").innerHTML = [
    "subject",
    "category",
    "reason",
    "scenario",
    "tags",
    "relations",
  ]
    .map(
      (k) =>
        '<label class="field">' +
        esc(
          {
            subject: "实体",
            category: "分类",
            reason: "原因",
            scenario: "适用场景",
            tags: "标签（每行一个）",
            relations: "关系（三元组 JSON）",
          }[k],
        ) +
        (k === "category"
          ? '<select data-field="category">' +
            Object.entries(labels)
              .map(
                ([v, n]) =>
                  '<option value="' +
                  v +
                  '" ' +
                  (row.category === v ? "selected" : "") +
                  ">" +
                  n +
                  "</option>",
              )
              .join("") +
            "</select>"
          : '<textarea data-field="' +
            k +
            '">' +
            esc(
              k === "relations"
                ? JSON.stringify(row[k], null, 2)
                : Array.isArray(row[k])
                  ? row[k].join("\n")
                  : row[k],
            ) +
            "</textarea>") +
        "</label>",
    )
    .join("");
  $("#factFields").insertAdjacentHTML(
    "beforeend",
    '<small class="wide">关系示例：[{"subject":"qq:123","predicate":"朋友","object":"qq:456"}]。只填有证据的完整关系；无法确认时填 []。不要用“认为”代替关系。</small>',
  );
  $("#sources").classList.remove("hide");
  $("#sourceText").textContent =
    "支持来源：" +
    row.sources.join("\n") +
    "\n\n最近修改 / 模型审校：\n" +
    (row.edit_history || [])
      .map((h) => date(h.created) + " · " + h.reason)
      .join("\n");
  $("#sourceLinks").innerHTML = row.sources
    .map(
      (id) =>
        '<button data-child="' +
        esc(id) +
        '">读取证据 ' +
        esc(id.slice(0, 12)) +
        "</button>",
    )
    .join("");
  $$("[data-child]").forEach(
    (e) => (e.onclick = () => guard(() => openRecord(e.dataset.child))),
  );
  renderVersions("fact", row.id, row.revision, row.versions || []);
  $("#forget").classList.add("hide");
  $("#delete").classList.remove("hide");
  showEditor();
}
function newMemory() {
  current = { kind: "new" };
  $("#editorTitle").textContent = "新增永久记忆";
  $("#editorMeta").innerHTML =
    '<label>所属会话 <input id="newSid" placeholder="adapter:dm:user 或 adapter:gm:group" value="' +
    esc($("#session").value || status?.sessions?.[0] || "") +
    '"></label>';
  $("#editLabel").textContent = "值得长久记住的事";
  $("#editText").value = "";
  $("#factFields").classList.add("hide");
  $("#sources").classList.add("hide");
  $("#forget").classList.add("hide");
  // ★ 2026-09-29（用户实测）：#reextract 由 renderRecord **动态创建** ⇒ openFact/newMemory
  //   此前完全不碰它 ✗ ⇒ 先开过常驻永久记录、再看事实/新增，按钮会残留，
  //   且它的 onclick 仍是 record 分支的闭包 ⇒ 点了会发错 job ✗（这里统一收起 ✓）
  const reextractEl = $("#reextract");
  if (reextractEl) reextractEl.classList.add("hide");
  $("#delete").classList.add("hide");
  showEditor();
}
async function saveEdit(extra) {
  if (current.kind === "new") {
    await api("/memory", {
      sid: $("#newSid").value,
      content: $("#editText").value,
    });
  } else {
    let patch =
      extra ||
      (current.kind === "record"
        ? { summary: $("#editText").value }
        : { content: $("#editText").value, ...factPatch() });
    if (current.kind === "fact" && !extra) {
      patch.tags = patch.tags.split("\n").filter(Boolean);
      try {
        patch.relations = JSON.parse(patch.relations);
      } catch {
        throw Error("关系必须是 JSON 三元组数组");
      }
    }
    const payload = {
      kind: current.kind,
      target: current.row.id,
      revision: current.row.revision,
      patch,
      reason: "WebUI 人工编辑",
    };
    try {
      await api("/edit", payload);
    } catch (e) {
      // ★ 2026-09-27（用户实测）：从「任务明细」进来的 revision 是**快照时的旧版本号** ✗
      //   ⇒ 必然 409 ✗；而旧文案讲的是"设置被改过" ✗ 且**没有出路按钮** ✗
      //   服务端已支持 force（明确覆盖 ✓ 不透明传 revision ✓）⇒ 这里补第二条出路 ✓
      if (e.status !== 409) throw e;
      if (
        !confirm(
          "这条记忆在别处已经改了（可能被整理、合并或压缩更新过）。\n" +
            "要用你现在的版本覆盖吗？你的输入不会丢。",
        )
      ) {
        return; // 保留编辑器与你的输入 ✓
      }
      await api("/edit", { ...payload, force: true });
    }
  }
  $("#editor").close();
  current = null;
  saveDraft();
  if (current.row && current.row.cold) {
    // ★ 冷归档正文不在热库 ⇒ 改摘要不会动正文，必须讲清楚 ✗（否则用户以为改了正文 ✓）
    toast("摘要已保存 ✓ 冷归档正文要用「取回」才会回到热库 ✓");
  } else {
    toast("已保存，即时生效");
  }
  await poll();
  if (tab === "archives") await loadArchives();
  if (tab === "profiles") await loadFacts();
}
function readConfig() {
  const value = { ...config.settings };
  $$("#configForm [data-key]").forEach((e) => {
    const key = e.dataset.key,
      kind = e.dataset.kind;
    value[key] =
      kind === "boolean"
        ? e.checked
        : kind === "array"
          ? e.value.split("\n").filter(Boolean)
          : ["integer", "number"].includes(kind)
            ? Number(e.value)
            : e.value;
  });
  return value;
}
async function loadConfig() {
  config = await api("/config");
  models = (await api("/models")).models;
  renderConfig(config.settings);
  configDirty = false;
  $("#dirty").textContent = "配置已同步";
}
function vectorControls() {
  const toggle = document.querySelector("[data-key=semantic_enabled]"),
    select = document.querySelector("[data-key=embedding_model]");
  if (toggle && select) select.disabled = !toggle.checked;
}
// v2.18.15：压缩分批模式是**二选一**的 ✓ 只显示当前模式真正生效的那几个参数 ✓
// ⚠️ 必须用 `.hide` 类 ✗ 不能用 `el.hidden = true` ✓
//    因为 `.field { display: flex }` 会**盖掉** `[hidden]` 的 display:none ✗
//    （实测：真实页面里字段照样显示 ✓ 而 jsdom 测试查的是属性所以没抓到 ✗ 现在测试也改成查类 ✓）
// ⚠️ 这里**不再**插入额外说明 ✓ 字段自带的帮助已经说清了 ✗ 重复显示反而乱 ✓
const BATCH_MODE_FIELDS = {
  rounds: ["compress_rounds"],
  records: ["threshold", "batch_size"],
};
function applyBatchMode() {
  const mode = document.querySelector('[data-key="compress_batch_mode"]');
  if (!mode) return;
  const current = mode.value;
  Object.entries(BATCH_MODE_FIELDS).forEach(([name, keys]) => {
    keys.forEach((key) => {
      const el = document.querySelector('[data-field="' + key + '"]');
      if (el) el.classList.toggle("hide", name !== current);
    });
  });
}
function renderConfig(values) {
  $("#configForm").innerHTML = Object.entries(config.schema.properties)
    .map(([key, p]) => {
      const type = p.type,
        value = values[key];
      let input = "";
      if (type === "boolean")
        input = '<input type="checkbox" ' + (value ? "checked" : "") + ">";
      else if (key.endsWith("_model"))
        input =
          '<select><option value="">使用 KiraAI 默认' +
          (key === "compress_model"
            ? "快速"
            : key === "audit_model"
              ? "主"
              : "向量") +
          "模型</option>" +
          models
            .filter(
              (m) =>
                m.kind === (key === "embedding_model" ? "embedding" : "llm"),
            )
            .map(
              (m) =>
                '<option value="' +
                esc(m.id) +
                '" ' +
                (value === m.id ? "selected" : "") +
                ">" +
                esc(
                  (m.provider || String(m.id).split(":")[0] || "") +
                  " · " +
                  (m.model || m.name || m.id),
                ) +
                "</option>",
            )
            .join("") +
          (value && !models.some((m) => m.id === value)
            ? '<option selected value="' +
              esc(value) +
              '">' +
              esc(value) +
              "（当前不可用）</option>"
            : "") +
          "</select>";
      else if (p.enum)
        input =
          "<select>" +
          p.enum
            .map(
              (v) =>
                '<option value="' +
                esc(v) +
                '" ' +
                (value === v ? "selected" : "") +
                ">" +
                esc(
                  {
                    global: "全局 · 跨用户跨会话",
                    linked: "同参与者 · 关联会话",
                    session: "当前会话",
                  }[v] || v,
                ) +
                "</option>",
            )
            .join("") +
          "</select>";
      else if (
        type === "array" ||
        key === "compress_instruction" ||
        key.endsWith("_prompt")
      )
        input =
          "<textarea>" +
          esc(Array.isArray(value) ? value.join("\n") : value) +
          "</textarea>";
      else
        input =
          '<input type="' +
          (["number", "integer"].includes(type) ? "number" : "text") +
          '" value="' +
          esc(value) +
          '" ' +
          (p.minimum !== undefined ? 'min="' + p.minimum + '"' : "") +
          " " +
          (p.maximum !== undefined ? 'max="' + p.maximum + '"' : "") +
          ' step="' +
          (type === "number" ? "any" : "1") +
          '">';
      input = input.replace(
        /<(input|select|textarea)/,
        '<$1 data-key="' + key + '" data-kind="' + type + '"',
      );
      const wide = key === "compress_instruction" || key.endsWith("_prompt");
      const restorable =
        p.default !== undefined &&
        (wide || type === "array" || type === "string" || type === "markdown");
      return (
        '<label class="field ' +
        (wide ? "wide" : "") +
        '" data-field="' + esc(key) +
        '"><span>' +
        esc(config.labels?.[key] || fields[key] || key) +
        (restorable
          ? '<button type="button" class="restore" data-default="' + key + '">恢复默认</button>'
          : "") +
        "</span>" +
        input +
        "<small>" +
        esc(config.help?.[key] || "") +
        "</small></label>"
      );
    })
    .join("");
  applyBatchMode();   // v2.18.15：按当前模式收起另一套参数 ✓
  $$("#configForm [data-key]").forEach(
    (e) =>
      (e.oninput = () => {
        vectorControls();
        applyBatchMode();   // 切换模式时立刻收/放 ✓ 不用刷新 ✓
        configDirty = true;
        $("#dirty").textContent = "有未保存的修改";
        saveDraft();
      }),
  );
  $$("#configForm [data-default]").forEach(
    (b) =>
      (b.onclick = (event) => {
        event.preventDefault();
        const key = b.dataset.default;
        const fallback = config.schema.properties[key]?.default;
        const input = document.querySelector(
          '#configForm [data-key="' + key + '"]',
        );
        if (fallback === undefined || !input) return;
        if (Array.isArray(fallback)) input.value = fallback.join("\n");
        else if (input.type === "checkbox") input.checked = !!fallback;
        else input.value = fallback;
        input.dispatchEvent(new Event("input", { bubbles: true }));
        toast("已填入内置默认文案，记得点「保存并生效」");
      }),
  );
  vectorControls();
}
async function guard(fn) {
  try {
    $("#error").classList.add("hide");
    await fn();
  } catch (e) {
    fail(e);
  }
}
$("#retryMigration").onclick = () =>
  guard(async () => {
    const b = $("#retryMigration");
    b.disabled = true;
    try {
      await api("/migrate", {});
      await poll();
      toast("迁移检查完成，请查看报告");
    } finally {
      b.disabled = false;
    }
  });
$$("[data-tab]").forEach(
  (e) =>
    (e.onclick = () =>
      guard(async () => {
        await selectTab(e.dataset.tab);
        if (e.dataset.tab === "health") {
          // ★ 2026-09-19 二改（用户实测"不切页就永远正在加载"）：
          //   ⚠️ 重试必须放 **finally** —— 原来写在 `await loadHealth()` **后面** ✗
          //      它一旦抛错/卡住，那行代码根本不会执行 ✗（"补救放在失败路径之后"）
          const heal = (tries) => {
            const m = $("#healthMeta");
            if (!m) return;
            if (!/正在加载/.test(m.textContent)) return;      // 已经出内容 ✓ 收工
            if (tries <= 0) {
              m.textContent = "加载失败：点右上「自动刷新」可重试 ✓";
              return;
            }
            loadHealth(healthPage)
              .catch(() => {})
              .finally(() => setTimeout(() => heal(tries - 1), 1200));
          };
          try {
            await loadHealth();
          } catch (err) {
            /* 抛错也照样排期自愈 ✓ */
          } finally {
            setTimeout(() => heal(3), 1200);
          }
        }
      })),
);
$("#refresh").onclick = () =>
  guard(async () => {
    await refreshNow();
    autoRefresh = !autoRefresh;
    localStorage.setItem("alife-auto-refresh", autoRefresh ? "on" : "off");
    applyRefreshMode();
  });
async function refreshNow() {
  await poll();
  if (tab === "archives") await loadArchives();
  if (tab === "profiles") await loadFacts();
  if (tab === "names") await loadNames();
}
function applyRefreshMode() {
  const button = $("#refresh");
  button.classList.toggle("auto", autoRefresh);
  button.setAttribute("aria-pressed", String(autoRefresh));
  $("#refreshLabel").textContent = autoRefresh ? "自动刷新" : "手动刷新";
  button.title = autoRefresh
    ? "自动刷新中 · 点击切换为手动"
    : "手动刷新 · 点击刷新并恢复自动";
  if (refreshTimer) clearInterval(refreshTimer);
  refreshTimer = autoRefresh
    ? setInterval(() => {
        if (!document.hidden) poll();
      }, 2500)
    : null;
}
// ---- 主题：用户选过就要守住，别被宿主的桥脚本改回去 ----
// 宿主把插件页放在 iframe 里，并自动注入 /plugin-bridge.js；那个桥会按**宿主的**主题
// 反复设置 <html data-theme>（切侧边栏、宿主主题变化都会触发）→ 用户的选择会被瞬间覆盖 ✗
const THEME_KEY = "alife-theme";

function savedTheme() {
  try {
    const value = localStorage.getItem(THEME_KEY);
    return value === "dark" || value === "light" ? value : "";
  } catch (err) {
    return "";
  }
}
function syncThemeButton(theme) {
  const button = $("#theme");
  if (!button) return;
  button.textContent = theme === "dark" ? "☾" : "☀";
  const saved = savedTheme();
  button.title = saved
    ? theme === "dark"
      ? "当前：黑夜主题（点击切到明亮）"
      : "当前：明亮主题（点击切到黑夜）"
    : "当前跟随宿主主题（点击可固定为" + (theme === "dark" ? "明亮" : "黑夜") + "）";
  button.setAttribute("aria-label", button.title);
}
function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  syncThemeButton(theme);
}
function initTheme() {
  // 内联脚本已经设过一次（避免首帧闪）；这里兜底 + 同步按钮图标
  applyTheme(savedTheme() || document.documentElement.dataset.theme || "light");
  // 宿主桥改 data-theme 时：用户明确选过 → 抢回来；没选过 → 跟随宿主，只同步图标
  new MutationObserver(() => {
    const saved = savedTheme();
    const current = document.documentElement.dataset.theme;
    if (saved) {
      if (current !== saved) applyTheme(saved);
    } else {
      syncThemeButton(current === "dark" ? "dark" : "light");
    }
  }).observe(document.documentElement, {
    attributes: true,
    attributeFilter: ["data-theme"],
  });
}
$("#theme").onclick = () => {
  const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  applyTheme(next);
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch (err) {
    /* 记不住就只在本次会话生效 */
  }
};
let forceMotion = localStorage.getItem("alife-motion") === "force";
function applyMotion() {
  if (forceMotion) document.documentElement.dataset.motion = "force";
  else delete document.documentElement.dataset.motion;
  const button = $("#motion");
  button.setAttribute("aria-pressed", String(forceMotion));
  button.classList.toggle("on", forceMotion);
  button.title = forceMotion
    ? "动效已强制开启 · 点击跟随系统"
    : "动效跟随系统 · 点击强制开启";
}
$("#motion").onclick = () => {
  forceMotion = !forceMotion;
  localStorage.setItem("alife-motion", forceMotion ? "force" : "auto");
  applyMotion();
};
applyMotion();
/* ---- 星尘与流星：背景一层、前景一层，同色系随机变色 ---- */
const FX_HUES = [240, 252, 262, 272, 282, 294, 306];
let fxEnabled = localStorage.getItem("alife-fx") !== "off";
let fxTimers = [];
const fxReduce = window.matchMedia("(prefers-reduced-motion: reduce)");
const fxAllowed = () =>
  fxEnabled && !(fxReduce.matches && !forceMotion);
function buildStars() {
  const layer = $("#fxStars");
  if (!layer) return;
  layer.innerHTML = "";
  const count = window.innerWidth < 720 ? 36 : 72;
  for (let i = 0; i < count; i += 1) {
    const star = document.createElement("i");
    const size = 1 + Math.random() * 1.9;
    star.style.cssText =
      "left:" + (Math.random() * 100).toFixed(2) + "%;" +
      "top:" + (Math.random() * 100).toFixed(2) + "%;" +
      "width:" + size.toFixed(2) + "px;height:" + size.toFixed(2) + "px;" +
      "--fx-hue:" + FX_HUES[Math.floor(Math.random() * FX_HUES.length)] + ";" +
      "--fx-dur:" + (2.2 + Math.random() * 4.6).toFixed(2) + "s;" +
      "--fx-delay:" + (-Math.random() * 7).toFixed(2) + "s;";
    layer.appendChild(star);
  }
}
function spawnMeteor(front) {
  const layer = front ? $("#fxFront") : $("#fxStars");
  if (!layer) return;
  const hue = FX_HUES[Math.floor(Math.random() * FX_HUES.length)];
  // 方向：0°=向右、90°=正下、180°=向左 —— 取 0~180 就永远不会向上飞
  const angle = 8 + Math.random() * 164;
  const rad = (angle * Math.PI) / 180;
  const distance = front ? 70 + Math.random() * 45 : 55 + Math.random() * 45;
  const travelX = Math.cos(rad) * distance;
  const travelY = Math.sin(rad) * distance * 0.6;
  // 起点随方向换边：向右飞从左侧入场、向左飞从右侧入场，纵向都偏上
  const fromLeft = Math.cos(rad) >= 0;
  const node = document.createElement("div");
  node.className = "meteor";
  node.style.cssText =
    "left:" + (fromLeft ? -18 + Math.random() * 44 : 56 + Math.random() * 44).toFixed(1) + "vw;" +
    "top:" + (-16 + Math.random() * 36).toFixed(1) + "vh;" +
    "--fx-hue:" + hue + ";" +
    "--fx-angle:" + angle.toFixed(1) + "deg;" +
    "--fx-dx:" + travelX.toFixed(1) + "vw;" +
    "--fx-dy:" + travelY.toFixed(1) + "vh;" +
    "--fx-len:" +
    (front ? 150 + Math.random() * 110 : 80 + Math.random() * 90).toFixed(0) +
    "px;--fx-thick:" + (front ? 3 : 2) + "px;" +
    "--fx-fly:" +
    (front ? 1.05 + Math.random() * 0.75 : 1.35 + Math.random() * 1.1).toFixed(2) +
    "s;";
  node.innerHTML = "<span></span>";
  node.addEventListener("animationend", () => node.remove());
  layer.appendChild(node);
}
function fxLoop(front, min, max) {
  const tick = () => {
    fxTimers.push(
      setTimeout(() => {
        if (fxAllowed() && !document.hidden) spawnMeteor(front);
        tick();
      }, (min + Math.random() * (max - min)) * 1000),
    );
  };
  tick();
}
function applyFx() {
  document.documentElement.dataset.fx = fxEnabled ? "on" : "off";
  const button = $("#fx");
  if (button) {
    button.setAttribute("aria-pressed", String(fxEnabled));
    button.classList.toggle("on", fxEnabled);
    button.title = fxEnabled
      ? "星尘与流星：开 · 点击关闭"
      : "星尘与流星：关 · 点击开启";
  }
  fxTimers.forEach(clearTimeout);
  fxTimers = [];
  const back = $("#fxStars"),
    front = $("#fxFront");
  if (fxAllowed()) {
    buildStars();
    fxLoop(false, 5, 12); // 背景流星
    fxLoop(true, 14, 30); // 前景流星：偶尔从卡片上掠过
  } else {
    if (back) back.innerHTML = "";
    if (front) front.innerHTML = "";
  }
}
$("#fx").onclick = () => {
  fxEnabled = !fxEnabled;
  localStorage.setItem("alife-fx", fxEnabled ? "on" : "off");
  applyFx();
};
document.addEventListener("visibilitychange", () =>
  document.documentElement.toggleAttribute("data-fx-paused", document.hidden),
);
window.addEventListener("resize", () => {
  if (fxAllowed()) buildStars();
});
applyFx();
initTheme();
const BOOT_QUOTES = [
  "和谁的记忆，我都不想忘记",
  "每段记忆，都有来处",
  "和你的记忆，我不想再忘记",
  "想念，念想",
  "记得，是最长情的陪伴",
  "把时间，收进可以回去的地方",
  "走过的路，都在这里",
  "你忘掉的，我替你记着",
  "往事有光，来日有信",
  "每一次相遇，都值得被留存",
  "我们把日子，过成了故事",
  "记忆不是负担，是归处",
];
let bootConfig = { enabled: true, replay_seconds: 90 };
try {
  Object.assign(bootConfig, JSON.parse(localStorage.getItem("alife-boot") || "{}"));
} catch {}
let bootPlaying = true,
  bootTimer = null,
  bootHiddenAt = 0;
function bootQuote() {
  const quote = $("#bootQuote");
  if (!quote) return;
  const text = BOOT_QUOTES[Math.floor(Math.random() * BOOT_QUOTES.length)];
  quote.replaceChildren(
    ...[...text].map((ch, index) => {
      const span = document.createElement("span");
      span.textContent = ch;
      span.style.setProperty("--i", index);
      return span;
    }),
  );
}
function endBoot() {
  const boot = $("#boot");
  if (!boot || !bootPlaying) return;
  boot.classList.add("skip");
  clearTimeout(bootTimer);
  bootTimer = setTimeout(() => {
    boot.hidden = true;
    boot.classList.remove("skip");
    bootPlaying = false;
  }, 320);
}
function startBootTimer() {
  clearTimeout(bootTimer);
  bootTimer = setTimeout(endBoot, 3900);
}
function playBoot(replay) {
  const boot = $("#boot");
  if (!boot) return;
  if (!bootConfig.enabled) {
    boot.hidden = true;
    return;
  }
  if (replay) {
    if (bootPlaying) return;
    // Replacing the node restarts every CSS animation, including pseudo-elements.
    const fresh = boot.cloneNode(true);
    boot.replaceWith(fresh);
  }
  const current = $("#boot");
  current.hidden = false;
  current.classList.remove("skip");
  bootPlaying = true;
  bootQuote();
  startBootTimer();
}
// The static markup is already animating; just fill the quote and arm the timer.
playBoot(false);
document.addEventListener("click", (event) => {
  if (bootPlaying && event.target.closest("#boot")) endBoot();
});
document.addEventListener("keydown", (event) => {
  if (bootPlaying && ["Escape", "Enter", " "].includes(event.key)) endBoot();
});
function bootReplay() {
  if (!bootHiddenAt) return;
  const gap = (Date.now() - bootHiddenAt) / 1000;
  bootHiddenAt = 0;
  const cooldown = Number(bootConfig.replay_seconds);
  if (cooldown > 0 && gap >= cooldown) playBoot(true);
}
new IntersectionObserver(
  (entries) => {
    for (const entry of entries) {
      if (entry.isIntersecting) bootReplay();
      else if (!bootHiddenAt) bootHiddenAt = Date.now();
    }
  },
  { threshold: 0 },
).observe(document.documentElement);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") bootReplay();
  else if (!bootHiddenAt) bootHiddenAt = Date.now();
});
$("#new").onclick = newMemory;
$("#closeEditor").onclick = () => {
  $("#editor").close();
  saveDraft();
};
$("#editText").oninput = saveDraft;
$("#search").onclick = () =>
  guard(() => {
    offset = 0;
    return loadArchives();
  });
$("#keyword").onkeydown = (e) => {
  if (e.key === "Enter") $("#search").click();
};
for (const id of ["#session", "#level", "#includeGlobal"])
  $(id).onchange = () =>
    guard(() => {
      offset = 0;
      return loadArchives();
    });
$("#factContent").onchange = () =>
  guard(() => {
    factOffset = 0;
    $("#subject").placeholder = $("#factContent").checked
      ? "搜索事实内容，比如「猫粮」「加班」"
      : "按实体 ID 筛选画像";
    clearGraphFocus();
    return loadFacts();
  });
$("#category").onchange = () =>
  guard(() => {
    factOffset = 0;
    clearGraphFocus();
    return loadFacts();
  });
$("#subject").onkeydown = (e) => {
  if (e.key === "Enter") $("#filterFacts").click();
};
$("#nameQuery").onkeydown = (e) => {
  if (e.key === "Enter") $("#searchNames").click();
};
$("#prev").onclick = () =>
  guard(() => {
    offset = Math.max(0, offset - 20);
    return loadArchives();
  });
$("#next").onclick = () =>
  guard(() => {
    offset += 20;
    return loadArchives();
  });
$("#filterFacts").onclick = () =>
  guard(() => {
    factOffset = 0;
    clearGraphFocus();
    return loadFacts();
  });
$("#factPrev").onclick = () =>
  guard(() => {
    factOffset = Math.max(0, factOffset - 100);
    return loadFacts();
  });
$("#factNext").onclick = () =>
  guard(() => {
    factOffset += 100;
    return loadFacts();
  });
$("#saveEdit").onclick = () =>
  guard(async () => {
    const b = $("#saveEdit");
    b.disabled = true;
    try {
      await saveEdit();
    } finally {
      b.disabled = false;
    }
  });
$("#forget").onclick = () =>
  guard(() => saveEdit({ active: !current.row.active }));
$("#delete").onclick = () => $("#deleteConfirm").classList.remove("hide");
$("#cancelDelete").onclick = () => $("#deleteConfirm").classList.add("hide");
$("#confirmDelete").onclick = () =>
  guard(async () => {
    const b = $("#confirmDelete");
    b.disabled = true;
    try {
      await saveEdit({ deleted: true });
    } finally {
      b.disabled = false;
    }
  });
async function saveConfigNow() {
  const result = await api("/config", {
    revision: config.revision,
    settings: readConfig(),
  });
  config.revision = result.revision;
  config.settings = readConfig();
  configDirty = false;
  $("#conflict").classList.add("hide");
  $("#dirty").textContent = "已保存，配置立即生效";
  saveDraft();
  toast("配置已生效");
}

$("#saveConfig").onclick = () =>
  guard(async () => {
    try {
      await saveConfigNow();
    } catch (e) {
      // v2.18.12：版本冲突**不当错误弹窗** ✓ 改为常驻条 + 两条真正可行的出路 ✓
      // （旧文案让人"重新打开最新版本再保存" ✗ 但刷新后草稿会带回旧版本号 →
      //   再点保存**仍然冲突** ✗ 是一句错误指引 ✓）
      if (e.status !== 409) {
        // ★ 2026-09-29：非 409 失败以前只弹 toast ✗ ⇒ 状态条还留着上一次的
        //   「已保存，配置立即生效」✗（用户会以为这次也存上了 ✗）⇒ 写明失败态 ✓
        $("#dirty").textContent = "保存失败：未生效";
        throw e;
      }
      $("#conflict").classList.remove("hide");
      $("#dirty").textContent = "未保存：设置已在别处被改过";
    }
  });

// 出路一：用我的修改覆盖 —— 只借服务端**最新**的 revision ✓ 表单内容仍是你的 ✓
$("#conflictOverwrite").onclick = () =>
  guard(async () => {
    const fresh = await api("/config");
    config.revision = fresh.revision;
    await saveConfigNow();
  });

// 出路二：丢弃我的修改，载入最新（loadConfig 会重拉、重渲染并清掉草稿 ✓）
$("#conflictDiscard").onclick = () =>
  guard(async () => {
    await loadConfig();
    $("#conflict").classList.add("hide");
    toast("已载入最新设置");
  });
// v2.18.19：整理永久记忆时先问一句 —— 按冷却 还是 全部重新整理 ✓
// 用仓库既有的原生 `<dialog class="dialog">` ✓（已有样式与 ::backdrop ✓ 自带 ESC 关闭 ✓）
function askTidyMode() {
  return new Promise((resolve) => {
    // 冷却天数从设置表单读 ✓（没渲染就读不到 → 回退 14 ✓）
    const box = document.querySelector('[data-key="permanent_tidy_days"]');
    const days = (box && box.value) || "14";
    const d = document.createElement("dialog");
    d.className = "dialog";
    // v2.18.23：按仓库**房型风格**重写 ✓（panel-title + muted + notice + actions + primary ✓）
    // 两个选项做成"可选卡片" ✓（.tidy-opt ✓ 样式只吃主题变量 ⇒ 深浅色自动适配 ✓）
    d.innerHTML =
      '<div class="panel-title"><h2>整理永久记忆</h2>' +
      '<button id="tidyClose" aria-label="关闭">×</button></div>' +
      '<p class="muted">永久记忆会常驻在会话上下文里，整理会重新核对它们与原文的关系。</p>' +
      '<div class="notice">按冷却整理更省 token；全部重新整理更彻底。</div>' +
      '<label class="field tidy-opt"><input type="radio" name="tidymode" value="due" checked>' +
      '<span><b>只整理到期的</b>' +
      '<small class="muted">按 ' + days + ' 天冷却，挑还没整理过的会话；token 花得更少</small></span></label>' +
      '<label class="field tidy-opt"><input type="radio" name="tidymode" value="all">' +
      '<span><b>全部重新整理</b>' +
      '<small class="muted">无视冷却，把每个会话常驻的永久记忆都过一遍；' +
      'token 花得更多，但会重新提取事实</small></span></label>' +
      // ★ 2026-09-19（用户要求）：全局**不做**"完全重新提取"（会连本来好好的事实一起重写），
      //   这里只给一句指引小字 ✓
      '<p class="muted">如果有你认为没有提取到位、想重复自动提取的，' +
      '可在永久记忆页面对单条强制重新提取事实。</p>' +
      '<div class="actions"><button id="tidyCancel">取消</button>' +
      '<button id="tidyGo" class="primary">开始</button></div>';
    document.body.appendChild(d);
    const done = (v) => { try { d.close(); } catch (err) {} d.remove(); resolve(v); };
    d.querySelector("#tidyCancel").onclick = () => done(null);
    d.querySelector("#tidyClose").onclick = () => done(null);
    d.querySelector("#tidyGo").onclick = () =>
      done(d.querySelector('input[name="tidymode"]:checked').value);
    // 选中项高亮 ✓（JS 切 class ✗ 不依赖 :has ✓ 兼容性更稳 ✓）
    const mark = () => d.querySelectorAll(".tidy-opt").forEach((el) => {
      const input = el.querySelector("input");
      el.classList.toggle("on", !!(input && input.checked));
    });
    d.querySelectorAll('input[name="tidymode"]').forEach((i) => { i.onchange = mark; });
    mark();
    d.addEventListener("cancel", () => done(null));   // ESC ✓
    d.showModal();
  });
}
$$("[data-job]").forEach(
  (e) =>
    (e.onclick = () =>
      guard(async () => {
        const job = e.dataset.job;
        // 「整理永久记忆」「重建向量索引」是**全局**任务：后端 tidy 走
        // `queue_tidy_all(sid)`（忽略 sid、按"所有有意久记忆的会话"排队 ✓），
        // reindex 更是与会话无关 ✓ ⇒ 不能因为没选会话就把它们拦掉 ✗✓
        // （按钮提示本来就写着"不受上方会话选择限制" ✓ 原来前后矛盾 ✓
        //   于是「无视冷却」那个确认框**永远弹不出来** ✓ 2026-09-17 用户实测 ✓）
        const needsSession = job !== "tidy" && job !== "reindex";
        if (needsSession && !$("#jobSession").value) {
          throw Error("请先选择一个已有会话");
        }
        const payload = {
          sid: $("#jobSession").value || "",
          kind: job,
        };
        if (job === "tidy") {
          const mode = await askTidyMode();
          if (!mode) return;                       // 取消 ✓
          payload.force = mode === "all";           // 全部重新整理 → 无视冷却 ✓
        }
        const res = await api("/jobs", payload);
        // ★ 2026-09-27：整理/合并是"按会话排队"✓ 若一个会话都没排上 ⇒ 别喊"已开始"✗
        //   （用户实测：点了却没任何任务 ✗ 容易以为坏了 ✓）
        if (res && res.sessions === 0) {
          toast("没有需要处理的会话（这些会话当前没有常驻永久记忆）");
        } else {
          toast(payload.force ? "已开始全部重新整理" : "任务已进入后台队列");
        }
        await poll();
      })),
);
$("#export").onclick = () =>
  guard(async () => {
    const data = await api("/export");
    $("#exportText").value = JSON.stringify(data, null, 2);
    $("#downloadExport").classList.toggle("hide", window.parent !== window);
    $("#exportHint").textContent = "";
    $("#exportDialog").showModal();
  });
$("#closeExport").onclick = () => $("#exportDialog").close();
$("#copyExport").onclick = async () => {
  const field = $("#exportText");
  field.focus();
  field.select();
  try {
    await navigator.clipboard.writeText(field.value);
    $("#exportHint").textContent = "已复制完整内容";
  } catch {
    $("#exportHint").textContent = document.execCommand("copy")
      ? "已复制完整内容"
      : "内容已全选，请按 Ctrl+C（Mac为 ⌘C）复制";
  }
};
$("#downloadExport").onclick = () => {
  const blob = new Blob([$("#exportText").value], { type: "application/json" }),
    a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "alife-memory-export.json";
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
};
for (let i = 1; i <= 32; i++)
  $("#level").add(new Option("L" + i + " · " + i + " 次压缩", i));
Object.entries(labels).forEach(([v, n]) =>
  $("#category").add(new Option(n, v)),
);
async function boot() {
  if (window.PluginPageContext && window.parent !== window)
    ctx = await window.PluginPageContext.ready();
  await poll();
  let draft;
  try {
    draft = JSON.parse(sessionStorage.getItem("alife-draft"));
  } catch {}
  if (draft?.configDirty) {
    await loadConfig();
    renderConfig({ ...config.settings, ...draft.config });
    config.revision = draft.configRevision;
    configDirty = true;
    $("#dirty").textContent = "已恢复未保存草稿；保存时将检查版本冲突";
  }
  if (draft?.tab) await selectTab(draft.tab);
  if (draft?.nameDraft) {
    openName(draft.nameDraft.row);
    $("#nameValue").value = draft.nameDraft.name;
    $("#nameReason").value = draft.nameDraft.reason;
  }
  if (draft?.editor?.current) {
    current = draft.editor.current;
    if (current.kind === "new") {
      newMemory();
      $("#newSid").value = draft.editor.newSid || "";
    } else if (current.kind === "record") {
      renderRecord();
      showEditor();
    } else openFact(current.row);
    $("#editText").value = draft.editor.text;
    Object.entries(draft.editor.factPatch || {}).forEach(([k, v]) => {
      const e = $('#factFields [data-field="' + k + '"]');
      if (e) e.value = v;
    });
  }
  applyRefreshMode();
  if (
    window.matchMedia("(prefers-reduced-motion: reduce)").matches &&
    !forceMotion &&
    !localStorage.getItem("alife-motion-hint")
  ) {
    localStorage.setItem("alife-motion-hint", "1");
    toast("系统开启了“减少动态效果”，动效已关闭；点 ✨ 可强制开启");
  }
}
let shownFacts = [],
  graphMode = "relations",
  nameOffset = 0,
  selectedName = null;
const displayNames = {};

// 2026-09-18：关系/主体里常见**原始 ID**（qq:769690776）✗ 而名字表早就有 ✓
//   ⇒ 统一解析成名字显示 ✓ 原始 ID 放进 title 备查 ✓
//   ⚠️ 必须在**模块作用域**定义 ✓（早前版本误放在某个函数里 ⇒ 别处调用会 ReferenceError ✓）

const nameOf = (value) => displayNames[String(value == null ? "" : value)] ||
  String(value == null ? "" : value);

// 名字表是"打开名字页才加载"的 ✗ ⇒ 画像页可能还没数据 ✓
// 这里做一次**静默补载** ✓：拿不到就退回原始 ID ✓（绝不影响主流程 ✓）
async function ensureNames() {
  if (Object.keys(displayNames).length) return;
  try {
    const rows = await api("/names?" + new URLSearchParams({ query: "", offset: 0 }));
    rows.forEach((n) => {
      if (n && n.name) displayNames[n.id] = n.name;
    });
  } catch (e) {
    /* 静默 ✓ */
  }
}
const fixedLabels = {
  global: "全局记忆",
  self: "机器人自身",
  unscoped: "未分类 · 来源会话未确定",
};
const kindLabel = (kind) =>
  kind === "user"
    ? "人物"
    : kind === "self"
      ? "机器人自身"
      : kind === "global"
        ? "全局记忆"
        : "会话 / 群";
const displayLabel = (id) => {
  const name = displayNames[id] || fixedLabels[id];
  return name ? name + " · " + id : id;
};
function renderGraph() {
  drawMemoryGraph(
    $("#relations"),
    shownFacts,
    graphMode,
    displayNames,
    (f) => openFact(f),
    focusGraphEntity,
    graphFilter,
  );
}
let graphFilter = null;
function clearGraphFocus() {
  if (!graphFilter) {
    $("#graphFocus").classList.add("hide");
    return;
  }
  graphFilter = null;
  $("#graphFocus").classList.add("hide");
  renderFactCards(shownFacts);
}
function focusGraphEntity(id) {
  const panel = $("#graphFocus");
  if (!id) {
    graphFilter = null;
    panel.classList.add("hide");
    renderFactCards(shownFacts);
    renderGraph();
    return;
  }
  graphFilter = id;
  const related = shownFacts.filter(
    (fact) =>
      fact.subject === id ||
      (fact.verified_relations || []).some(
        (relation) => relation.subject === id || relation.object === id,
      ),
  );
  renderFactCards(related);
  renderGraphFocus(id, related);
  renderGraph();
}
function renderGraphFocus(id, facts) {
  const rows = [];
  for (const fact of facts)
    for (const relation of fact.verified_relations || []) {
      if (relation.subject !== id && relation.object !== id) continue;
      const other = relation.subject === id ? relation.object : relation.subject;
      rows.push({ label: relation.predicate + " → " + displayLabel(other), fact });
    }
  const panel = $("#graphFocus");
  panel.innerHTML =
    '<div class="row"><strong>' +
    esc(displayLabel(id)) +
    '</strong><small class="muted">' +
    rows.length +
    ' 条联结 · 已筛选下方事实卡片</small><button id="graphClear">清除筛选</button></div>' +
    (rows.length
      ? '<div class="graph-focus-list">' +
        rows
          .map(
            (row, index) =>
              '<div class="graph-focus-row"><span>' +
              esc(row.label) +
              '</span><button data-focus-fact="' +
              index +
              '">编辑依据</button></div>',
          )
          .join("") +
        "</div>"
      : '<p class="muted">该实体在当前筛选页没有可展示的联结。</p>');
  panel.classList.remove("hide");
  $("#graphClear").onclick = () => focusGraphEntity(null);
  $$("[data-focus-fact]").forEach(
    (button) =>
      (button.onclick = () =>
        guard(() => openFact(rows[Number(button.dataset.focusFact)].fact))),
  );
}
$("#graphRelations").onclick = () => {
  graphMode = "relations";
  $("#graphRelations").setAttribute("aria-pressed", "true");
  $("#graphDimensions").setAttribute("aria-pressed", "false");
  renderGraph();
};
$("#graphDimensions").onclick = () => {
  graphMode = "dimensions";
  $("#graphRelations").setAttribute("aria-pressed", "false");
  $("#graphDimensions").setAttribute("aria-pressed", "true");
  renderGraph();
};
async function loadNames() {
  const rows = await api(
    "/names?" +
      new URLSearchParams({ query: $("#nameQuery").value, offset: nameOffset }),
  );
  rows.forEach((n) => {
    if (n.name) displayNames[n.id] = n.name;
  });
  $("#nameCards").innerHTML = rows.length
    ? rows
        .map((n, i) => {
          const stats = n.stats || {};
          const summary = (stats.summary || []).join(" · ") || n.identity_note || "";
          const aliases =
            [...new Set(n.history.map((h) => h.name))]
              .filter((x) => x !== n.name)
              .join("、") || "暂无";
          return `<article class="card"><span class="tag">${esc(kindLabel(n.kind))}</span><h3>${esc(n.name || n.label || "名称待补全")}</h3><small>${esc(n.id)}</small><p class="muted">${esc(summary)}</p><p class="muted">事实 ${stats.facts ?? 0} · 关系 ${stats.relations ?? 0} · 曾用名：${esc(aliases)}</p><footer><small>${n.updated ? date(n.updated) : "等待新消息或手动更新"}</small><button data-profile="${i}">查看画像</button><button data-name="${i}">查看与审校</button></footer></article>`;
        })
        .join("")
    : empty("未找到名称");
  $$("[data-name]").forEach(
    (b) => (b.onclick = () => openName(rows[Number(b.dataset.name)])),
  );
  $$("[data-profile]").forEach(
    (b) =>
      (b.onclick = () =>
        guard(() => openProfile(rows[Number(b.dataset.profile)].id))),
  );
  $("#namePage").textContent = "第 " + (nameOffset / 100 + 1) + " 页";
  $("#namePrev").disabled = nameOffset === 0;
  $("#nameNext").disabled = rows.length < 100;
}
function openName(n) {
  $$(".dialog-error").forEach((e) => e.remove());
  selectedName = n;
  $("#nameIdentity").textContent = n.id + " · " + (n.identity_note || "");
  $("#lookupName").disabled = !n.lookup_id;
  $("#lookupName").title = n.lookup_id
    ? "按稳定账号查询平台名称"
    : "迁移归档区没有可确认的平台账号，请手动补充称呼";
  $("#nameValue").value = n.name;
  $("#nameReason").value = "";
  $("#nameHistory").innerHTML =
    n.history
      .map(
        (h) =>
          `<div class="task"><strong>${esc(h.name)}</strong><small>${date(h.observed)} · ${esc(h.source)}<br>${esc(h.context)} ${esc(h.reason)}</small>${h.name === n.name ? "" : `<button data-setname="${esc(h.name)}">设为当前名</button>`}</div>`,
      )
      .join("") || '<p class="muted">尚无名称记录</p>';
  $$("#nameHistory [data-setname]").forEach(
    (b) =>
      (b.onclick = () =>
        guard(async () => {
          await api("/names", {
            entity_id: n.id,
            name: b.dataset.setname,
            revision: n.revision,
            reason: "恢复到曾用名",
          });
          toast("已切换当前称呼");
          $("#nameEditor").close();
          await loadNames();
        })),
  );
  if (!$("#nameEditor").open) $("#nameEditor").showModal();
}
$("#searchNames").onclick = () =>
  guard(() => {
    nameOffset = 0;
    return loadNames();
  });
$("#repairNames").onclick = () =>
  guard(async () => {
    const button = $("#repairNames");
    button.disabled = true;
    try {
      const report = await api("/maintenance/names", {});
      $("#repairNamesHint").textContent = report.repaired.length
        ? "已修复 " +
          report.repaired.length +
          " 个昵称：" +
          report.repaired.map((item) => item.name).join("、")
        : "没有发现被第三方插件改写的昵称";
      await loadNames();
    } finally {
      button.disabled = false;
    }
  });
function renderBootstrapNotice(info) {
  const box = $("#bootstrapNotice");
  if (!box) return;
  const active = info && info.merge_plugin && info.count && !info.reviewed;
  box.classList.toggle("hide", !active);
  if (!active) return;
  $("#bootstrapNoticeText").textContent =
    "检测到 " +
    info.merge_plugin +
    " 会改写会话上下文：库中有 " +
    info.count +
    " 个会话的历史播种记录无法确认来源，可能混入了其他会话。" +
    "如果你是先装记忆插件、后装合并插件，这些记录通常是正常的，可以保留。";
}
$("#bootstrapGo").onclick = () => selectTab("tasks");
$("#bootstrapKeep").onclick = () =>
  guard(async () => {
    await api("/maintenance/bootstrap/review", {});
    await poll();
  });
$("#purgeBootstrap").onclick = () =>
  guard(async () => {
    const button = $("#purgeBootstrap");
    button.disabled = true;
    try {
      const report = await api("/maintenance/bootstrap", {});
      $("#purgeBootstrapHint").textContent = report.removed
        ? "已清理 " +
          report.removed +
          " 条（涉及 " +
          report.sessions +
          " 个会话）· 原文保留可恢复"
        : "没有发现历史播种记录";
      await poll();
    } finally {
      button.disabled = false;
    }
  });
$("#cleanupTools").onclick = () =>
  guard(async () => {
    const button = $("#cleanupTools");
    button.disabled = true;
    try {
      const report = await api("/maintenance/tools", {});
      $("#cleanupToolsHint").textContent =
        "已清理：移除 " +
        report.removed +
        " 条 · 重写 " +
        report.rewritten +
        " 条 · 约省 " +
        report.freed_chars +
        " 字符（原文保留）";
      await poll();
    } finally {
      button.disabled = false;
    }
  });
async function maybeAskNameBatch() {
  if (localStorage.getItem("alife-name-batch") === "off") return;
  if (sessionStorage.getItem("alife-name-batch-shown") === "1") return;
  let rows = [];
  try {
    rows = await api("/names?offset=0");
  } catch {
    return;
  }
  const pending = rows.filter(
    (row) =>
      !row.name &&
      row.kind !== "global" &&
      row.kind !== "self" &&
      row.id !== "unscoped" &&
      /\d/.test(String(row.id).split(":").pop() || ""),
  );
  if (!pending.length) return;
  sessionStorage.setItem("alife-name-batch-shown", "1");
  $("#nameBatchIntro").textContent =
    "记忆里有 " + pending.length + " 个号码还没有当前称呼，要从聊天平台一次性查询吗？";
  $("#nameBatchStatus").textContent = "";
  $("#nameBatchGo").disabled = false;
  $("#nameBatch").showModal();
}
$("#nameBatchLater").onclick = () => $("#nameBatch").close();
$("#nameBatchNever").onclick = () => {
  localStorage.setItem("alife-name-batch", "off");
  $("#nameBatch").close();
};
let nameBatchRunning = false;
let nameBatchMode = "missing";
async function runNameBatch(mode) {
  nameBatchMode = mode === "all" ? "all" : "missing";
  const go = $("#nameBatchGo"),
    stop = $("#nameBatchStop");
  go.disabled = true;
  $("#nameBatchLater").disabled = true;
  $("#nameBatchNever").disabled = true;
  stop.classList.remove("hide");
  nameBatchRunning = true;
  let done = 0,
    updated = 0,
    skipped = 0,
    failed = 0,
    remaining = 0;
  const scope = nameBatchMode === "all" ? "全量" : "只补缺失";
  try {
    const pending = await api("/names/pending");
    const ids = (nameBatchMode === "all" ? pending.ids_all : pending.ids) || [];
    if (!ids.length) {
      $("#nameBatchStatus").textContent =
        nameBatchMode === "all" ? "没有可查询的号码。" : "没有需要补全的号码。";
      return;
    }
    for (let i = 0; i < ids.length && nameBatchRunning; i += 20) {
      $("#nameBatchStatus").textContent =
        "正在查询（" + scope + "）… 已处理 " + done + " / " + ids.length;
      const result = await api("/names/refresh-batch", {
        ids: ids.slice(i, i + 20),
        reason:
          nameBatchMode === "all"
            ? "批量确认当前QQ昵称（全量）"
            : "批量确认当前QQ昵称",
        mode: nameBatchMode,
      });
      done += Math.min(20, ids.length - i);
      updated += result.updated.length;
      skipped += (result.skipped || []).length;
      failed += result.failed.length;
      remaining = result.remaining;
    }
    $("#nameBatchStatus").textContent =
      "完成（" +
      scope +
      "）：补全 " +
      updated +
      " · 跳过（已有名字）" +
      skipped +
      " · 失败 " +
      failed +
      " · 还有 " +
      remaining +
      " 个未填";
    await loadNames();
    if (tab === "profiles") await loadFacts();
    toast("已更新 " + updated + " 个昵称");
  } finally {
    nameBatchRunning = false;
    go.disabled = false;
    $("#nameBatchLater").disabled = false;
    $("#nameBatchNever").disabled = false;
    stop.classList.add("hide");
  }
}
$("#nameBatchGo").onclick = () =>
  guard(() => {
    $("#nameBatchIntro").textContent = "正在从聊天平台批量确认当前称呼。";
    return runNameBatch("missing");
  });
$("#nameBatchManual").onclick = () =>
  guard(async () => {
    const pending = await api("/names/pending");
    $("#nameBatchAskText").textContent =
      "当前有 " +
      (pending.total || 0) +
      " 个号码还没有称呼；全库共 " +
      (pending.all_total || 0) +
      " 个可查询的号码。要拉取哪一批？";
    $("#nameBatchAsk").showModal();
  });
$("#nameBatchCancel").onclick = () => $("#nameBatchAsk").close();
function startManualNameBatch(mode) {
  $("#nameBatchAsk").close();
  $("#nameBatchIntro").textContent =
    mode === "all"
      ? "全量拉取：已有名字的也会用平台当前昵称更新，旧名保留在曾用名里。"
      : "只补没有名字的号码，已有名字的会跳过。";
  $("#nameBatchStatus").textContent = "";
  $("#nameBatchGo").disabled = false;
  $("#nameBatch").showModal();
  guard(() => runNameBatch(mode));
}
$("#nameBatchMissing").onclick = () => startManualNameBatch("missing");
$("#nameBatchAll").onclick = () => startManualNameBatch("all");
$("#nameBatchStop").onclick = () => {
  nameBatchRunning = false;
  $("#nameBatchStatus").textContent =
    "已停止；再次点击「一键拉取」或「批量拉取姓名」可继续。";
};
$("#namePrev").onclick = () =>
  guard(() => {
    nameOffset = Math.max(0, nameOffset - 100);
    return loadNames();
  });
$("#nameNext").onclick = () =>
  guard(() => {
    nameOffset += 100;
    return loadNames();
  });
$("#closeName").onclick = () => {
  $("#nameEditor").close();
  saveDraft();
};
$("#nameValue").oninput = saveDraft;
$("#nameReason").oninput = saveDraft;
$("#saveName").onclick = () =>
  guard(async () => {
    await api("/names", {
      entity_id: selectedName.id,
      name: $("#nameValue").value,
      revision: selectedName.revision,
      reason: $("#nameReason").value,
    });
    $("#nameEditor").close();
    saveDraft();
    await loadNames();
    toast("称呼已更新，曾用名已保留");
  });
$("#lookupName").onclick = () =>
  guard(async () => {
    const b = $("#lookupName");
    b.disabled = true;
    try {
      openName(
        await api("/names/refresh", {
          entity_id: selectedName.lookup_id || selectedName.id,
        }),
      );
      await loadNames();
      toast("已从聊天平台更新");
    } finally {
      b.disabled = !selectedName.lookup_id;
    }
  });
guard(boot);

function renderVersions(kind, target, revision, versions) {
  const box = $("#versionList");
  if (!box) return;
  box.innerHTML = versions.length
    ? versions
        .map(
          (v) =>
            `<div class="task"><strong>${esc(v.reason || "修改")}</strong><small>${date(v.created)}</small><button data-version="${v.id}" data-vtarget="${esc(target)}" data-vrevision="${revision}" data-vkind="${kind}">恢复此版本</button></div>`,
        )
        .join("")
    : '<p class="muted">还没有历史版本</p>';
  $$("#versionList [data-version]").forEach(
    (b) =>
      (b.onclick = () =>
        guard(async () => {
          await api("/restore", {
            kind: b.dataset.vkind,
            target: b.dataset.vtarget,
            version_id: Number(b.dataset.version),
            revision: Number(b.dataset.vrevision),
          });
          toast("已恢复该版本");
          if (b.dataset.vkind === "record") await openRecord(b.dataset.vtarget);
          else await openFact({ id: b.dataset.vtarget });
        })),
  );
}

let profileData = null;

async function openProfile(entityId) {
  profileData = null; // ★ 先清空：GET /profile 404 时不能把**上一个实体**的旧数据
                      //   留在 profileData 里（「改名字」按钮会拿它去开名称弹窗 ✗）
  await ensureNames();                       // ★ 先把名字表补上 ✓ 关系行才显示人名 ✓
  const p = await api("/profile?entity_id=" + encodeURIComponent(entityId));
  profileData = p;
  const stats = p.stats || {};
  $("#profileTitle").textContent =
    (p.entity.name || p.entity.label || "实体") + " 的画像";
  $("#profileMeta").textContent =
    p.entity.id +
    " · " +
    kindLabel(p.entity.kind) +
    " · 事实 " + (stats.facts || 0) +
    " · 关系 " + (stats.relations || 0) +
    " · 会话 " + (stats.sessions || 0) +
    (stats.last_active ? " · 最近活跃 " + date(stats.last_active) : "");
  $("#profileSummary").textContent =
    (p.summary || []).join(" · ") || "还没有画像要点";
  const groups = Object.entries(p.categories || {})
    .map(
      ([category, facts]) =>
        "<h3>" + esc(labels[category] || category) + "</h3>" +
        facts
          .map(
            (f) =>
              `<div class="task"><strong>${esc(f.content)}</strong><small>重要度 ${esc(String(f.importance))} · ${esc(f.sid)}${f.reason ? " · " + esc(f.reason) : ""}</small><button data-pf="${esc(f.id)}">编辑 / 恢复</button></div>`,
          )
          .join(""),
    )
    .join("");
  const relations = (p.relations || [])
    .map(
      (r) =>
        `<div class="task" title="${esc(r.subject)} → ${esc(r.object)}"><strong>${esc(nameOf(r.subject))} —${esc(r.predicate)}→ ${esc(nameOf(r.object))}</strong></div>`,
    )
    .join("");
  const names = (p.entity.history || [])
    .map(
      (h) =>
        `<div class="task"><strong>${esc(h.name)}</strong><small>${date(h.observed)} · ${esc(h.source)}</small>${h.name === p.entity.name ? "" : `<button data-pn="${esc(h.name)}">设为当前名</button>`}</div>`,
    )
    .join("");
  $("#profileBody").innerHTML =
    groups +
    "<h3>关系</h3>" +
    (relations || '<p class="muted">暂无关系连线</p>') +
    "<h3>名字历史</h3>" +
    (names || '<p class="muted">暂无名字记录</p>');
  $$("#profileBody [data-pf]").forEach(
    (b) =>
      (b.onclick = () =>
        guard(async () => {
          const fact = Object.values(p.categories || {})
            .flat()
            .find((f) => f.id === b.dataset.pf);
          $("#profileDialog").close();
          if (fact) await openFact(fact);
        })),
  );
  $$("#profileBody [data-pn]").forEach(
    (b) =>
      (b.onclick = () =>
        guard(async () => {
          await api("/names", {
            entity_id: p.entity.id,
            name: b.dataset.pn,
            revision: p.entity.revision,
            reason: "恢复到曾用名",
          });
          toast("已切换当前称呼");
          await openProfile(p.entity.id);
        })),
  );
  if (!$("#profileDialog").open) $("#profileDialog").showModal();
}
$("#closeProfile").onclick = () => $("#profileDialog").close();
$("#profileEditName").onclick = () =>
  guard(() => {
    if (!profileData) return;
    $("#profileDialog").close();
    openName({ ...profileData.entity, identity_note: "" });
  });

// 身份绑定面板的事件（委托 ✓ 面板是动态渲染的 ✓）
document.addEventListener("click", (ev) => {
  const t = ev.target;
  if (!(t instanceof HTMLElement) || t.tagName !== "BUTTON") return;
  const impId = t.getAttribute("data-imp");
  if (impId) {
    const delta = Number(t.getAttribute("data-delta") || 0);
    const card = t.closest(".card");
    const m = card && card.querySelector(".muted");
    const cur = m ? Number((m.textContent.match(/重要度 (\d+)/) || [])[1] || 5) : 5;
    const next = Math.max(1, Math.min(10, cur + delta));
    api("/edit", { kind: "fact", target: impId, patch: { importance: next }, reason: "WebUI 体检：调整重要度" })
      .then(() => {
        toast("重要度已改为 " + next);
        loadHealth();
      })
      .catch((e) => toast("修改失败：" + e.message));
    return;
  }
  const delId = t.getAttribute("data-del");
  if (delId) {
    api("/edit", { kind: "fact", target: delId, patch: { deleted: true }, reason: "WebUI 体检：删除事实" })
      .then(() => {
        toast("已移入回收站（可恢复）");
        loadHealth();
      })
      .catch((e) => toast("删除失败：" + e.message));
  }
});

/* ── 冷归档（P7）面板动作 ─────────────────────────────────────────────
   四个按钮对应 GET /cold?action=stats|preview|spill|restore
   · preview 只读 ✓ · spill/restore 未启用时后端会直接拒绝 ✓
   · 所有结果都写在 #coldMsg 里（不弹窗 ✓ 失败也只提示，不影响页面其它部分 ✓）
*/
async function coldAction(action) {
  const msg = $("#coldMsg");
  if (!msg) return;
  const mb = (n) => ((Number(n) || 0) / 1048576).toFixed(1);
  msg.textContent = "处理中…";
  try {
    const data = await api("/cold?action=" + action);
    if (action === "stats") {
      const s = data.stats || {};
      msg.textContent = data.enabled
        ? "冷归档：已启用 · 冷库 " + (s.records || 0) + " 条 / " + mb(s.bytes) + " MB"
        : "冷归档：未启用 —— 在「偏好设置」里打开「冷归档」即可启用";
    } else if (action === "preview") {
      msg.textContent = "可搬 " + (data.would_move || 0) + " 条，预计释放 " + mb(data.would_free_bytes) + " MB";
    } else if (action === "spill") {
      msg.textContent = data.ok
        ? "已搬 " + (data.moved || 0) + " 条，释放约 " + mb(data.bytes) + " MB"
        : "未执行：" + (data.error || "未知原因");
      if (data.ok && typeof loadTrash === "function") await loadTrash();
    } else if (action === "restore") {
      msg.textContent = data.ok ? "已取回 " + (data.restored || 0) + " 条" : "未执行：" + (data.error || "未知原因");
      if (data.ok && typeof loadTrash === "function") await loadTrash();
    }
  } catch (error) {
    msg.textContent = "请求失败：" + error.message;
  }
}

["coldStats", "coldPreview", "coldSpill", "coldRestore"].forEach((id) => {
  const el = document.getElementById(id);
  if (el) {
    el.addEventListener("click", () =>
      coldAction(id.replace("cold", "").toLowerCase()),
    );
  }
});

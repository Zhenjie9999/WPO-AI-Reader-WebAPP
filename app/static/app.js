const invitePanel = document.querySelector("#invitePanel");
const aiPanel = document.querySelector("#aiPanel");
const loginPanel = document.querySelector("#loginPanel");
const reportPanel = document.querySelector("#reportPanel");
const inviteCodeInput = document.querySelector("#inviteCodeInput");
const inviteButton = document.querySelector("#inviteButton");
const apiPresetSelect = document.querySelector("#apiPresetSelect");
const aiBaseUrlInput = document.querySelector("#aiBaseUrlInput");
const aiModelInput = document.querySelector("#aiModelInput");
const aiApiKeyInput = document.querySelector("#aiApiKeyInput");
const saveAiButton = document.querySelector("#saveAiButton");
const clearAiButton = document.querySelector("#clearAiButton");
const aiStatusText = document.querySelector("#aiStatusText");
const emailInput = document.querySelector("#emailInput");
const passwordInput = document.querySelector("#passwordInput");
const loginButton = document.querySelector("#loginButton");
const reportSetSelect = document.querySelector("#reportSetSelect");
const enterButton = document.querySelector("#enterButton");
const categorySelect = document.querySelector("#categorySelect");
const loadCategoryButton = document.querySelector("#loadCategoryButton");
const reportSelect = document.querySelector("#reportSelect");
const allKpisInput = document.querySelector("#allKpisInput");
const refreshButton = document.querySelector("#refreshButton");
const checkButton = document.querySelector("#checkButton");
const copyAnswerButton = document.querySelector("#copyAnswerButton");
const downloadCsvButton = document.querySelector("#downloadCsvButton");
const statusText = document.querySelector("#statusText");
const aiText = document.querySelector("#aiText");
const messages = document.querySelector("#messages");
const askForm = document.querySelector("#askForm");
const questionInput = document.querySelector("#questionInput");
const progressBoard = document.querySelector("#progressBoard");
const progressTitle = document.querySelector("#progressTitle");
const progressElapsed = document.querySelector("#progressElapsed");
const progressList = document.querySelector("#progressList");

const ACCESS_STORAGE_KEY = "wpo-access-token";
const AI_STORAGE_KEY = "wpo-ai-configuration";
const AI_PRESETS = {
  custom: { label: "Custom", baseUrl: "", model: "" },
  openai: {
    label: "OpenAI compatible",
    baseUrl: "https://api.openai.com/v1/chat/completions",
    model: "gpt-4.1",
  },
  deepseek: {
    label: "DeepSeek",
    baseUrl: "https://api.deepseek.com/chat/completions",
    model: "deepseek-chat",
  },
  doubao: {
    label: "Doubao",
    baseUrl: "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    model: "doubao-seed-1-6-250615",
  },
};

let accessToken = localStorage.getItem(ACCESS_STORAGE_KEY) || null;
let sessionId = null;
let pendingQuestion = null;
let pendingClarification = null;
let latestAnswerText = "";
let progressPollTimer = null;
let progressClockTimer = null;
let progressStartedAt = null;

function setStatus(text) {
  statusText.textContent = text;
}

function startProgress(title, steps = []) {
  progressStartedAt = Date.now();
  progressBoard.classList.remove("idle", "complete", "error");
  progressBoard.classList.add("active");
  progressTitle.textContent = title;
  renderProgressEvents(steps.length ? steps : [{ status: "running", message: title }]);
  updateProgressClock();
  if (progressClockTimer) clearInterval(progressClockTimer);
  progressClockTimer = setInterval(updateProgressClock, 1000);
  startProgressPolling();
}

function finishProgress(status = "done", message = "完成") {
  renderProgressEvents([{ status, message }], true);
  progressBoard.classList.remove("active", "idle");
  progressBoard.classList.add(status === "error" ? "error" : "complete");
  stopProgressPolling();
}

function renderProgressEvents(events, append = false) {
  const existing = append
    ? [...progressList.querySelectorAll("li")].map((item) => ({
        status: item.dataset.status || "running",
        message: item.querySelector(".progress-message")?.textContent || item.textContent,
      }))
    : [];
  const merged = [...existing, ...events].slice(-8);
  progressList.innerHTML = "";
  for (const event of merged) {
    const item = document.createElement("li");
    item.dataset.status = event.status || "running";
    const dot = document.createElement("span");
    dot.className = "progress-dot";
    const message = document.createElement("span");
    message.className = "progress-message";
    message.textContent = event.message || "Working...";
    item.appendChild(dot);
    item.appendChild(message);
    progressList.appendChild(item);
  }
}

function updateProgressClock() {
  if (!progressStartedAt) {
    progressElapsed.textContent = "00:00";
    return;
  }
  const seconds = Math.max(0, Math.floor((Date.now() - progressStartedAt) / 1000));
  const minutes = String(Math.floor(seconds / 60)).padStart(2, "0");
  const rest = String(seconds % 60).padStart(2, "0");
  progressElapsed.textContent = `${minutes}:${rest}`;
}

function startProgressPolling() {
  if (!sessionId || progressPollTimer) return;
  progressPollTimer = setInterval(loadProgress, 1200);
  loadProgress().catch(() => {});
}

function stopProgressPolling() {
  if (progressPollTimer) clearInterval(progressPollTimer);
  progressPollTimer = null;
  if (progressClockTimer) clearInterval(progressClockTimer);
  progressClockTimer = null;
}

async function loadProgress() {
  if (!sessionId) return;
  const result = await requestJson(`/api/sessions/${sessionId}/progress`);
  const events = result.events || [];
  if (events.length) {
    progressTitle.textContent = result.current || "Working...";
    renderProgressEvents(events);
    progressBoard.classList.remove("idle");
    progressBoard.classList.toggle("active", result.active);
  }
  if (!result.active && events.length) {
    const last = events[events.length - 1];
    progressBoard.classList.remove("active");
    progressBoard.classList.add(last.status === "error" ? "error" : "complete");
    stopProgressPolling();
  }
}

function unlock(panel) {
  panel.classList.remove("locked");
}

function addMessage(role, text) {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  node.textContent = text;
  messages.appendChild(node);
  messages.scrollTop = messages.scrollHeight;
  return node;
}

function rememberAnswer(text) {
  latestAnswerText = text;
  copyAnswerButton.disabled = !text;
}

function fillSelect(select, options, getValue = (item) => item, getLabel = (item) => item) {
  select.innerHTML = "";
  for (const option of options || []) {
    const node = document.createElement("option");
    node.value = getValue(option);
    node.textContent = getLabel(option);
    select.appendChild(node);
  }
}

function selectedReport() {
  const option = reportSelect.selectedOptions[0];
  if (!option) return null;
  return { parameter: option.value, title: option.textContent };
}

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(payload.detail || "请求失败");
  }
  return payload;
}

async function postJson(url, payload) {
  return requestJson(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
}

function currentAiConfiguration() {
  return {
    base_url: aiBaseUrlInput.value.trim(),
    model: aiModelInput.value.trim(),
    api_key: aiApiKeyInput.value,
    provider: apiPresetSelect.value || "custom",
    access_token: accessToken,
  };
}

function restoreAiConfiguration() {
  try {
    const saved = JSON.parse(localStorage.getItem(AI_STORAGE_KEY) || "null");
    if (!saved) return;
    apiPresetSelect.value = saved.provider || "custom";
    aiBaseUrlInput.value = saved.base_url || "";
    aiModelInput.value = saved.model || "";
    aiApiKeyInput.value = saved.api_key || "";
    aiStatusText.textContent = "已恢复浏览器保存的 AI 配置";
  } catch {
    localStorage.removeItem(AI_STORAGE_KEY);
  }
}

function applyPreset(presetName) {
  const preset = AI_PRESETS[presetName];
  if (!preset) return;
  if (preset.baseUrl) aiBaseUrlInput.value = preset.baseUrl;
  if (preset.model) aiModelInput.value = preset.model;
}

async function bindSavedAiConfiguration() {
  if (!sessionId || !aiApiKeyInput.value) return;
  const result = await requestJson(`/api/sessions/${sessionId}/ai`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(currentAiConfiguration()),
  });
  aiStatusText.textContent = `AI 可用：${result.ai.provider} / ${result.ai.model}`;
  aiText.textContent = `AI：${result.ai.provider} / ${result.ai.model}`;
}

inviteButton.addEventListener("click", async () => {
  inviteButton.disabled = true;
  startProgress("Invite access", [{ status: "running", message: "Checking invite code" }]);
  setStatus("正在验证邀请码...");
  try {
    const result = await postJson("/api/access", { invite_code: inviteCodeInput.value.trim() });
    accessToken = result.access_token;
    localStorage.setItem(ACCESS_STORAGE_KEY, accessToken);
    unlock(aiPanel);
    unlock(loginPanel);
    finishProgress("done", "Invite accepted");
    setStatus("邀请码已通过，请配置 AI 并登录 Worldpanel");
    addMessage("assistant", "欢迎使用 WPO AI Reader。请先配置你的 AI API，然后登录 Worldpanel Online。");
  } catch (error) {
    finishProgress("error", error.message);
    setStatus(error.message);
  } finally {
    inviteButton.disabled = false;
  }
});

apiPresetSelect.addEventListener("change", () => applyPreset(apiPresetSelect.value));

saveAiButton.addEventListener("click", async () => {
  if (!accessToken) {
    setStatus("请先输入邀请码。");
    return;
  }
  saveAiButton.disabled = true;
  aiStatusText.textContent = "正在测试 AI 服务...";
  try {
    const configuration = currentAiConfiguration();
    const result = await postJson("/api/ai/test", configuration);
    localStorage.setItem(AI_STORAGE_KEY, JSON.stringify(configuration));
    if (sessionId) await bindSavedAiConfiguration();
    aiStatusText.textContent = `AI 可用：${result.ai.provider} / ${result.ai.model}`;
    aiText.textContent = `AI：${result.ai.provider} / ${result.ai.model}`;
  } catch (error) {
    aiStatusText.textContent = `AI 测试失败：${error.message}`;
  } finally {
    saveAiButton.disabled = false;
  }
});

clearAiButton.addEventListener("click", async () => {
  localStorage.removeItem(AI_STORAGE_KEY);
  apiPresetSelect.value = "custom";
  aiBaseUrlInput.value = "";
  aiModelInput.value = "";
  aiApiKeyInput.value = "";
  if (sessionId) {
    await requestJson(`/api/sessions/${sessionId}/ai`, { method: "DELETE" });
  }
  aiStatusText.textContent = "AI 未配置";
  aiText.textContent = "AI 未配置";
});

loginButton.addEventListener("click", async () => {
  const email = emailInput.value.trim();
  const password = passwordInput.value;
  if (!accessToken) {
    setStatus("请先输入邀请码。");
    return;
  }
  if (!email || !password) {
    setStatus("请填写 Worldpanel 账号和密码。");
    return;
  }

  loginButton.disabled = true;
  startProgress("Worldpanel login", [
    { status: "running", message: "Submitting credentials" },
    { status: "running", message: "Reading Report Set list" },
  ]);
  setStatus("正在登录 Worldpanel...");
  try {
    const result = await postJson("/api/login", { email, password, access_token: accessToken });
    sessionId = result.session_id;
    fillSelect(reportSetSelect, result.report_sets);
    reportSetSelect.value = result.current;
    enterButton.disabled = false;
    unlock(reportPanel);
    finishProgress("done", `Loaded ${result.report_sets.length} Report Sets`);
    setStatus(`登录成功，读取到 ${result.report_sets.length} 个 Report Set`);
    addMessage("assistant", "请选择 Report Set，然后进入 Ready-to-Use Reports。");
    bindSavedAiConfiguration().catch((error) => {
      aiStatusText.textContent = `AI 绑定失败：${error.message}`;
    });
  } catch (error) {
    finishProgress("error", error.message);
    setStatus(error.message);
    addMessage("assistant", `登录失败：${error.message}`);
  } finally {
    loginButton.disabled = false;
  }
});

enterButton.addEventListener("click", async () => {
  if (!sessionId) return;
  enterButton.disabled = true;
  startProgress("Ready-to-Use reports", [
    { status: "running", message: "Opening selected Report Set" },
    { status: "running", message: "Reading report catalog" },
  ]);
  setStatus("正在进入 Report 并读取 Ready-to-Use Reports...");
  try {
    const result = await postJson("/api/ready-to-use", {
      session_id: sessionId,
      report_set: reportSetSelect.value,
    });
    renderReadyToUse(result);
    finishProgress("done", `Loaded ${result.reports.length} reports`);
    setStatus(`已进入 ${result.report_set}，当前分类：${result.current_category}`);
    addMessage("assistant", "请选择 Ready-to-Use 分类和 Data Explorer 报表，然后点击准备所选报表。");
  } catch (error) {
    finishProgress("error", error.message);
    setStatus(error.message);
    addMessage("assistant", `进入 Report 失败：${error.message}`);
  } finally {
    enterButton.disabled = false;
  }
});

loadCategoryButton.addEventListener("click", async () => {
  if (!sessionId) return;
  loadCategoryButton.disabled = true;
  setStatus("正在应用 Ready-to-Use 分类...");
  try {
    const result = await postJson("/api/ready-to-use", {
      session_id: sessionId,
      report_set: reportSetSelect.value,
      category: categorySelect.value,
    });
    renderReadyToUse(result);
    setStatus(`分类已切换到：${result.current_category}`);
  } catch (error) {
    setStatus(error.message);
    addMessage("assistant", `分类筛选失败：${error.message}`);
  } finally {
    loadCategoryButton.disabled = false;
  }
});

refreshButton.addEventListener("click", async () => {
  const report = selectedReport();
  if (!sessionId || !report) return;

  refreshButton.disabled = true;
  startProgress("Prepare Data Explorer", [
    { status: "running", message: "Opening selected report" },
    { status: "running", message: "Discovering Data Explorer controls" },
    { status: "running", message: allKpisInput.checked ? "Reading KPI tables" : "Reading current KPI table" },
  ]);
  setStatus("正在准备 Data Explorer 上下文...");
  try {
    const result = await postJson("/api/refresh", {
      session_id: sessionId,
      report_set: reportSetSelect.value,
      report_parameter: report.parameter,
      report_name: report.title,
      all_kpis: allKpisInput.checked,
    });
    const metricText = result.metrics?.length ? `${result.metrics.length} 个 KPI` : "当前 KPI";
    setStatus(`报表已准备：${result.products.length} 个产品，${result.dates.length} 个日期，${metricText}`);
    checkButton.disabled = false;
    finishProgress("done", `Prepared ${result.products.length} products and ${result.dates.length} dates`);
    downloadCsvButton.disabled = false;
    const dimensions = result.context?.dimensions ? Object.keys(result.context.dimensions).join("、") : "待识别";
    const segmentCount = result.context?.segments?.length || 0;
    addMessage(
      "assistant",
      `报表已准备：${result.report.report_set} / ${result.report.report_name}\n可操作维度：${dimensions}\nPivot segment：${segmentCount} 个`,
    );
  } catch (error) {
    finishProgress("error", error.message);
    setStatus(error.message);
    addMessage("assistant", `准备报表失败：${error.message}`);
  } finally {
    refreshButton.disabled = false;
  }
});

checkButton.addEventListener("click", async () => {
  checkButton.disabled = true;
  try {
    const result = await postJson("/api/check", { session_id: sessionId });
    const issueLines = result.issues
      .slice(0, 8)
      .map((issue) => `- [${issue.severity}] ${issue.message}`)
      .join("\n");
    const text = issueLines ? `${result.summary}\n\n${issueLines}` : result.summary;
    addMessage("assistant", text);
    rememberAnswer(text);
  } catch (error) {
    addMessage("assistant", `检查失败：${error.message}`);
  } finally {
    checkButton.disabled = false;
  }
});

copyAnswerButton.addEventListener("click", async () => {
  if (!latestAnswerText) return;
  await navigator.clipboard.writeText(latestAnswerText);
  setStatus("答案已复制。");
});

downloadCsvButton.addEventListener("click", () => {
  if (!sessionId) return;
  window.open(`/api/export.csv?session_id=${encodeURIComponent(sessionId)}`, "_blank");
});

askForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question) return;
  questionInput.value = "";
  if (pendingClarification) {
    await submitClarification(pendingClarification.dimensionKey, question, pendingClarification.question);
    return;
  }
  pendingQuestion = question;
  addMessage("user", question);
  await submitAsk({ question });
});

async function submitClarification(dimensionKey, value, questionOverride = null) {
  const question = questionOverride || pendingClarification?.question || pendingQuestion;
  if (!question) {
    addMessage("assistant", "请选择后重新输入完整问题。");
    return;
  }
  pendingQuestion = question;
  addMessage("user", value);
  setStatus("正在应用你补充的筛选条件...");
  await submitAsk({
    question,
    clarification: {
      dimension_key: dimensionKey,
      value,
    },
  });
}

async function submitAsk(payload) {
  try {
    if (!sessionId) {
      addMessage("assistant", "请先登录并准备一个 Data Explorer 报表。");
      return;
    }
    if (!payload.clarification) {
      const handled = await submitPivotAsk(payload.question);
      if (handled) return;
    }
    const result = await postJson("/api/ask", {
      session_id: sessionId,
      ...payload,
    });
    if (result.needs_clarification) {
      pendingQuestion = payload.question || pendingQuestion;
      pendingClarification = {
        question: pendingQuestion,
        dimensionKey: result.clarification.dimension_key,
      };
      addClarification(result.clarification);
      return;
    }
    pendingQuestion = null;
    pendingClarification = null;
    const cacheText = result.cache_hit ? "\n（来自缓存）" : "";
    const filterText = result.filters ? `\n\n筛选口径：${formatFilters(result.filters)}` : "";
    const text = `${result.answer}${cacheText}${filterText}`;
    addMessage("assistant", text);
    rememberAnswer(text);
    downloadCsvButton.disabled = false;
  } catch (error) {
    finishProgress("error", error.message);
    addMessage("assistant", error.message);
  }
}

async function submitPivotAsk(question, clarification = null) {
  startProgress("Natural language query", [
    { status: "running", message: clarification ? "Applying your clarification" : "Planning Pivot Screen operations" },
  ]);
  const planned = await postJson("/api/pivot/plan", {
    session_id: sessionId,
    question,
    clarification,
  });
  if (planned.needs_clarification) {
    finishProgress("waiting", "Waiting for your clarification");
    addPivotClarification(question, planned.clarification);
    return true;
  }
  startProgress("Execute Pivot query", [
    { status: "running", message: "Applying Pivot layout and member selections" },
    { status: "running", message: "Reading KPI table from rendered report" },
    { status: "running", message: "Parsing values and verifying receipt" },
  ]);
  const executed = await postJson("/api/pivot/execute", {
    session_id: sessionId,
    question,
    plan: planned.plan,
  });
  const receipt = executed.receipt;
  const receiptText = [
    `Row: ${(receipt.row_dimensions || []).join(" / ") || "-"}`,
    `Column: ${(receipt.column_dimensions || []).join(" / ") || "-"}`,
    `KPI: ${(receipt.kpis || []).join(" / ") || "-"}`,
    `Period: ${receipt.period || "-"}`,
    `Verified: ${receipt.verified ? "yes" : "no"}`,
    `Cache: ${receipt.cache_hit ? "hit" : "fresh"}`,
  ].join("\n");
  const answerText =
    executed.answer ||
    (executed.answer_error ? `Pivot 已应用，但读取答案失败：${executed.answer_error}` : "Pivot 已应用，表格已刷新。");
  const text = `${answerText}\n\n执行凭证：\n${receiptText}`;
  addMessage("assistant", text);
  rememberAnswer(text);
  finishProgress("done", "Data pull completed");
  downloadCsvButton.disabled = false;
  pendingQuestion = null;
  pendingClarification = null;
  return true;
}

function addClarification(clarification) {
  const question = pendingQuestion || pendingClarification?.question || "";
  pendingClarification = {
    question,
    dimensionKey: clarification.dimension_key,
  };
  const node = addMessage("assistant", clarification.question);
  const actions = document.createElement("div");
  actions.className = "choice-row";
  for (const option of clarification.options) {
    const button = document.createElement("button");
    button.className = "choice";
    button.type = "button";
    button.textContent = option.label;
    button.addEventListener("click", () => {
      for (const choice of actions.querySelectorAll("button")) choice.disabled = true;
      submitClarification(clarification.dimension_key, option.label, question);
    });
    actions.appendChild(button);
  }
  node.appendChild(actions);
}

function addPivotClarification(question, clarification) {
  const node = addMessage("assistant", clarification.question);
  const actions = document.createElement("div");
  actions.className = "choice-row";
  for (const option of clarification.options) {
    const button = document.createElement("button");
    button.className = "choice";
    button.type = "button";
    button.textContent = option.label;
    button.addEventListener("click", async () => {
      for (const choice of actions.querySelectorAll("button")) choice.disabled = true;
      addMessage("user", option.label);
      setStatus("正在按你选择的成员路径执行查询...");
      try {
        await submitPivotAsk(question, {
          dimension: clarification.dimension_key,
          member_path: option.value,
        });
      } catch (error) {
        addMessage("assistant", error.message);
      }
    });
    actions.appendChild(button);
  }
  node.appendChild(actions);
}

function formatFilters(filters) {
  const pieces = [];
  if (filters.products?.length) pieces.push(`产品 ${filters.products.join("/")}`);
  if (filters.metrics?.length) pieces.push(`KPI ${filters.metrics.join("/")}`);
  if (filters.year) pieces.push(filters.full_year ? `${filters.year} 全年` : `${filters.year}`);
  for (const [key, value] of Object.entries(filters.dimensions || {})) {
    pieces.push(`${key}=${value}`);
  }
  return pieces.join("；") || "当前 Data Explorer 选择";
}

function renderReadyToUse(result) {
  fillSelect(categorySelect, result.categories);
  categorySelect.value = result.current_category;
  fillSelect(
    reportSelect,
    result.reports,
    (report) => report.parameter,
    (report) => report.title,
  );
  loadCategoryButton.disabled = result.categories.length === 0;
  refreshButton.disabled = result.reports.length === 0;
}

async function loadHealth() {
  try {
    const health = await requestJson("/api/health");
    aiText.textContent = health.ai.enabled ? `AI：${health.ai.provider} / ${health.ai.model}` : "AI 未配置";
  } catch (error) {
    setStatus(error.message);
  }
}

if (accessToken) {
  unlock(aiPanel);
  unlock(loginPanel);
  setStatus("已恢复邀请码访问，请继续配置或登录。");
} else {
  setStatus("等待邀请码");
}

restoreAiConfiguration();
loadHealth();
addMessage("assistant", "请输入邀请码开始试用。");

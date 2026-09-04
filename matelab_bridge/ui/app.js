"use strict";

const csrf = document.querySelector('meta[name="bridge-csrf"]').content;
const matelabUrl = document.querySelector('meta[name="matelab-url"]').content;
const byId = (id) => document.getElementById(id);

const elements = {
  pill: byId("session-pill"),
  sessionLabel: byId("session-label"),
  loginForm: byId("login-form"),
  loginButton: byId("login-button"),
  username: byId("username"),
  password: byId("password"),
  loggedIn: byId("logged-in-panel"),
  logout: byId("logout-button"),
  notebook: byId("notebook"),
  refresh: byId("refresh-notebooks"),
  recordForm: byId("record-form"),
  title: byId("record-title"),
  content: byId("record-content"),
  actor: byId("actor-id"),
  files: byId("attachments"),
  dropZone: byId("drop-zone"),
  fileList: byId("file-list"),
  submit: byId("submit-button"),
  error: byId("error-panel"),
  errorTitle: byId("error-title"),
  errorMessage: byId("error-message"),
  errorAction: byId("error-action"),
  errorDetails: byId("error-details"),
  progress: byId("progress-card"),
  progressTitle: byId("progress-title"),
  progressMessage: byId("progress-message"),
  captureId: byId("capture-id"),
  recordLink: byId("record-link"),
};

let selectedFiles = [];
let authenticated = false;
let pollTimer = null;

class ApiProblem extends Error {
  constructor(problem, status) {
    super(problem.message || `请求失败（HTTP ${status}）`);
    this.problem = problem;
    this.status = status;
  }
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Bridge-UI-CSRF", csrf);
  headers.set("Accept", "application/json");
  if (options.body && !(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }
  let response;
  try {
    response = await fetch(path, { ...options, headers });
  } catch (error) {
    throw new ApiProblem({
      code: "bridge_unreachable",
      message: "浏览器无法连接本地 Connector 服务。",
      action: "确认启动窗口仍在运行，然后刷新本页面。",
      retryable: true,
      details: String(error),
    }, 0);
  }
  let data = {};
  try {
    data = await response.json();
  } catch (_error) {
    data = {};
  }
  if (!response.ok) {
    throw new ApiProblem(data.error || {
      code: "unexpected_response",
      message: `请求失败（HTTP ${response.status}）。`,
      action: "刷新页面后重试。",
      retryable: false,
    }, response.status);
  }
  return data;
}

function showError(error) {
  const problem = error instanceof ApiProblem ? error.problem : {
    code: "ui_error",
    message: error.message || String(error),
    action: "刷新页面后重试。",
    retryable: false,
  };
  elements.errorTitle.textContent = problem.code ? `操作失败 · ${problem.code}` : "操作失败";
  elements.errorMessage.textContent = problem.message || "发生未知错误。";
  elements.errorAction.textContent = problem.action ? `建议：${problem.action}` : "";
  elements.errorDetails.textContent = JSON.stringify({
    request_id: problem.request_id,
    retryable: problem.retryable,
    details: problem.details,
  }, null, 2);
  elements.error.classList.remove("hidden");
  elements.error.scrollIntoView({ behavior: "smooth", block: "center" });
  if (problem.code === "matelab_auth_required") {
    setAuthenticated(false, "登录已失效");
  }
}

function hideError() {
  elements.error.classList.add("hidden");
}

function setBusy(button, busy, busyText) {
  if (!button.dataset.normalText) {
    button.dataset.normalText = button.querySelector("span")?.textContent || button.textContent;
  }
  const label = button.querySelector("span");
  if (label) label.textContent = busy ? busyText : button.dataset.normalText;
  button.disabled = busy;
  button.classList.toggle("busy", busy);
}

function setAuthenticated(value, label = null) {
  authenticated = value;
  elements.pill.classList.toggle("status-online", value);
  elements.pill.classList.toggle("status-offline", !value);
  elements.sessionLabel.textContent = label || (value ? "MatElab 已登录" : "MatElab 未登录");
  elements.loginForm.classList.toggle("hidden", value);
  elements.loggedIn.classList.toggle("hidden", !value);
  elements.notebook.disabled = !value;
  elements.refresh.disabled = !value;
  elements.submit.disabled = !value || !elements.notebook.value;
  if (!value) {
    elements.notebook.innerHTML = '<option value="">请先登录 MatElab</option>';
  }
}

function renderNotebooks(notebooks) {
  const previous = elements.notebook.value;
  elements.notebook.replaceChildren();
  if (!notebooks.length) {
    const option = document.createElement("option");
    option.textContent = "没有可用记录本";
    option.value = "";
    elements.notebook.append(option);
  } else {
    notebooks.forEach((notebook) => {
      const option = document.createElement("option");
      option.value = notebook.id;
      option.dataset.name = notebook.name;
      option.textContent = `${notebook.name}${notebook.owner ? "" : " · 协作"}`;
      elements.notebook.append(option);
    });
    if ([...elements.notebook.options].some((option) => option.value === previous)) {
      elements.notebook.value = previous;
    }
  }
  elements.submit.disabled = !authenticated || !elements.notebook.value;
}

async function refreshNotebooks() {
  setBusy(elements.refresh, true, "↻");
  try {
    const data = await api("/v1/ui/notebooks");
    renderNotebooks(data.notebooks || []);
  } catch (error) {
    showError(error);
  } finally {
    setBusy(elements.refresh, false, "↻");
  }
}

function formatBytes(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function renderFiles() {
  elements.fileList.replaceChildren();
  selectedFiles.forEach((file, index) => {
    const item = document.createElement("li");
    const name = document.createElement("strong");
    name.textContent = file.name;
    const size = document.createElement("span");
    size.textContent = formatBytes(file.size);
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "移除";
    remove.addEventListener("click", () => {
      selectedFiles.splice(index, 1);
      renderFiles();
    });
    item.append(name, size, remove);
    elements.fileList.append(item);
  });
}

function addFiles(fileList) {
  const incoming = [...fileList];
  incoming.forEach((file) => {
    const duplicate = selectedFiles.some((item) =>
      item.name === file.name && item.size === file.size && item.lastModified === file.lastModified);
    if (!duplicate) selectedFiles.push(file);
  });
  renderFiles();
}

const stateStep = {
  receiving: 0,
  validating: 0,
  accepted: 0,
  ready: 0,
  uploading_files: 1,
  retry_wait: 1,
  creating_record: 2,
  populating_record: 2,
  verifying_export: 3,
  matelab_verified: 3,
  complete: 4,
  auth_required: 1,
  needs_attention: 2,
};

const stateMessage = {
  ready: "本机已安全接收，等待后台同步。",
  uploading_files: "正在将附件串行上传到 MatElab。",
  creating_record: "正在所选记录本中创建记录。",
  populating_record: "正在写入正文、附件链接和追踪信息。",
  verifying_export: "正在从 MatElab 回读并核验记录。",
  matelab_verified: "MatElab 回读核验通过，正在保存最终回执。",
  complete: "提交完成，MatElab 记录已通过回读核验。",
  retry_wait: "网络或 MatElab 暂时不可用，后台会自动重试。",
  auth_required: "登录已失效。内容已安全保存在本机，重新登录后可以继续。",
  needs_attention: "同步遇到无法自动处理的问题，请查看错误详情。",
};

function renderProgress(receipt) {
  const step = stateStep[receipt.state] ?? 0;
  const failed = ["auth_required", "needs_attention"].includes(receipt.state);
  document.querySelectorAll(".progress-steps li").forEach((item, index) => {
    item.classList.toggle("done", !failed && index < step);
    item.classList.toggle("active", !failed && index === step);
    item.classList.toggle("failed", failed && index === step);
  });
  elements.progressTitle.textContent = receipt.state === "complete" ? "记录上传完成" : "正在提交记录";
  elements.progressMessage.textContent = stateMessage[receipt.state] || `当前状态：${receipt.state}`;
  if (receipt.state === "complete") {
    elements.recordLink.classList.remove("hidden");
  }
  if (failed && receipt.last_error) {
    showError(new ApiProblem({
      code: receipt.last_error.code || receipt.state,
      message: stateMessage[receipt.state],
      action: receipt.state === "auth_required" ? "重新登录后，任务会保留在本机。" : "运行诊断并检查 MatElab 中的目标记录。",
      retryable: receipt.state === "auth_required",
      details: receipt.last_error,
    }, 409));
  }
  return receipt.state === "complete" || failed;
}

async function pollCapture(captureId) {
  window.clearTimeout(pollTimer);
  try {
    const receipt = await api(`/v1/ui/captures/${encodeURIComponent(captureId)}`);
    if (!renderProgress(receipt)) {
      pollTimer = window.setTimeout(() => pollCapture(captureId), 1000);
    }
  } catch (error) {
    showError(error);
    pollTimer = window.setTimeout(() => pollCapture(captureId), 3000);
  }
}

elements.loginForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideError();
  setBusy(elements.loginButton, true, "正在登录…");
  try {
    const data = await api("/v1/ui/login", {
      method: "POST",
      body: JSON.stringify({
        username: elements.username.value.trim(),
        password: elements.password.value,
      }),
    });
    elements.password.value = "";
    setAuthenticated(Boolean(data.session?.authenticated));
    renderNotebooks(data.notebooks || []);
  } catch (error) {
    elements.password.value = "";
    setAuthenticated(false, "登录失败");
    showError(error);
  } finally {
    setBusy(elements.loginButton, false, "正在登录…");
  }
});

elements.logout.addEventListener("click", async () => {
  hideError();
  try {
    await api("/v1/ui/logout", { method: "POST" });
    setAuthenticated(false);
  } catch (error) {
    showError(error);
  }
});

elements.refresh.addEventListener("click", refreshNotebooks);
elements.notebook.addEventListener("change", () => {
  elements.submit.disabled = !authenticated || !elements.notebook.value;
});
elements.dropZone.addEventListener("click", () => elements.files.click());
elements.dropZone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    elements.files.click();
  }
});
elements.files.addEventListener("change", () => addFiles(elements.files.files));
["dragenter", "dragover"].forEach((name) => elements.dropZone.addEventListener(name, (event) => {
  event.preventDefault();
  elements.dropZone.classList.add("dragging");
}));
["dragleave", "drop"].forEach((name) => elements.dropZone.addEventListener(name, (event) => {
  event.preventDefault();
  elements.dropZone.classList.remove("dragging");
}));
elements.dropZone.addEventListener("drop", (event) => addFiles(event.dataTransfer.files));

elements.recordForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  hideError();
  const selected = elements.notebook.selectedOptions[0];
  if (!selected?.value) return;
  const form = new FormData();
  form.set("title", elements.title.value.trim());
  form.set("content", elements.content.value);
  form.set("notebook_id", selected.value);
  form.set("notebook_name", selected.dataset.name);
  if (elements.actor.value.trim()) form.set("actor_id", elements.actor.value.trim());
  selectedFiles.forEach((file) => form.append("attachments", file, file.name));
  setBusy(elements.submit, true, "正在接收并校验…");
  try {
    const result = await api("/v1/ui/manual-submissions", { method: "POST", body: form });
    elements.progress.classList.remove("hidden");
    elements.captureId.textContent = result.capture_id;
    elements.recordLink.classList.add("hidden");
    elements.progress.scrollIntoView({ behavior: "smooth", block: "center" });
    renderProgress({ state: result.state, capture_id: result.capture_id });
    elements.recordForm.reset();
    selectedFiles = [];
    renderFiles();
    elements.notebook.value = selected.value;
    pollCapture(result.capture_id);
  } catch (error) {
    showError(error);
  } finally {
    setBusy(elements.submit, false, "正在接收并校验…");
    elements.submit.disabled = !authenticated || !elements.notebook.value;
  }
});

byId("dismiss-error").addEventListener("click", hideError);
elements.recordLink.addEventListener("click", async () => {
  await navigator.clipboard.writeText(elements.captureId.textContent);
  elements.recordLink.textContent = "已复制";
  window.setTimeout(() => { elements.recordLink.textContent = "复制回执编号"; }, 1200);
});
byId("copy-api").addEventListener("click", async (event) => {
  await navigator.clipboard.writeText(`${location.origin}/v1`);
  event.currentTarget.textContent = "已复制";
  window.setTimeout(() => { event.currentTarget.textContent = "复制地址"; }, 1200);
});

async function boot() {
  const apiAddress = `${location.origin}/v1`;
  byId("api-address").textContent = apiAddress;
  byId("service-address").textContent = location.host;
  byId("matelab-server").textContent = matelabUrl;
  try {
    const session = await api("/v1/ui/session");
    setAuthenticated(Boolean(session.authenticated));
    if (session.authenticated) await refreshNotebooks();
  } catch (error) {
    elements.pill.classList.add("status-error");
    elements.sessionLabel.textContent = "本地服务异常";
    showError(error);
  }
}

boot();

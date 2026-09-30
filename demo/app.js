/* Demo UI for the IranDoc news-generation API.
   Sends valid and deliberately invalid payloads to show the backend's
   validation order: 400 (bad JSON) -> 401 (bad secret) -> 422 (schema). */

const $ = (id) => document.getElementById(id);

const FIELDS = {
  secret: $("secret"),
  username: $("username"),
  title: $("title"),
  abstract: $("abstract"),
  field: $("field"),
  university: $("university"),
  toc: $("toc"),
  author: $("author"),
  supervisor: $("supervisor"),
};

let lastRecordId = null;

function apiBase() {
  return $("apiBase").value.trim().replace(/\/+$/, "");
}

function readForm() {
  return {
    shared_Secret: FIELDS.secret.value,
    username: FIELDS.username.value,
    title: FIELDS.title.value,
    abstract: FIELDS.abstract.value,
    field: FIELDS.field.value,
    university: FIELDS.university.value,
    table_of_contents: FIELDS.toc.value,
    author: FIELDS.author.value,
    supervisor: FIELDS.supervisor.value,
  };
}

function show(text) {
  try {
    return JSON.stringify(text, null, 2);
  } catch {
    return String(text);
  }
}

/* Render the outcome: banner colour by status class, plus both payloads. */
function render(status, statusText, body, sent, elapsed) {
  const banner = $("statusBanner");
  const kind = status === 200 ? "ok" : status === 429 ? "warn" : "err";

  banner.className = "banner " + kind;
  $("statusCode").textContent = status === 0 ? "خطای شبکه" : status;
  $("statusText").textContent = statusText;
  $("statusTime").textContent = elapsed != null ? elapsed + " ms" : "";
  $("resultMeta").textContent = status ? `HTTP ${status}` : "—";

  $("responseBody").textContent = body === undefined ? "—" : show(body);
  $("requestBody").textContent = sent === undefined ? "—" : show(sent);

  if (status === 200 && body && typeof body.id === "number") {
    lastRecordId = body.id;
  }
}

/* ---- Scenarios: each one shows a different backend behaviour ---- */
const SCENARIOS = {
  valid: {
    note: "درخواست معتبر — رکورد با وضعیت ready ذخیره شد.",
    body: () => readForm(),
  },
  missingField: {
    note: "فیلد supervisor جا افتاده — اعتبارسنجی Pydantic (۴۲۲).",
    body: () => {
      const b = readForm();
      delete b.supervisor;
      return b;
    },
  },
  whitespace: {
    note: "مقدار فقط فاصله — رد شدن توسط اعتبارسنج non_whitespace (۴۲۲).",
    body: () => ({ ...readForm(), title: "     " }),
  },
  longTitle: {
    note: "عنوان بلندتر از ۵۰۰ کاراکتر — نقض max_length (۴۲۲).",
    body: () => ({ ...readForm(), title: "مطالعه‌ای ".repeat(120) }),
  },
  badSecret: {
    note: "رمز اشتباه — احراز هویت پیش از اعتبارسنجی (۴۰۱).",
    body: () => ({ ...readForm(), shared_Secret: "definitely-wrong-secret" }),
  },
  noSecret: {
    note: "رمز ارسال نشده — دسترسی غیرمجاز (۴۰۱).",
    body: () => {
      const b = readForm();
      delete b.shared_Secret;
      return b;
    },
  },
  malformed: {
    note: "بدنهٔ JSON خراب — بدنه پیش از هر چیز پارس می‌شود (۴۰۰).",
    raw: "{ \"username\": \"demo_user\", ",
  },
  extra: {
    note: "فیلدهای اضافی نادیده گرفته می‌شوند و درخواست پذیرفته می‌شود (۲۰۰).",
    body: () => ({ ...readForm(), unexpected_field: "ignored", nested: { a: 1 } }),
  },
};

async function send() {
  const scenario = SCENARIOS[sendingScenario || "valid"];
  const btn = $("sendBtn");
  btn.disabled = true;
  btn.textContent = "در حال ارسال…";

  const started = performance.now();
  const url = `${apiBase()}/submit`;

  let sentBody;
  let requestInit = { method: "POST", headers: { "Content-Type": "application/json" } };

  if (scenario.raw !== undefined) {
    // Raw string body: reproduces the malformed-JSON path (HTTP 400).
    sentBody = scenario.raw;
    requestInit.body = scenario.raw;
  } else {
    sentBody = scenario.body();
    requestInit.body = JSON.stringify(sentBody);
  }

  try {
    const res = await fetch(url, requestInit);
    const elapsed = Math.round(performance.now() - started);
    let body;
    try {
      body = await res.json();
    } catch {
      body = "(بدنهٔ پاسخ JSON نبود)";
    }
    render(res.status, scenario.note, body, sentBody, elapsed);
  } catch (err) {
    const elapsed = Math.round(performance.now() - started);
    render(0, "ارتباط با سرور برقرار نشد — آدرس API را بررسی کنید.", String(err), sentBody, elapsed);
  } finally {
    sendingScenario = null;
    btn.disabled = false;
    btn.textContent = "ارسال درخواست";
  }
}

let sendingScenario = null;

/* ---- Poll a submitted record to show the async worker pipeline ---- */
async function pollRecord() {
  const id = lastRecordId ?? prompt("شناسهٔ رکورد را وارد کنید:", "1");
  if (id === null) return;
  const url = `${apiBase()}/records/${encodeURIComponent(id)}?shared_Secret=${encodeURIComponent(FIELDS.secret.value)}`;

  $("pollBtn").disabled = true;
  $("pollBtn").textContent = "در حال بررسی…";

  try {
    const res = await fetch(url);
    let body;
    try {
      body = await res.json();
    } catch {
      body = "(بدنهٔ پاسخ JSON نبود)";
    }

    let note = "وضعیت رکورد از سرور دریافت شد.";
    if (res.status === 200) {
      if (body.status === "review") note = "پردازش کامل شد — خروجی تولیدشده در output_json موجود است.";
      else if (body.status === "ready") note = "در صف پردازش Worker — کمی بعد دوباره بررسی کنید.";
      else if (body.status === "error") note = "پردازش با خطا متوقف شد.";
    }
    render(res.status, note, body, `GET ${url.replace(FIELDS.secret.value, "***")}`);
  } catch (err) {
    render(0, "ارتباط با سرور برقرار نشد.", String(err));
  } finally {
    $("pollBtn").disabled = false;
    $("pollBtn").textContent = "بررسی وضعیت رکورد (GET /records/{id})";
  }
}

/* ---- Startup: show service identity and DB health ---- */
async function probe() {
  try {
    const rootRes = await fetch(`${apiBase()}/`);
    if (rootRes.ok) {
      const info = await rootRes.json();
      $("svcName").textContent = info.service;
      $("svcVer").textContent = info.version;
    }
  } catch { /* identity is optional */ }

  try {
    const res = await fetch(`${apiBase()}/health`);
    const data = await res.json();
    const ok = data.status === "ok" && data.database === "connected";
    $("healthDot").className = "dot " + (ok ? "ok" : "bad");
    $("healthText").textContent = `API: ${data.status} · DB: ${data.database}`;
  } catch {
    $("healthDot").className = "dot bad";
    $("healthText").textContent = "API در دسترس نیست";
  }
}

/* ---- Wiring ---- */
document.querySelectorAll("[data-scenario]").forEach((btn) => {
  btn.addEventListener("click", () => {
    sendingScenario = btn.dataset.scenario;
    send();
  });
});

$("sendBtn").addEventListener("click", () => {
  sendingScenario = "valid";
  send();
});

$("pollBtn").addEventListener("click", pollRecord);

$("resetBtn").addEventListener("click", () => {
  sendingScenario = null;
  $("statusBanner").className = "banner idle";
  $("statusCode").textContent = "—";
  $("statusText").textContent = "هنوز درخواستی ارسال نشده است.";
  $("statusTime").textContent = "";
  $("resultMeta").textContent = "—";
  $("responseBody").textContent = "—";
  $("requestBody").textContent = "—";
  lastRecordId = null;
});

probe();
setInterval(probe, 15000);

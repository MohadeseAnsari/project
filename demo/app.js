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

/* Where the API lives.

Defaults to the same host that served this page, on port 8000 — that is correct
whether the demo is opened on localhost or reached from another machine, which a
hardcoded "http://localhost:8000" is not (from another machine that resolves to
the *visitor's* own localhost and silently fails).

An explicit value in ?api= wins, so a presenter can point one shared page at a
different deployment without editing files. A typed value always wins over both.
*/
const API_PORT = "8000";

function defaultApiBase() {
  const fromQuery = new URLSearchParams(location.search).get("api");
  if (fromQuery) return fromQuery.replace(/\/+$/, "");
  if (location.protocol === "file:") return "http://localhost:" + API_PORT;
  return `${location.protocol}//${location.hostname}:${API_PORT}`;
}

function initApiBase() {
  $("apiBase").value = defaultApiBase();
  updateApiBaseHint();
}

function updateApiBaseHint() {
  const hint = $("apiBaseHint");
  const isLocal = ["localhost", "127.0.0.1", "::1", ""].includes(location.hostname);
  if (!isLocal) {
    hint.className = "hint warn";
    hint.textContent =
      `Served from ${location.hostname} — the API is expected on the same host, port ${API_PORT}. ` +
      `If requests fail, allow this origin on the API (DEMO_CORS_ORIGINS=http://${location.protocol}//${location.hostname}:8080).`;
  } else {
    hint.className = "hint";
    hint.textContent = "Leave as-is for a local run; edit to point at another deployment.";
  }
}

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

/* ---- Scenarios ----
 *
 * A scenario only *fills the form*. Nothing is sent until the presenter presses
 * the send button, so the audience can read the payload that is about to go out
 * and the presenter controls the timing.
 *
 * `apply` sets field values. `omit` names fields the backend will not receive at
 * all, which a form cannot express (an absent key is not an empty string) --
 * those fields are blanked and greyed out so the omission is visible rather
 * than implied.
 */
const GOOD_INPUT = {
  username: "demo_user",
  title: "بررسی اثر متغیرهای محیطی بر رشد پایتخت انسانی",
  abstract:
    "این پژوهش با هدف بررسی تأثیر عوامل محیطی بر رشد سلولی در بافت پایتخت انسانی انجام شده است. نمونه‌ها در سه محیط کنترل‌شده نگهداری و پس از ۴۸ ساعت اندازه‌گیری شدند.",
  field: "پزشکی",
  university: "دانشگاه تهران",
  toc: "۱. مقدمه\n۲. مرور ادبیات\n۳. روش‌شناسی\n۴. نتایج\n۵. نتیجه‌گیری",
  author: "زهرا محمدی",
  supervisor: "دکتر رضایی",
};

const SCENARIOS = {
  valid: {
    label: "ورودی معتبر → ۲۰۰",
    expect: "درخواست معتبر — رکورد با وضعیت ready ذخیره می‌شود.",
    apply: () => ({ ...GOOD_INPUT }),
  },
  missingField: {
    label: "فیلد جاافتاده → ۴۲۲",
    expect: "کلید supervisor اصلاً ارسال نمی‌شود — اعتبارسنجی Pydantic.",
    apply: () => ({ ...GOOD_INPUT }),
    omit: ["supervisor"],
  },
  whitespace: {
    label: "فقط فاصله → ۴۲۲",
    expect: "مقدار title فقط فاصله است — رد می‌شود (نه خالی، نه کوتاه).",
    apply: () => ({ ...GOOD_INPUT, title: "     " }),
  },
  longTitle: {
    label: "عنوان طولانی → ۴۲۲",
    expect: "عنوان از ۵۰۰ کاراکتر عبور می‌کند — نقض max_length.",
    apply: () => ({ ...GOOD_INPUT, title: "مطالعه‌ای ".repeat(120) }),
  },
  badSecret: {
    label: "رمز اشتباه → ۴۰۱",
    expect: "shared_Secret نادرست — احراز هویت پیش از اعتبارسنجی.",
    apply: () => ({ ...GOOD_INPUT }),
    secret: "definitely-wrong-secret",
  },
  noSecret: {
    label: "بدون رمز → ۴۰۱",
    expect: "shared_Secret اصلاً ارسال نمی‌شود — دسترسی غیرمجاز.",
    apply: () => ({ ...GOOD_INPUT }),
    omit: ["secret"],
  },
  malformed: {
    label: "JSON خراب → ۴۰۰",
    expect: "بدنه JSON ناقص است — بدنه پیش از هر چیز پارس می‌شود.",
    apply: () => ({ ...GOOD_INPUT }),
    raw: '{ "shared_Secret": "…", "username": "demo_user", ',
  },
  extra: {
    label: "فیلد اضافی → ۲۰۰",
    expect: "کلیدهای ناشناخته نادیده گرفته می‌شوند و درخواست پذیرفته می‌شود.",
    apply: () => ({ ...GOOD_INPUT }),
    extra: { unexpected_field: "ignored", nested: { a: 1 } },
  },
};

/* Form-field key <-> input id. */
const GOOD_SECRET = FIELDS.secret.value;

const KEY_TO_INPUT = {
  username: "username",
  title: "title",
  abstract: "abstract",
  field: "field",
  university: "university",
  toc: "toc",
  author: "author",
  supervisor: "supervisor",
};

/* Load a scenario into the form. The selected scenario only records *intent*
 * (omit / raw / extra); the payload is rebuilt from the form at send time, so
 * an edit made after clicking a chip is what actually gets sent. */
let activeScenario = "valid";

function applyScenario(key) {
  const s = SCENARIOS[key];
  if (!s) return;
  activeScenario = key;

  const values = s.apply();

  for (const [formKey, inputId] of Object.entries(KEY_TO_INPUT)) {
    const el = $(inputId);
    el.value = values[formKey] ?? "";
    // Omitted fields are blanked and dimmed, so the audience can see the key is
    // absent rather than empty.
    el.classList.toggle("omitted", (s.omit || []).includes(formKey));
    el.closest(".field").classList.toggle(
      "is-omitted",
      (s.omit || []).includes(formKey)
    );
  }

  FIELDS.secret.value = (s.omit || []).includes("secret") ? "" : s.secret ?? GOOD_SECRET;
  FIELDS.secret.classList.toggle("omitted", (s.omit || []).includes("secret"));
  FIELDS.secret.closest(".field").classList.toggle(
    "is-omitted",
    (s.omit || []).includes("secret")
  );

  $("scenarioNote").textContent = s.expect;
  $("scenarioNote").className = "note";

  document.querySelectorAll("[data-scenario]").forEach((b) => {
    b.classList.toggle("active", b.dataset.scenario === key);
  });
}

/* Build the payload from the form as it currently stands. */
function buildPayload() {
  const payload = readForm();

  for (const formKey of SCENARIOS[activeScenario]?.omit || []) {
    const payloadKey = formKey === "secret" ? "shared_Secret" : formKey;
    delete payload[payloadKey];
  }

  Object.assign(payload, SCENARIOS[activeScenario]?.extra || {});
  return payload;
}

async function send() {
  const scenario = SCENARIOS[activeScenario] || SCENARIOS.valid;
  const btn = $("sendBtn");
  btn.disabled = true;
  btn.textContent = "در حال ارسال…";

  const started = performance.now();
  const url = `${apiBase()}/submit`;

  let sentBody;
  const requestInit = {
    method: "POST",
    headers: { "Content-Type": "application/json" },
  };

  if (scenario.raw !== undefined) {
    // Raw string body: reproduces the malformed-JSON path (HTTP 400). The
    // scenario chip fills the form, but the wire payload is deliberately broken.
    sentBody = scenario.raw;
    requestInit.body = scenario.raw;
  } else {
    sentBody = buildPayload();
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
    render(res.status, outcomeFor(res.status), body, sentBody, elapsed);
  } catch (err) {
    const elapsed = Math.round(performance.now() - started);
    render(0, "ارتباط با سرور برقرار نشد — آدرس API را بررسی کنید.", String(err), sentBody, elapsed);
  } finally {
    btn.disabled = false;
    btn.textContent = "ارسال درخواست";
  }
}

/* Describe what the server actually did, rather than what the scenario expected.
   A scenario is only a pre-filled form, so the response is the source of truth —
   which also means an edit made after picking a chip is described correctly. */
function outcomeFor(status) {
  switch (status) {
    case 200:
      return "پذیرفته شد — رکورد با وضعیت ready ذخیره شد و Worker آن را پردازش می‌کند.";
    case 400:
      return "بدنه JSON معتبر نبود — قبل از هر بررسی دیگری رد شد (۴۰۰).";
    case 401:
      return "احراز هویت ناموفق — قبل از اعتبارسنجی رد شد، پس رکوردی ساخته نشد (۴۰۱).";
    case 422:
      return "رمز درست بود اما بار اعتبارسنجی نشد (۴۲۲).";
    case 429:
      return "محدودیت نرخ: حداکثر ۱۰ درخواست در دقیقه (۴۲۹).";
    default:
      return status >= 500 ? "خطای داخلی سرور (۵xx)." : `پاسخ غیرمنتظره (${status}).`;
  }
}

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
$("apiBase").addEventListener("input", updateApiBaseHint);

// A chip only fills the form. The presenter then presses send, so the audience
// can read the payload first and the timing of the request stays under control.
document.querySelectorAll("[data-scenario]").forEach((btn) => {
  btn.addEventListener("click", () => applyScenario(btn.dataset.scenario));
});

$("sendBtn").addEventListener("click", send);

// Editing a field invalidates the scenario's "expect" line, since the presenter
// may have changed the payload away from what the chip set up.
Object.values(FIELDS).forEach((el) =>
  el.addEventListener("input", () => {
    if (activeScenario !== "valid") {
      $("scenarioNote").textContent =
        "فرم ویرایش شده — همان چیزی که الان در فیلدهاست ارسال می‌شود.";
      $("scenarioNote").className = "note edited";
    }
  })
);

$("pollBtn").addEventListener("click", pollRecord);

$("resetBtn").addEventListener("click", () => {
  applyScenario("valid");
  $("statusBanner").className = "banner idle";
  $("statusCode").textContent = "—";
  $("statusText").textContent = "هنوز درخواستی ارسال نشده است.";
  $("statusTime").textContent = "";
  $("resultMeta").textContent = "—";
  $("responseBody").textContent = "—";
  $("requestBody").textContent = "—";
  lastRecordId = null;
});

initApiBase();
applyScenario("valid");
probe();
setInterval(probe, 15000);

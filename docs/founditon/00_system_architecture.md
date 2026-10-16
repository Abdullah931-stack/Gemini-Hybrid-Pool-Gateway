# 00. المعمارية العامة للنظام وحدود المكونات (`00_SYSTEM_ARCHITECTURE.md`)

## 1. رؤية النظام والغرض المعماري (System Vision & Scope)

تُحدد هذه الوثيقة المعمارية البرمجية النهائية، وهياكل البيانات، والخوارزميات الرياضية، وبروتوكولات التحويل لنقاط الربط (Endpoints) لبناء بوابة توجيه محلية هجينة (**Local Hybrid AI API Gateway**) تحت اسم **`Gemini-Hybrid-Pool-Gateway` (الإصدار المعماري المحدث v3.2)** تعمل كطبقة ربط موحدة متوافقة 100% مع معيار **OpenAI API** لتشغيل بيئات الوكلاء الذاتيين (Agent Harnesses مثل **Pi** و **Hermes**) باستثمار حصص اشتراكات **Google AI Pro** المتعددة خارج بيئات جوجل القياسية.

صُممت هذه النسخة المحدثة (**v3.2**) لتدمج أفضل الممارسات والحلول البرمجية المختبرة من المشاريع مفتوحة المصدر الرائدة، مع معالجة الفجوات البروتوكولية والمعمارية المكتشفة حتى أكتوبر 2026 بعد تمحيصها رياضياً وهندسياً:

* إزالة حقل `requestType: "agent"` المسبب لأخطاء `429` الزائفة على حسابات Pro.
* معالجة بصمات التفكير `thoughtSignature`، وإصلاح انقطاع الأدوات الصفرية بعد التفكير مع الحفاظ على اتساق السجل التاريخي (`History Replay Consistency`).
* موازنة معادلة التوجيه في قناة الويب بحيث تجمع بين التزامن المزدوج الآمن ($C_{\text{web}, i} \le 2$) وحماية ألفة الجلسة ($S_i$) دون أن يسحق مقام التوازي أولوية الجلسة المفتوحة، مع ربط مهلة التهدئة ببداية الطلب (`T_start`) لا بنهايته.
* التوافق العابر للأنظمة (Linux / Windows / macOS) في تمرير ملفات السياق الضخم عبر فحص `/dev/shm` والانتقال التلقائي إلى `tempfile.gettempdir()` مع التنظيف الدوري للملفات اليتيمة.
* دعم استدعاءات الأدوات المتعددة والمتتالية (`Parallel Tool Calls`) داخل نفس الرسالة في محلل البث الاستباقي لقناة الويب مع كبح تسرب النصوص الفاصلة بعد بدء الأدوات.
* اعتماد سجل الذاكرة الموحد (`In-Memory Authority`) مع الحفظ الدوري الذري المحمي بـ `accounts_file_lock` لمنع أي تنافس (`Race Condition`) دون التضحية بقابلية قراءة ملف `accounts.json`.

## 2. الفصل الهجين بين قناتي الاتصال (Dual-Channel Architecture)

تعتمد المعمارية على الفصل التام والتكامل الديناميكي بين قناتين مستقلتين للاتصال، مع قابلية توسع مرنة تقبل تسجيل أي عدد من الحسابات (من حساب واحد إلى عشرات الحسابات، أو العمل بقناة واحدة فقط) دون أي قيود ثابتة أو حاجة لإعادة تشغيل الخادم:

1. **قناة `OAuth CLI Pool` (مسبح حسابات Antigravity 2.0 / Code Assist CLI):** قناة مخصصة لبيئات الوكلاء، تعتمد على مصادقة OAuth 2.0 + PKCE المستقلة عبر نقطة الإنتاج `https://cloudcode-pa.googleapis.com/v1internal` (أو عبر محول العملية الفرعية الرسمي `agy --input-format stream-json`)، وتُستغل لتمرير طلبات الوكلاء الفرعيين المتوازية، واستدعاء الأدوات الأصلي (`functionDeclarations`)، ونماذج السرعة (`gemini-3.8-flash` و `gemini-3.1-flash-lite`) مع محول بروتوكول ومطهر مخططات وحاقن `thoughtSignature` مدمج.
2. **قناة `Web Cookie Pool` (مسبح حسابات واجهة الويب):** قناة مستقلة تماماً تعتمد على جلسات الويب (`gemini-webapi v2.1.1+`)، وتُستغل لتشغيل النماذج الفائقة المتاحة لمشتركي Pro على واجهة الويب مثل **`gemini-4-argon`** و **`gemini-3.1-pro`**، مع دعم معالجة طلبين متوازيين لكل حساب ويب ($C_{\text{web}, i} \le 2$)، بالإضافة إلى العمل كخط إسناد تلقائي فوري (Cross-Channel Failover) لطلبات القناة الأولى عند استنفاد حصصها.

## 3. حدود النظام وتقسيم المستويات التشغيلية (Operational Planes)

| المستوى التشغيلي (Operational Plane) | الملفات والوحدات التابعة له | الوصف الوظيفي وفق المعمارية |
| :--- | :--- | :--- |
| **1. مستوى الحالة والتخزين (State & Storage Plane)** | `config/settings.py`, `config/accounts.json`, `config/cookies_cache/`, `lmdb` (`db_sessions`, `db_signatures`) | الإعدادات العامة، السجل الموحد للحسابات وحصصها (يُكتب ذرياً من الذاكرة عبر `accounts_file_lock`)، ملفات الكوكيز المعزولة المحمية بـ `cookie_write_lock`، وجدولا مطابقة البادئات وبصمات التفكير. |
| **2. مستوى الأمان ودورة الحياة (Security & Lifecycle Plane)** | `core/auth_oauth_cli.py`, `core/auth_web.py`, `core/model_registry.py`, `core/usage_monitor.py` | مصادقة OAuth 2.0 + PKCE، إدارة جلسات الويب وحفظ الكوكيز الخلفي، الاكتشاف الديناميكي الموحد للنماذج وترجمة مستويات التفكير، ومراقب الحصص الاستباقي للقناتين. |
| **3. مستوى التحكم والجدولة (Control & Scheduling Plane)** | `core/hybrid_scheduler.py`, `core/failover.py` | محرك التوجيه الرياضي (دالة التوازي المشروطة بالألفة + معامل Soonest-Reset)، ومصافحة البث غير الفارغ (`Non-Empty Pre-Stream Handshake`) والتحويل الفوري. |
| **4. مستوى البيانات والترجمة البروتوكولية (Data & Protocol Plane)** | `core/schema_sanitizer.py`, `core/token_saver.py`, `core/prefix_differ.py`, `core/oauth_cli_channel.py`, `core/web_channel.py` | مطهر JSON Schema وفلتر WAF، ضاغط مخرجات الأدوات غير المخلّ (`RTK-Lite`)، محرك مطابقة البادئة وآلة إعادة ترطيب السياق العابرة للأنظمة، عميل قناة OAuth CLI، وعميل قناة الويب مع محلل الأدوات المتعددة وبث التفكير. |
| **5. مستوى الواجهات والربط الخارجي (Interface Plane)** | `api/openai_routes.py`, `api/dashboard_routes.py`, `static/index.html`, `main.py`, `requirements.txt` | نقاط ربط OpenAI القياسية، واجهات برمجة الإضافة والحذف اللحظي للحسابات ولوحة التحكم الرسومية، ونقطة تشغيل الخادم وأوامر CLI. |

## 4. المكدس التقني ومصادر التطعيم البرمجي (Tech Stack & Open-Source Grafting)

يعتمد النظام على بيئة **Python 3.11+** غير المتزامنة (`asyncio`) مع استراتيجية **التطعيم البرمجي المباشر (Code Grafting)** من المستودعات المرجعية لتفادي إعادة اختراع المحولات البروتوكولية:

| المكتبة / المكون                | الدور الوظيفي في النظام                                                                                                                                                                                                            | المصدر المرجعي المتبنى (Code Grafting Reference)                                                                                                                                                                                                                                                                                                    |
| :------------------------------ | :--------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **`fastapi` & `uvicorn`**       | بناء الخادم المحلي، وتوفير نقاط ربط OpenAI (`/v1/chat/completions`, `/v1/models`) وواجهة الإدارة الفورية (`/dashboard`).                                                                                                           | قياسي (`ASGI` مع دعم `StreamingResponse` المحمي بمصافحة البث غير الفارغ).                                                                                                                                                                                                                                                                           |
| **`gemini-webapi` (v2.1.1+)**   | إدارة قناة **Web Pool**، والاكتشاف الديناميكي للنماذج (`list_models`)، والبث التزايدي المتوازي (`generate_content_stream` / `send_message_stream`)، وفصل التفكير (`thoughts`)، والتدوير التلقائي الخلفي لكوكيز `__Secure-1PSIDTS`. | مكتبة [HanaokaYuzu/Gemini-API](https://github.com/HanaokaYuzu/Gemini-API) على [PyPI](https://pypi.org/project/gemini-webapi/).                                                                                                                                                                          |
| **`httpx` (`http2=True`)**      | تنفيذ الطلبات غير المتزامنة لقناة **OAuth CLI** عبر نقطة الإنتاج `cloudcode-pa.googleapis.com/v1internal`، واستدعاء RPC `jSf9Qc` (`batchexecute`) الموثق برمز `SNlM0e` لقراءة نسب استهلاك الويب من صفحة `/usage`.                  | محول [su-kaka/gcli2api](https://github.com/su-kaka/gcli2api)، ومنطق [router-for-me/CLIProxyAPI](https://github.com/router-for-me/CLIProxyAPI)، وقارئ [quotacheck-mcp](https://github.com/mingjianliu/quotacheck-mcp). |
| **`browser-cookie3` (Firefox)** | استيراد جلسات الويب من متصفح **Firefox** حصراً (لتفادي تشفير **DBSC** المقيد في متصفحات Chromium الذي يبطل الكوكيز خلال ساعات).                                                                                                    | موصى به رسمياً في [gemini-webapi](https://pypi.org/project/gemini-webapi/).                                                                                                                                                                                                                             |
| **`lmdb`**                      | تخزين جدول مطابقة البادئات (`db_sessions`: ربط بصمة الرسائل بـ `[cid, rid, rcid]` ومعرف الحساب) وجدول بصمات التفكير (`db_signatures`: ربط `tool_call_id` بـ `thoughtSignature`).                                                   | بنية التخزين في [Nativu5/Gemini-FastAPI](https://github.com/Nativu5/Gemini-FastAPI).                                                                                                                                                                                                                      |
| **`pydantic` (v2)**             | التحقق من صحة هياكل البيانات، ومطابقة نماذج الطلبات والاستجابات، وتنظيف مخططات الأدوات (`Tool Schema Sanitization`).                                                                                                               | قواعد التطهير في [PicoClaw Antigravity](https://docs.picoclaw.io/docs/providers/antigravity/) ومعالجة [CLIProxyAPI #739](https://github.com/router-for-me/CLIProxyAPI/issues/739).                                                                      |
| **`apscheduler`**               | جدولة المهام الخلفية الدورية (حفظ الكوكيز المحدثة بالخلفية كل 5 دقائق، ومزامنة نسب الاستهلاك عبر القناتين، وتحديث سجل النماذج المكتشفة، وتنظيف محادثات الويب والملفات المؤقتة المنتهية).                                           | جدولة خلفية غير متزامنة (`AsyncIOScheduler`).                                                                                                                                                                                                                                                                                                       |

## 5. الهيكل التنظيمي لملفات المشروع (Project Structure)

```text
gemini-hybrid-pool-gateway/
├── config/
│   ├── settings.py               # الإعدادات العامة، أوزان الاستهلاك التناسبية، وسقوف التوازي، وفاصل الإطلاق (0.5s)
│   ├── accounts.json             # السجل الموحد للحسابات وحصصها (يُكتب ذرياً من الذاكرة عبر accounts_file_lock)
│   └── cookies_cache/            # ملفات كوكيز معزولة لكل حساب ({account_id}.json) محمية بـ cookie_write_lock
├── core/
│   ├── auth_oauth_cli.py         # مصادقة OAuth 2.0 + PKCE، وجلب project_id، ومحول agy الاختياري، وتجديد التوكنات
│   ├── auth_web.py               # إدارة جلسات الويب، وحماية الإغلاق المتزامن، وخطاف حفظ الكوكيز الخلفي المعزول
│   ├── schema_sanitizer.py       # مطهر JSON Schema، ومعالج الأدوات الصفرية، وفلتر WAF لترويسات النظام
│   ├── token_saver.py            # ضاغط مخرجات الأدوات غير المخلّ (RTK-Lite Filter لـ git diff / grep / ls)
│   ├── model_registry.py         # الاكتشاف الديناميكي الموحد للنماذج وترجمة مستويات التفكير (thinkingLevel)
│   ├── usage_monitor.py          # مراقب الحصص الاستباقي (:fetchAvailableModels / :retrieveUserQuota + RPC jSf9Qc)
│   ├── hybrid_scheduler.py       # محرك التوجيه الرياضي (دالة التوازي المشروطة بالألفة + معامل Soonest-Reset)
│   ├── prefix_differ.py          # محرك مطابقة البادئة وآلة إعادة ترطيب السياق العابرة للأنظمة (/dev/shm أو tempfile)
│   ├── oauth_cli_channel.py      # عميل قناة OAuth CLI مع محول v1internal وحاقن thoughtSignature و _step التاريخي
│   ├── web_channel.py            # عميل قناة الويب مع محلل الأدوات المتعددة (Multi-Tool Lookahead Parser) وبث التفكير
│   └── failover.py               # مصافحة البث غير الفارغ (Non-Empty Pre-Stream Handshake) والتحويل الفوري
├── api/
│   ├── openai_routes.py          # نقاط ربط OpenAI القياسية (/v1/chat/completions, /v1/models)
│   └── dashboard_routes.py       # واجهات برمجة الإضافة والحذف اللحظي للحسابات ومراقبة الحالة الحية
├── static/
│   └── index.html                # لوحة التحكم الرسومية لإدارة القناتين وإضافة الحسابات حياً
├── main.py                       # نقطة تشغيل الخادم وأوامر CLI (--serve, --add-oauth, --add-web)
└── requirements.txt              # قائمة المكتبات المعتمدة
```

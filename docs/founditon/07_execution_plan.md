# 07. الخطة التنفيذية المرحلية والترتيب التوبولوجي (`07_EXECUTION_PLAN.md`)

تُرتب هذه الوثيقة خطوات بناء ملفات نظام `Gemini-Hybrid-Pool-Gateway (v3.2)` وفق تسلسل اعتمادية من الأسفل إلى الأعلى (**Bottom-Up Topological Implementation**) استناداً حصراً إلى المكونات والوظائف المحددة في وثيقة المواصفات الهندسية.

## 1. الرسم البياني التوبولوجي للاعتمادية (Topological Dependency Graph)

تُصنف ملفات المشروع الواردة في الهيكل التنظيمي إلى 6 مستويات اعتمادية (`Level 0` إلى `Level 5`) لضمان عدم استيراد أي وحدة قبل اكتمال الوحدات التي تعتمد عليها:

| مستوى الاعتمادية | الملفات البرمجية | الاعتماديات الداخلية (Internal Imports) |
| :--- | :--- | :--- |
| **المستوى 0 (الإعدادات والحالة والتخزين)** | `config/settings.py`, `config/accounts.json`, `config/cookies_cache/` | لا يوجد (تعتمد فقط على مكتبات البيئة القياسية و`pydantic` و`lmdb`). |
| **المستوى 1 (المطهرات والفلاتر عديمة الحالة)** | `core/token_saver.py`, `core/schema_sanitizer.py` | يستورد من **المستوى 0** (`config/settings.py`). |
| **المستوى 2 (المصادقة، اكتشاف النماذج، ومطابقة البادئات)** | `core/auth_oauth_cli.py`, `core/auth_web.py`, `core/model_registry.py`, `core/prefix_differ.py` | يستورد من **المستوى 0 و 1**. |
| **المستوى 3 (مراقبة الحصص وعملاء القناتين)** | `core/usage_monitor.py`, `core/oauth_cli_channel.py`, `core/web_channel.py` | يستورد من **المستويات 0 و 1 و 2**. |
| **المستوى 4 (الجدولة الرياضية والتحويل الفوري)** | `core/hybrid_scheduler.py`, `core/failover.py` | يستورد من **المستويات 0 و 2 و 3**. |
| **المستوى 5 (نقاط الربط، لوحة التحكم، ونقطة التشغيل)** | `api/openai_routes.py`, `api/dashboard_routes.py`, `static/index.html`, `main.py` | يستورد من **جميع المستويات الأدنى (0 إلى 4)**. |

## 2. مراحل التنفيذ المتسلسلة وبوابات التحقق (Sequential Execution Phases & Verification Gates)

### المرحلة الأولى: إعداد التكوين، وسجل الذاكرة الموحد، وجداول `LMDB` (المستوى 0)

* **الملفات المستهدفة:** `requirements.txt`, `config/settings.py`, `config/accounts.json`, `config/cookies_cache/`
* **المهام التنفيذية:**
  1. تثبيت المكتبات المعتمدة في `requirements.txt` (`fastapi`, `uvicorn`, `httpx[http2]`, `gemini-webapi[browser]`, `lmdb`, `pydantic`, `apscheduler`).
  2. ضبط `config/settings.py` شاملاً الأوزان الأساسية $\Delta_{\text{base}}(m)$ (`+0.10` لـ `gemini-4-argon`، و`+0.03` لـ `gemini-3.1-pro`، و`+0.002` لـ `gemini-3.8-flash`، و`0.000` لـ `gemini-3.1-flash-lite`)، وسقوف التوازي (`max_concurrency = 3` لـ OAuth و`max_concurrency = 2` للويب)، وفاصل الإطلاق التكيفي (`WEB_MIN_START_INTERVAL_S = 0.5s` بين بدايتي طلبين متتاليين، وتهدئة `0.4s–0.8s` / `WEB_MIN_INTERVAL_S = 0.6` للطلبات الخاطفة التي استغرقت أقل من ثانية واحدة).
  3. تنفيذ قراءة `config/accounts.json` عند إقلاع الخادم إلى كائنات Python في الذاكرة (`In-Memory Single Source of Truth`) مع متغيرات الحالة اللحظية وأقفال `token_refresh_lock` و`cookie_write_lock` و`conversation_locks` و`reinit_lock`، وتنفيذ دالة الحفظ الذري `save_accounts_snapshot()` المحمية بقفل `accounts_file_lock = asyncio.Lock()` عبر الكتابة إلى `accounts.json.tmp` ثم `os.replace()`.
  4. تهيئة جدولي `db_sessions` و `db_signatures` في `lmdb`.
* **بوابة التحقق (Verification Gate 1):**
  * التحقق من تحميل `config/accounts.json` في الذاكرة وتنفيذ عمليات `save_accounts_snapshot()` المتزامنة تحت حماية `accounts_file_lock` دون حدوث `Race Condition`، والتحقق من قراءة وكتابة `db_sessions` و `db_signatures` في `lmdb`.

### المرحلة الثانية: بناء ضاغط التوكنات ومطهر المخططات ودرع WAF (المستوى 1)

* **الملفات المستهدفة:** `core/token_saver.py`, `core/schema_sanitizer.py`
* **المهام التنفيذية:**
  1. تنفيذ فلتر `RTK-Lite Lossless Filter` في `core/token_saver.py` لضغط مخرجات `git diff`، و`grep`، و`find`، و`ls`، وسجلات الاختبارات المتكررة في رسائل `role: "tool"`، مع دعم تعطيله بترويسة `X-Token-Saver: off`.
  2. تنفيذ فلتر درع WAF (`WAF System Prompt Shield`) في `core/schema_sanitizer.py` لفحص رسائل `system` و `developer` وتحويل وسم `<system-conventions>` إلى `<conventions>` وفصل سطر `RFC 2119: MUST, REQUIRED...` عن وسم الافتتاح.
  3. تنفيذ دالة التنظيف العودية لمصفوفة `tools` لحذف مفاتيح JSON Schema العشرين غير المدعومة في `cloudcode-pa` وتسطيح اتحادات `anyOf` و `oneOf`.
  4. تنفيذ المعالجة المزدوجة للأدوات بدون `required` (الحالة الأولى: حقن `"_step": {"type": "string"}` و`required: ["_step"]` للأدوات ذات `properties: {}`؛ الحالة الثانية: ترقية أول خاصية اختيارية مع `nullable: true` أو `""` في وصفها إلى `required` للأدوات ذات `required: []`).
* **بوابة التحقق (Verification Gate 2):**
  * التحقق من تطهير مخططات الأدوات من كافة المفاتيح العشرين غير المدعومة، ونجاح معالجة الأدوات الصفرية في الحالتين، وتحويل ترويسات النظام المحظورة في WAF.

### المرحلة الثالثة: بناء وحدات المصادقة، واكتشاف النماذج، وآلة إعادة ترطيب السياق (المستوى 2)

* **الملفات المستهدفة:** `core/auth_oauth_cli.py`, `core/auth_web.py`, `core/model_registry.py`, `core/prefix_differ.py`
* **المهام التنفيذية:**
  1. تنفيذ مصادقة OAuth 2.0 + PKCE (`S256`) في `core/auth_oauth_cli.py` بالحزم الخمس المطلوبة، واستدعاء `https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` (مع حظر `daily-cloudcode-pa.googleapis.com`)، وتجديد التوكنات قبل 5 دقائق من انتهائها عبر `token_refresh_lock`، ودعم وضعي `http_v1internal` و `agy_subprocess`.
  2. تنفيذ إدارة جلسات الويب في `core/auth_web.py` (الاستيراد من ملف تعريف Firefox معزول أو اللصق المباشر من نافذة خاصة مغلقة)، وتهيئة `GeminiClient` بضبط `await client.init(timeout=30, auto_close=False, auto_refresh=True)`، وحفظ `client.cookies` ذرياً في `config/cookies_cache/{account_id}.json` تحت حماية `cookie_write_lock`.
  3. تنفيذ `core/model_registry.py` لاكتشاف النماذج عبر `client.list_models()` و`client.resolve_model(name)` في قناة الويب، وعبر `:fetchAvailableModels` (مع التحول التلقائي إلى `:retrieveUserQuota` عند خطأ `403 Forbidden`) في قناة OAuth، وترجمة `reasoning_effort` ولواحق النماذج (`-low`, `-medium`, `-high`) مع خفض المستوى افتراضياً إلى `low` في المهام الروتينية.
  4. تنفيذ `core/prefix_differ.py` شاملاً الوضع التكيفي للجلسات (`temporary=True` للطلبات المنفردة البسيطة بدون `tools` وبدون سجل سابق، و`temporary=False` لسلاسل الوكلاء متعددة الجولات أو المحتوية على `tools` مع حذف المحادثات الخاملة بعد 60 دقيقة)، وآلة إعادة ترطيب السياق ثلاثية المستويات (المستوى 1: تطابق الحساب والبادئة مع `conversation_locks[cid]` وإرسال `Delta` وصياغة `<tool_response>`؛ المستوى 2: دمج `messages[:-1]` في `<conversation_history>` للسياقات $\le 60,000$ حرف؛ المستوى 3: كتابة `messages[:-3]` في `harness_history_{uuid}.txt` داخل `TEMP_HISTORY_DIR` العابر للأنظمة للسياقات $> 60,000$ حرف مع تثبيت `system` و`tools` و`messages[-3:]` في `prompt` والحذف في `finally:` وعند إقلاع الخادم).
* **بوابة التحقق (Verification Gate 3):**
  * التحقق من عمل مستويات ترطيب السياق الثلاثة، واختيار `TEMP_HISTORY_DIR` الصحيح بين `/dev/shm` و `tempfile.gettempdir()`، وحذف ملفات `harness_history_*.txt` المؤقتة في `finally:` وعند الإقلاع.

### المرحلة الرابعة: بناء عملاء القناتين ومراقب الحصص الاستباقي (المستوى 3)

* **الملفات المستهدفة:** `core/oauth_cli_channel.py`, `core/web_channel.py`, `core/usage_monitor.py`
* **المهام التنفيذية:**
  1. تنفيذ `core/oauth_cli_channel.py` لتحويل الأدوار إلى `contents` (`user`, `model`, `functionResponse`)، واسترجاع `thoughtSignature` من `db_signatures` أو حقن `"skip_thought_signature_validator"`، وإعادة حقن `"_step": "done"` في `functionCall.args` التاريخية، ووضع صور مخرجات الأدوات كعنصر `inlineData` داخل `functionResponse.parts`، وإلحاق رسالة `"Continue."` بدور `user` إذا كان `contents[-1]["role"] == "model"`، وإرسال الطلب إلى `v1internal:streamGenerateContent?alt=sse` بدون `"requestType": "agent"`, وفك تغليف البث.
  2. تنفيذ `core/web_channel.py` لبث `chunk.thoughts` كـ `delta.reasoning_content`، وتمرير `chunk.text_delta` عبر محلل آلة الحالة ذي الـ 64 حرفاً (`TEXT_STREAMING`, `LOOKAHEAD_BUFFERING`, `TOOL_CAPTURING`) مع عداد `tool_call_index` وعَلَم `has_emitted_tool_call` لكبح المسافات والنصوص الفاصلة وختم البث بـ `finish_reason: "tool_calls"` أو `"stop"` عند انتهاء مولد البث الأصلي.
  3. تنفيذ `core/usage_monitor.py` لمراقبة حصة الويب عبر RPC `jSf9Qc` (`batchexecute` من `/usage`) مع كشف التجديد المقاوم للانجراف واحتساب `optimistic_5h_delta` بعد اكتمال الطلب بنجاح فقط، ومراقبة حصة OAuth والتمييز بين `429` المحتوي على `quotaResetDelay` والرفض العابر، وتعطيل الحساب (`status = "SUSPENDED"`) عند `403 PERMISSION_DENIED` أو `Terms of Service violation`.
* **بوابة التحقق (Verification Gate 4):**
  * التحقق من بناء غلاف `v1internal` السليم وحقن `thoughtSignature` و`_step` التاريخي في `oauth_cli_channel.py`، والتحقق من التقاط استدعاءات الأدوات المتعددة في `web_channel.py` دون تسرب نصوص فاصلة بعد بدء الأدوات.

### المرحلة الخامسة: بناء محرك التوجيه الرياضي ومصافحة البث غير الفارغ (المستوى 4)

* **الملفات المستهدفة:** `core/hybrid_scheduler.py`, `core/failover.py`
* **المهام التنفيذية:**
  1. تنفيذ قواعد توجيه القناة، ومعادلة $P_{\text{oauth}, k}$ وبوابة $G_{\text{oauth}, k}$، ومعادلة $P_{\text{web}, i}$ وبوابة $G_{\text{web}, i}$ مع مقام التوازي المشروط $D_{\text{conc}, i}$ ومعامل ألفة الجلسة $S_i = 1.35$ (تحت سقف `120,000` حرف) ومعامل قرب التجديد $\tau_{\text{web}, i}$ ودالة $\Delta_{5h}(m, L_{\text{chars}})$ وتنظيم الإيقاع الذكي (`Smart Pacing`) في `core/hybrid_scheduler.py`.
  2. تنفيذ مصافحة البث غير الفارغ (`Non-Empty Pre-Stream Handshake`) في `core/failover.py` لسحب القطع الأولية في `buffered_chunks` داخل `try/except` حتى التقاط أول قطعة غير فارغة تحتوي فعلياً على `content` أو `reasoning_content` أو `tool_calls` قبل بناء `StreamingResponse`، والتحويل الفوري لحساب بديل عند الفشل ثم تمرير `chain(buffered_chunks, stream_gen)`.
* **بوابة التحقق (Verification Gate 5):**
  * التحقق الرياضي من بقاء أولوية الحساب صاحب الجلسة المفتوحة $\frac{S_i}{D_{\text{conc}, i}} = \frac{1.35}{1.25} = 1.08 > 1.0$ عند $C_{\text{web}, i} = 1$، والتحقق من التحويل الفوري غير المنقطع (`Zero-Drop`) في `failover.py` عند إرجاع بث فارغ أو خطأ من الحساب الأول.

### المرحلة السادسة: بناء نقاط ربط OpenAI، ولوحة التحكم، وأوامر التشغيل (المستوى 5)

* **الملفات المستهدفة:** `api/openai_routes.py`, `api/dashboard_routes.py`, `static/index.html`, `main.py`
* **المهام التنفيذية:**
  1. تنفيذ نقاط ربط `GET /v1/models` و `POST /v1/chat/completions` في `api/openai_routes.py`.
  2. تنفيذ نقاط ربط الإدارة الحية (`GET /api/status`, `POST /api/oauth-accounts/init`, `DELETE /api/oauth-accounts/{id}`, `POST /api/web-accounts/add`, `DELETE /api/web-accounts/{id}`, `POST /api/accounts/sync-quotas`) في `api/dashboard_routes.py` ولوحة التحكم `static/index.html`.
  3. ربط جدولة المهام الخلفية الدورية عبر `apscheduler` وأوامر CLI (`--serve`, `--add-oauth`, `--add-web`) في `main.py`.
* **بوابة التحقق النهائية (Verification Gate 6):**
  * تشغيل الخادم عبر `python main.py --serve --port 8000` والتحقق من استجابة `/v1/models` و `/v1/chat/completions` و `/dashboard` وعمل الإضافة الحية للحسابات وفق إعدادات الربط مع **Pi** و **Hermes**.

# 06. سجل القرارات المعمارية والمبررات التجريبية (`06_ARCHITECTURE_DECISIONS_ADR.md`)

توثق هذه الوثيقة القرارات المعمارية والمبررات الهندسية ("لماذا") والمراجع البرمجية المباشرة المعتمدة في وثيقة المواصفات `v3.2`.

## ADR-01: الفصل والتكامل الديناميكي بين قناتي `OAuth CLI Pool` و `Web Cookie Pool`

* **السياق:** يوفر اشتراك **Google AI Pro** نموذج `gemini-4-argon` على تطبيق الويب ولا يوفره على منصة Antigravity، في حين تستهلك طلبات الوكلاء الفرعيين السريعة نافذة الـ 5 ساعات الخاصة بواجهة الويب إذا مرت عبرها.
* **القرار:** توجيه طلبات الوكلاء السريعة ونماذج `gemini-3.8-flash` و `gemini-3.1-flash-lite` إلى `OAuth CLI Pool` كمسار أساسي (مع إسناد تلقائي Cross-Channel Failover إلى `Web Cookie Pool` عند انشغال أو تهدئة جميع حسابات OAuth)، وتوجيه النماذج الفائقة والثقيلة (`gemini-4-argon` و `gemini-3.1-pro`) إلى `Web Cookie Pool` لحماية حسابات `OAuth CLI` من الاستنزاف السريع للحصة المشتركة.
* **المراجع المباشرة:**
  * [gemini-3.8-flash Documentation](https://ai.google.dev/gemini-api/docs/models/gemini-3.8-flash)
  * [gemini-3.1-flash-lite Documentation](https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite)

## ADR-02: اعتماد سجل الذاكرة الموحد (`In-Memory Authority`) مع التفريغ الذري المحمي بـ `accounts_file_lock`

* **السياق:** منع أي تنافس (`Race Condition`) بين تحديثات `usage_monitor.py` وإضافات `dashboard_routes.py` دون التضحية بقابلية قراءة ملف `config/accounts.json`.
* **القرار:** قراءة `accounts.json` مرة واحدة عند إقلاع الخادم إلى كائنات Python في الذاكرة لتكون المصدر الوحيد للحقيقة أثناء التشغيل، وحظر عمليات `Read-Modify-Write` من القرص، وتمرير كل عملية حفظ (`save_accounts_snapshot()`) عبر قفل `accounts_file_lock = asyncio.Lock()` الذي يفرغ حالة الذاكرة إلى `accounts.json.tmp` ثم يستبدله ذرياً عبر `os.replace()`.

## ADR-03: تجنب متصفحات Chromium بسبب تشفير DBSC وعزل الكوكيز وحماية الإغلاق المتزامن

* **السياق:** يؤدي تشفير **DBSC (Device Bound Session Credentials)** في متصفحات Chromium إلى إبطال الكوكيز المستخرجة خلال ساعات. كما أن بقاء نافذة التصفح مفتوحة يتنافس مع الخادم على تدوير `__Secure-1PSIDTS`، وتستمر ميزة "المعلومات المحفوظة (Saved Info)" في التأثير على مخرجات النموذج حتى مع `temporary=True`.
* **القرار:** استيراد الكوكيز من متصفح **Firefox** حصراً أو لصقها من نافذة خاصة تُغلق فوراً دون تسجيل خروج، وتعطيل `Saved Info`، وتعطيل المجلد المشترك `GEMINI_COOKIE_PATH` لصالح ملفات معزولة `config/cookies_cache/{account_id}.json` محمية بـ `cookie_write_lock`، وحماية إغلاق العميل المشترك (`GeminiClient.close()`) عبر `reinit_lock` أثناء وجود طلب ثانٍ قيد البث ($C_{\text{web}, i} > 0$).
* **المراجع المباشرة:**
  * [HanaokaYuzu/Gemini-API Repository](https://github.com/HanaokaYuzu/Gemini-API)
  * [gemini-webapi on PyPI](https://pypi.org/project/gemini-webapi/)

## ADR-04: إزالة حقل `requestType: "agent"` ودرع WAF لترويسات النظام في قناة `OAuth CLI`

* **السياق:** يتسبب حقل `"requestType": "agent"` في إخضاع الطلب لدلو حصص مقيد وإرجاع أخطاء `429 RESOURCE_EXHAUSTED` زائفة على حسابات Pro، كما تحظر جدارية جوجل (ESF/WAF) وسم `<system-conventions>` المقترن بسطر `RFC 2119` بردود `429` زائفة.
* **القرار:** حذف حقل `"requestType": "agent"` نهائياً من غلاف طلبات `v1internal`، واستبدال وسم `<system-conventions>` بـ `<conventions>` مع فصل سطر `RFC 2119: MUST, REQUIRED...` عن وسم الافتتاح.
* **المراجع المباشرة:**
  * [can1357/oh-my-pi Issue #11883](https://github.com/can1357/oh-my-pi/issues/11883)
  * [can1357/oh-my-pi Issue #11719](https://github.com/can1357/oh-my-pi/issues/11719)
  * [can1357/oh-my-pi Issue #11699](https://github.com/can1357/oh-my-pi/issues/11699)
  * [can1357/oh-my-pi Issue #12029](https://github.com/can1357/oh-my-pi/issues/12029)

## ADR-05: المعالجة المزدوجة للأدوات بدون `required` واتساق السجل التاريخي (`History Replay Consistency`)

* **السياق:** يفشل `cloudcode-pa` في إصدار `functionCall` بعد التفكير إذا كانت مصفوفة `required` فارغة (`[]`) حتى لو وُجدت خواص اختيارية في `properties` (مثل `take_snapshot` أو `list_dir`).
* **القرار:** للأدوات الخالية من الخواص إطلاقاً (`properties: {}`) يُحقن `"_step": {"type": "string"}` في `required` ويُحذف من استجابة النموذج قبل إرسالها للوكيل ثم يُعاد حقن `"_step": "done"` في `functionCall.args` التاريخية عند إعادة بناء `request.contents`؛ وللأدوات ذات الخواص الاختيارية و`required: []` تُرقّى أول خاصية اختيارية موجودة أصلاً (مع `nullable: true` أو قيمة افتراضية فارغة `""` في وصفها) إلى `required` دون حقن `_step`.
* **المراجع المباشرة:**
  * [router-for-me/CLIProxyAPI Issue #739](https://github.com/router-for-me/CLIProxyAPI/issues/739)
  * [PicoClaw Antigravity Provider Docs](https://docs.picoclaw.io/docs/providers/antigravity/)

## ADR-06: حقن `thoughtSignature` وتداخل صور الأدوات وحارس نهاية الدور النهائي

* **السياق:** تشترط نماذج Gemini 3/4 وجود `thoughtSignature` في أجزاء `functionCall` بالجولات المتعددة، وترفض الصور الموضوعة خارج `functionResponse.parts`، وترفض الطلبات المنتهية بدور `model`.
* **القرار:** استرجاع البصمة الأصلية من جدول `db_signatures` في `LMDB` أو حقن `"thoughtSignature": "skip_thought_signature_validator"`، ووضع صور الأدوات كعنصر `inlineData` داخل `functionResponse.parts`، وإلحاق رسالة `{"role": "user", "parts": [{"text": "Continue."}]}` إذا انتهى `contents` بدور `model`.
* **المراجع المباشرة:**
  * [cline/cline Issue #7974](https://github.com/cline/cline/issues/7974)
  * [router-for-me/CLIProxyAPI Issue #2098](https://github.com/router-for-me/CLIProxyAPI/issues/2098)
  * [router-for-me/CLIProxyAPI Issue #5070](https://github.com/router-for-me/CLIProxyAPI/issues/5070)
  * [router-for-me/CLIProxyAPI Issue #5415](https://github.com/router-for-me/CLIProxyAPI/issues/5415)
  * [router-for-me/CLIProxyAPI Issue #5358](https://github.com/router-for-me/CLIProxyAPI/issues/5358)

## ADR-07: موازنة معادلة التوجيه في قناة الويب عبر مقام التوازي المشروط بالألفة ($D_{\text{conc}, i}$)

* **السياق:** القسمة الدائمة على $(1 + C_{\text{web}, i})^2$ عند وجود طلب فرعي واحد قيد التنفيذ ($C_{\text{web}, i} = 1 \implies \text{المقام} = 4$) تخسف أولوية الحساب صاحب الجلسة المفتوحة إلى $\frac{1.35}{4} = 0.3375$ مقابل $1.0$ لأي حساب فارغ، مما يدمر ألفة الجلسة ($S_i = 1.35$).
* **القرار:** فصل مقام التوازي المشروط $D_{\text{conc}, i}$ ليكون خطياً خفيفاً $1 + 0.25 \times C_{\text{web}, i}$ عند استكمال محادثة مفتوحة ($S_i = 1.35$ فتظل النسبة $\frac{1.35}{1.25} = 1.08 > 1.0$)، وتربيعياً $(1 + C_{\text{web}, i})^2$ للمحادثات الجديدة ($S_i = 1.0$)، مع تقييد ألفة الجلسة بسقف تدوير `120,000` حرف وربط تنظيم الإيقاع ببداية الطلب ($T_{\text{start}}$).
* **المراجع المباشرة:**
  * [router-for-me/CLIProxyAPI Repository](https://github.com/router-for-me/CLIProxyAPI)

## ADR-08: التوافق العابر للأنظمة في تمرير ملفات السياق الضخم ($> 60,000$ حرف)

* **السياق:** عدم وجود المسار `/dev/shm` على أنظمة Windows و macOS يسبب انهيار الخادم بخطأ `FileNotFoundError`.
* **القرار:** فحص وجود `/dev/shm` وقابليته للكتابة برمجياً والانتقال التلقائي إلى `tempfile.gettempdir()`، وكتابة الرسائل القديمة (`messages[:-3]`) في `harness_history_{uuid}.txt` مع إبقاء `system` و`tools` و`messages[-3:]` في `prompt`، وحذف الملف في `finally:` وعند إقلاع الخادم.
* **المراجع المباشرة:**
  * [Nativu5/Gemini-FastAPI Repository](https://github.com/Nativu5/Gemini-FastAPI)

## ADR-09: دعم استدعاءات الأدوات المتعددة والمتتالية في محلل البث الاستباقي لقناة الويب

* **السياق:** تستدعي نماذج مثل `gemini-4-argon` عدة أدوات متوازية في رسالة واحدة وقد تسبقها بكتل ```` ```xml\n<tool_call> ```` أو تفصل بينها بأسطر جديدة ومسافات فارغة.
* **القرار:** بناء محلل آلة حالة من 3 أطوار (`TEXT_STREAMING`, `LOOKAHEAD_BUFFERING` بسعة 64 حرفاً, `TOOL_CAPTURING`) مع عداد `tool_call_index` وعَلَم `has_emitted_tool_call` الذي يكبح تسرب أي مسافات أو نصوص فاصلة بعد بدء الأدوات، وعدم إرسال حزمة الختم إلا بعد انتهاء مولد البث الأصلي بالكامل.

## ADR-10: مصافحة البث غير الفارغ قبل الإرجاع (`Non-Empty Pre-Stream Handshake`)

* **السياق:** في `FastAPI` لا يمكن التراجع عن حالة `HTTP 200` بعد إرجاع `StreamingResponse`، وقد ترجع خوادم `cloudcode-pa` حالة `HTTP 200 OK` مع حزمة SSE فارغة للنماذج المقيدة.
* **القرار:** سحب القطع الأولية في `buffered_chunks` داخل حلقة المحاولة (`try/except`) حتى التقاط أول قطعة غير فارغة تحتوي فعلياً على `content` أو `reasoning_content` أو `tool_calls` قبل بناء `StreamingResponse`، والتحويل الفوري لحساب بديل عند الفشل ثم تمرير `chain(buffered_chunks, stream_gen)`.
* **المراجع المباشرة:**
  * [PicoClaw Antigravity Provider Docs](https://docs.picoclaw.io/docs/providers/antigravity/)
  * [router-for-me/CLIProxyAPI Issue #5722](https://github.com/router-for-me/CLIProxyAPI/issues/5722)

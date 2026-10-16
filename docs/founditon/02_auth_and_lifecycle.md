# 02. المصادقة، واكتشاف النماذج، ومراقبة الحصص (`02_AUTH_AND_LIFECYCLE.md`)

## 1. التسجيل الديناميكي المعزول للقناتين (`core/auth_oauth_cli.py` & `core/auth_web.py`)

### أولاً: تسجيل حسابات `OAuth CLI / Antigravity 2.0` (`core/auth_oauth_cli.py`)

* **تحذير تشغيلي ووضع النقل المزدوج:** نظراً لسياسات جوجل في مراقبة استخدام توكنات `cloudcode-pa` عبر وكلاء الطرف الثالث، وإيقاف أداة `Gemini CLI` القديمة في 18 يونيو 2026 لصالح منصة `Antigravity 2.0`، يجب استخدام حسابات ثانوية مخصصة للعمل، مع دعم وضعين للنقل (`transport_mode`):
  * **الوضع المباشر (`http_v1internal` - الافتراضي للسرعة):** يتصل مباشرة عبر `httpx` بنقطة `https://cloudcode-pa.googleapis.com/v1internal` مع تطبيق درع الترويسات المحدث.
  * **وضع العملية الفرعية الآمن (`agy_subprocess` - خيار أمان إضافي):** يشغل الأداة الرسمية `agy --input-format stream-json --output-format stream-json` كعملية فرعية معزولة لكل حساب بحيث تبقى التوكنات وخيار `useG1Credits` داخل الملف التنفيذي الموقّع من جوجل.
* عند طلب إضافة حساب جديد (`POST /api/oauth-accounts/init` أو `--add-oauth`)، يولد النظام زوج مفاتيح **PKCE** (`code_verifier` و `code_challenge` بطريقة `S256`) ويفتح رابط المصادقة بالحزم المطلوبة (`cloud-platform`, `userinfo.email`, `userinfo.profile`, `cclog`, `experimentsandconfigs`) مع خادم استماع محلي مؤقت أو الوضع اليدوي بنسخ رابط Callback للخوادم البعيدة/WSL2.
* بعد استبدال الكود بـ `access_token` و `refresh_token`، يستدعي النظام نقطة الإنتاج الحصرية `https://cloudcode-pa.googleapis.com/v1internal:loadCodeAssist` (مع حظر النطاق التجريبي `daily-cloudcode-pa.googleapis.com`) لجلب معرف المشروع المدار `cloudaicompanionProject` وحالة الشريحة `paidTier`.
* تُجدد التوكنات تلقائياً قبل انتهاء صلاحيتها بـ 5 دقائق عبر `token_refresh_lock`، وتُفرغ حالة الحسابات إلى `config/accounts.json` عبر `accounts_file_lock`.

### ثانياً: تسجيل حسابات `Web Pool` وحماية التوازي وخطاف الكوكيز (`core/auth_web.py`)

* يتجنب النظام متصفحات Chromium تماماً لتفادي إبطال الكوكيز الناتج عن تقنية **DBSC (Device Bound Session Credentials)**.
* يتيح طريقتين آمنتين ومستقرتين لإضافة الحسابات (`POST /api/web-accounts/add` أو `--add-web`):
  * **الطريقة أ (الاستيراد من ملف تعريف Firefox معزول):** قراءة `__Secure-1PSID` و `__Secure-1PSIDTS` مباشرة من قاعدة بيانات SQLite الخاصة بملف تعريف Firefox محدد.
  * **الطريقة ب (اللصق المباشر من نافذة خاصة مغلقة):** نسخ قيمتي الكوكيز بعد تسجيل الدخول في نافذة تصفح خاصة (Private/Incognito Window) وإغلاق النافذة فوراً دون تسجيل خروج حتى لا تتنافس الجلسة المفتوحة مع الخادم على تدوير `__Secure-1PSIDTS`.
* **تعطيل ذاكرة الحساب المخصصة (`Saved Info Hygiene`):** يُوجه المستخدم عند إعداد حسابات الويب لتعطيل ميزة "المعلومات المحفوظة (Saved Info)" في إعدادات Gemini Web، نظراً لأن ذاكرة الحساب تستمر في التأثير على مخرجات النموذج حتى عند تفعيل `temporary=True`.
* **حماية التوازي وخطاف حفظ الكوكيز الخلفي المعزول (`Concurrency Guard & Background Cookie Hook`):**
  * يُنشئ النظام كائن `GeminiClient(secure_1psid, secure_1psidts, proxy)` مستقل لكل حساب مع ضبط `await client.init(timeout=30, auto_close=False, auto_refresh=True)`.
  * في `gemini-webapi v2.1.1`، يتم تدوير `__Secure-1PSIDTS` عبر مهمة خلفية دورية (`RotateCookies`) بالإضافة إلى تحديثات `Set-Cookie` في ذاكرة `httpx`. يعطل النظام الاعتماد على المجلد المشترك `GEMINI_COOKIE_PATH` ويحفظ `client.cookies` ذرياً في `config/cookies_cache/{account_id}.json` تحت حماية `cookie_write_lock` بعد الطلبات وكل 5 دقائق وعند إيقاف الخادم.

## 2. مكتشف النماذج الديناميكي ومترجم التفكير (`core/model_registry.py`)

1. **في قناة `Web Pool`:**
   * يستدعي النظام `await client.init()` ثم `client.list_models()` المتاحة في `gemini-webapi v2.1.1+` لاكتشاف النماذج المتاحة فعلياً للحساب وبناء ترويسات النموذج ديناميكياً عبر `client.resolve_model(name)`.
2. **في قناة `OAuth CLI Pool`:**
   * يستدعي النظام `POST https://cloudcode-pa.googleapis.com/v1internal:fetchAvailableModels` مع تمرير `{"project": project_id}`.
   * **المسار الاحتياطي التلقائي لحسابات Pro القياسية:** إذا أرجعت نقطة `:fetchAvailableModels` خطأ `403 Forbidden`، يتحول النظام تلقائياً لاستدعاء `POST https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuota` لاستخراج قائمة النماذج الفعالة وحصصها.
3. **توحيد وترجمة مستويات التفكير (`Thinking / Reasoning Effort Mapping`):**
   * يدعم النظام استقبال معامل `reasoning_effort` (`minimal`, `low`, `medium`, `high`) أو لواحق أسماء النماذج (مثل `gemini-3.8-flash-high` أو `gemini-4-argon-low`) ويترجمها تلقائياً إلى `generationConfig.thinkingConfig` في قناة OAuth وإلى ترويسة النموذج المقابلة في قناة الويب، مع خفض المستوى افتراضياً إلى `low` في المهام الروتينية لتقليل استنزاف الحصة الحاسوبية.

## 3. مراقب الحصص الاستباقي (`core/usage_monitor.py`)

1. **مراقبة حصة حسابات `Web Pool` الموثقة (`Authenticated RPC jSf9Qc`):**
   * يستخدم المراقب الدوري جلسة `GeminiClient` المهيأة أصلاً لكل حساب ويب (أو روتين الفحص `inspect` المدمج في `gemini-webapi v2.1.1`) لاستدعاء RPC `jSf9Qc` عبر `batchexecute` من صفحة `https://gemini.google.com/usage` واستخلاص إطارات XSSI لتحديث `five_hour_pct`، و`weekly_pct`، ومواعيد التجديد (`five_hour_reset_at` و`weekly_reset_at`) في كائنات الذاكرة ثم تفريغها ذرياً إلى `accounts.json` عبر `accounts_file_lock`.
   * **كشف التجديد المقاوم للانجراف (`Drift-Safe Reset Detection`):** نظراً لأن وقت التجديد الذي ترجعه جوجل ينجرف للأمام مع كل استعلام عندما يكون الحساب خاملاً، لا يقارن النظام مواعيد التجديد بالمساواة (`==`)، بل يصفر العدادات التقديرية (`optimistic_5h_delta = 0.0`) عند نجاح الاستعلام أو عند عبور الوقت الحالي لموعد `five_hour_reset_at` المسجل سابقاً.
   * لا يُضاف `optimistic_5h_delta` إلا بعد اكتمال الطلب بنجاح، لأن الطلبات التي تنتهي بخطأ خادم لا تُحتسب من حصة الـ 5 ساعات.
2. **مراقبة حصة حسابات `OAuth CLI Pool` الاستباقية والتمييز الذكي لأخطاء `429`:**
   * يستعلم المراقب دورياً من `:fetchAvailableModels` (أو `:retrieveUserQuota`) لتحديث `remaining_fraction` و`reset_at` لكل نموذج في الذاكرة وحفظها عبر `accounts_file_lock`.
   * عند حدوث خطأ `429` أثناء الطلب على قناة OAuth، يفحص النظام متن الخطأ (`error.details`):
     * إذا احتوى على `quotaResetDelay` (مثل `"4h30m28s"`), يُوضع هذا النموذج/الحساب في `COOLDOWN` للمدة المحددة بدقة.
     * إذا لم يحتوِ على `quotaResetDelay` (خطأ معدل طلبات لحظي أو رفض عابر)، **لا يُوضع الحساب في `COOLDOWN` طويل**، بل يُعاد توجيه الطلب الحالي لحساب آخر فوراً.
     * إذا ظهر خطأ `403 PERMISSION_DENIED` أو `Terms of Service violation`، يُعطل الحساب فوراً (`status = "SUSPENDED"`) عبر `accounts_file_lock` ويُرسل تنبيه فوري في لوحة التحكم.

# 01. هياكل البيانات وإدارة الحالة الموحدة بدون تنافس (`01_DATA_SCHEMAS_AND_STATE.md`)

## 1. إدارة الحالة الموحدة بدون تنافس (Race-Free State Management)

يُحفظ ملف `config/accounts.json` بصيغة مقروءة بشرياً تضم مصفوفتي `oauth_accounts` و `web_accounts`. ولمنع أي تنافس (`Race Condition`) بين تحديثات `usage_monitor.py` وإضافات `dashboard_routes.py` دون التضحية بقابلية قراءة ملف JSON:

1. **الذاكرة هي المصدر الوحيد للحقيقة أثناء التشغيل (`In-Memory Single Source of Truth`):** تُقرأ بيانات `accounts.json` مرة واحدة عند إقلاع الخادم إلى كائنات Python في الذاكرة؛ وتُنفذ جميع القراءات والتعديلات اللحظية (بما في ذلك الإضافة الحية `Hot-Plug` وتحديث الحصص) على الكائنات في الذاكرة مباشرة.
2. **التفريغ الذري المحمي بقفل مركزي (`Atomic Snapshot Persistence`):** لا يقوم أي مكون بعملية `Read-Modify-Write` من القرص أثناء التشغيل؛ بل تمر كل عملية حفظ (`save_accounts_snapshot()`) عبر قفل واحد `accounts_file_lock = asyncio.Lock()` يفرغ حالة الذاكرة الحالية إلى `accounts.json.tmp` ثم يستبدله ذرياً عبر `os.replace()`.

## 2. الهيكل التمثيلي التعريفي لملف الحسابات (`config/accounts.json` Schema Definition)

يمثل المخطط التالي البنية الهيكلية المجردة لملف `config/accounts.json` بمعرفات الأنواع فقط دون أي بيانات محاكاة، حيث تُملأ الحقول الديناميكية (مثل `discovered_models` و`model_quotas` و`usage` وبيانات الجلسة) تلقائياً بواسطة وحدات النظام أثناء التشغيل:

```json
{
  "oauth_accounts": [
    {
      "id": "<string>",
      "label": "<string>",
      "transport_mode": "<string: 'http_v1internal' | 'agy_subprocess'>",
      "max_concurrency": "<int>",
      "proxy": "<string | null>",
      "credentials": {
        "access_token": "<string>",
        "refresh_token": "<string>",
        "expires_at": "<int>",
        "project_id": "<string>",
        "ide_type": "<string>"
      },
      "status": "<string: 'ACTIVE' | 'COOLDOWN' | 'SUSPENDED'>",
      "discovered_models": "<list[string]>",
      "quota_state": {
        "model_quotas": {
          "<model_id>": {
            "remaining_fraction": "<float>",
            "reset_at": "<int | null>",
            "is_exhausted": "<bool>"
          }
        },
        "cooldown_until": "<int | null>",
        "consecutive_errors": "<int>",
        "last_error_reason": "<string | null>"
      }
    }
  ],
  "web_accounts": [
    {
      "id": "<string>",
      "label": "<string>",
      "max_concurrency": "<int>",
      "proxy": "<string | null>",
      "cookie_file": "<string>",
      "cookies": {
        "__Secure-1PSID": "<string>",
        "__Secure-1PSIDTS": "<string>"
      },
      "status": "<string: 'ACTIVE' | 'COOLDOWN' | 'SUSPENDED'>",
      "discovered_models": "<list[string]>",
      "usage": {
        "five_hour_pct": "<float>",
        "five_hour_reset_at": "<int | null>",
        "weekly_pct": "<float>",
        "weekly_reset_at": "<int | null>",
        "last_synced_at": "<int | null>"
      },
      "cooldown_until": "<int | null>",
      "consecutive_errors": "<int>",
      "last_error_reason": "<string | null>"
    }
  ]
}
```

### توصيف الحقول ومصادر التغذية الديناميكية (`Pydantic v2` Structural Contracts)

#### أولاً: هيكل حسابات `oauth_accounts`

| مسار الحقل (Field Path) | النوع البياني (Type) | طبيعة الحقل ومصدر تغذيته في النظام |
| :--- | :--- | :--- |
| **`id`** | `str` | معرف فريد للحساب داخل المسبح. |
| **`label`** | `str` | تسمية وصفية للحساب تُمرر عند التسجيل. |
| **`transport_mode`** | `Literal["http_v1internal", "agy_subprocess"]` | وضع النقل المعتمد للحساب (الاتصال المباشر عبر `httpx` أو العملية الفرعية `agy`). |
| **`max_concurrency`** | `int` | السقف الأقصى للطلبات المتزامنة المسموح بها على الحساب (السقف الافتراضي المرن `3`). |
| **`proxy`** | `str \| None` | عنوان خادم البروكسي المخصص للحساب (إن وُجد). |
| **`credentials.access_token`** | `str` | توكن الوصول الحالي؛ يُجلب ويُجدد تلقائياً عبر `core/auth_oauth_cli.py`. |
| **`credentials.refresh_token`** | `str` | توكن التجديد الدائم الناتج عن مصادقة OAuth 2.0 + PKCE. |
| **`credentials.expires_at`** | `int` | طابع زمني (Unix Timestamp) لانتهاء صلاحية `access_token` (يُجدد قبل انتهائه بـ 5 دقائق). |
| **`credentials.project_id`** | `str` | معرف المشروع المدار (`cloudaicompanionProject`) المكتشف ديناميكياً من نقطة `:loadCodeAssist`. |
| **`credentials.ide_type`** | `str` | معرف بيئة العميل في بروتوكول المصادقة. |
| **`status`** | `Literal["ACTIVE", "COOLDOWN", "SUSPENDED"]` | الحالة التشغيلية للحساب؛ تتغير ديناميكياً وفق استجابات الخادم وحالة الحصة. |
| **`discovered_models`** | `list[str]` | قائمة معرفات النماذج المكتشفة ديناميكياً للحساب عبر `core/model_registry.py` من `:fetchAvailableModels` أو `:retrieveUserQuota`. |
| **`quota_state.model_quotas`** | `dict[str, ModelQuota]` | قاموس ديناميكي يُربط فيه كل معرف نموذج مكتشف (`<model_id>`) بحصته الفعلية (`remaining_fraction: float`, `reset_at: int \| None`, `is_exhausted: bool`) عبر `core/usage_monitor.py`. |
| **`quota_state.cooldown_until`** | `int \| None` | الطابع الزمني لانتهاء فترة التهدئة المحسوبة ديناميكياً من `quotaResetDelay` عند حدوث خطأ `429`. |
| **`quota_state.consecutive_errors`** | `int` | عداد الأخطاء المتتالية اللحظي. |
| **`quota_state.last_error_reason`** | `str \| None` | توصيف آخر خطأ مسجل على الحساب. |

#### ثانياً: هيكل حسابات `web_accounts`

| مسار الحقل (Field Path) | النوع البياني (Type) | طبيعة الحقل ومصدر تغذيته في النظام |
| :--- | :--- | :--- |
| **`id`** | `str` | معرف فريد لحساب الويب داخل المسبح. |
| **`label`** | `str` | تسمية وصفية للحساب تُمرر عند الإضافة. |
| **`max_concurrency`** | `int` | السقف الأقصى للطلبات المتزامنة على الحساب (السقف الافتراضي الآمن `2`). |
| **`proxy`** | `str \| None` | عنوان خادم البروكسي المخصص للحساب (إن وُجد). |
| **`cookie_file`** | `str` | مسار ملف الكوكيز المعزول الخاص بالحساب (`config/cookies_cache/{account_id}.json`). |
| **`cookies.__Secure-1PSID`** | `str` | قيمة كوكي الجلسة الأساسي المستورد من Firefox أو النافذة الخاصة المغلقة. |
| **`cookies.__Secure-1PSIDTS`** | `str` | قيمة كوكي الجلسة المتغير؛ يتم تدويره وتحديثه تلقائياً بالخلفية عبر `gemini-webapi` وحفظه عبر `cookie_write_lock`. |
| **`status`** | `Literal["ACTIVE", "COOLDOWN", "SUSPENDED"]` | الحالة التشغيلية للحساب؛ تتغير ديناميكياً أثناء التشغيل. |
| **`discovered_models`** | `list[str]` | قائمة معرفات النماذج المكتشفة ديناميكياً للحساب عبر `client.list_models()` في `core/model_registry.py`. |
| **`usage.five_hour_pct`** | `float` | نسبة استهلاك نافذة الـ 5 ساعات المقروءة ديناميكياً عبر RPC `jSf9Qc` من صفحة `/usage`. |
| **`usage.five_hour_reset_at`** | `int \| None` | الطابع الزمني لتجديد نافذة الـ 5 ساعات المستخلص ديناميكياً من صفحة `/usage`. |
| **`usage.weekly_pct`** | `float` | نسبة الاستهلاك الأسبوعي المقروءة ديناميكياً عبر RPC `jSf9Qc` من صفحة `/usage`. |
| **`usage.weekly_reset_at`** | `int \| None` | الطابع الزمني لتجديد النافذة الأسبوعية المستخلص ديناميكياً من صفحة `/usage`. |
| **`usage.last_synced_at`** | `int \| None` | الطابع الزمني لآخر مزامنة ناجحة مع صفحة `/usage`. |
| **`cooldown_until`** | `int \| None` | الطابع الزمني لانتهاء فترة التهدئة (إن وُجدت). |
| **`consecutive_errors`** | `int` | عداد الأخطاء المتتالية اللحظي. |
| **`last_error_reason`** | `str \| None` | توصيف آخر خطأ مسجل على الحساب. |

## 3. متغيرات الحالة اللحظية في الذاكرة (In-Memory Runtime State)

تُدار هذه المتغيرات في الذاكرة فقط أثناء التشغيل لكل حساب نشط ولا تُكتب كقيم ثابتة على القرص:

### أولاً: لكل حساب مسجل في `oauth_accounts` (عدد ديناميكي $M$)

* **`active_requests` ($C_{\text{oauth}, k} \in \{0 \dots 3\}$):** عدد الطلبات المتزامنة قيد التنفيذ حالياً على هذا الحساب، مع سقف افتراضي مرن $C_{\text{oauth}, k} \le 3$.
* **`recent_latency_ms` ($L_{\text{oauth}, k}$):** المتوسط المتحرك الأسي (EMA) لزمن الاستجابة بالملي ثانية.
* **`token_refresh_lock` (`asyncio.Lock`):** قفل لمنع إرسال طلبات تجديد توكن متزامنة لنفس الحساب (`In-Flight Deduplication`).

### ثانياً: لكل حساب مسجل في `web_accounts` (عدد ديناميكي $N$)

* **`active_requests` ($C_{\text{web}, i} \in \{0, 1, 2\}$):** عدد الطلبات المتزامنة الجارية حالياً على هذا الحساب، مع سقف أمان افتراضي `max_concurrency = 2` (يسمح بمعالجة طلبين في نفس الوقت دون استثارة فلاتر CAPTCHA في واجهة الويب).
* **`cookie_write_lock` (`asyncio.Lock`):** قفل خفيف يحيط **فقط بعملية كتابة ملف الكوكيز على القرص** (`config/cookies_cache/{account_id}.json`) دون حجب طلبات الشبكة الجارية.
* **`conversation_locks` (`dict[str, asyncio.Lock]`):** قفل خفيف على مستوى **معرف المحادثة الواحدة (`cid`) فقط** لضمان تسلسل الرسائل إذا أرسل الوكيل طلبين لنفس المحادثة المفتوحة (`[cid, rid, rcid]`)، مع السماح بتشغيل محادثة ثانية بالتوازي على نفس الحساب.
* **`reinit_lock` (`asyncio.Lock`):** قفل حماية يمنع إغلاق العميل المشترك (`GeminiClient.close()`) أثناء وجود طلب ثانٍ قيد البث ($C_{\text{web}, i} > 0$) في حال تعثر أحد الطلبين المتوازيين.
* **`last_started_timestamp` ($T_{\text{start}, i}$) و `last_completed_timestamp` ($T_{\text{done}, i}$):** لتنظيم فاصل الإطلاق التكيفي (`WEB_MIN_START_INTERVAL_S = 0.5s` بين بدايتي طلبين متتاليين، أو تهدئة خفيفة قابلة للضبط `0.4s–0.8s` فقط إذا كان الطلب السابق قد استغرق أقل من ثانية واحدة).
* **`optimistic_5h_delta` و `optimistic_weekly_delta`:** نسبة الاستهلاك التقديرية المضافة محلياً (والموزونة بطول السياق) منذ آخر مزامنة فعلية لصفحة `/usage`.

## 4. الهياكل التمثيلية لجداول `LMDB` (`db_sessions` & `db_signatures`)

تدير مكتبة `lmdb` جدولين محليين بالهيكل التعريفي التالي:

| اسم الجدول | هيكل المفتاح (Key Schema) | هيكل القيمة (Value Schema) | الغرض المعماري |
| :--- | :--- | :--- | :--- |
| **`db_sessions`** | `<sha256_prefix_hash: string>`<br>(البصمة التسلسلية لبادئة مصفوفة الرسائل `[m_1 ... m_k]`) | `{"account_id": "<string>", "metadata": ["<cid>", "<rid>", "<rcid>"]}` | ربط بصمة الرسائل بمعرف الحساب وبيانات جلسة الويب المفتوحة (`chat.metadata`) لحساب ألفة الجلسة ($S_i$) واستئناف المحادثة. |
| **`db_signatures`** | `<tool_call_id: string>`<br>(معرف استدعاء الأداة الصادر للوكيل) | `<thoughtSignature: string>`<br>(بصمة التفكير الأصلية الواردة من `v1internal`) | استرجاع `thoughtSignature` وحقنها في أجزاء `functionCall` خلال الجولات المتعددة على قناة `OAuth CLI`. |

## 5. سجل أقفال التزامن (`asyncio.Lock`) ونطاق عملها

| معرف القفل | النطاق الهيكلي | المسؤولية الحصرية |
| :--- | :--- | :--- |
| **`accounts_file_lock`** | قفل مركزي واحد على مستوى الخادم | حماية عملية التفريغ الذري `save_accounts_snapshot()` من الذاكرة إلى `accounts.json.tmp` ثم `os.replace()`. |
| **`token_refresh_lock`** | قفل مستقل لكل كائن حساب في `oauth_accounts` | منع إرسال طلبات تجديد توكن متزامنة لنفس الحساب (`In-Flight Deduplication`). |
| **`cookie_write_lock`** | قفل مستقل لكل كائن حساب في `web_accounts` | حماية عملية كتابة ملف الكوكيز المعزول `config/cookies_cache/{account_id}.json` على القرص دون حجب طلبات الشبكة. |
| **`conversation_locks[cid]`** | قفل مستقل لكل معرف محادثة `cid` داخل حساب الويب | ضمان تسلسل الرسائل التزايدية (`Delta`) لنفس المحادثة المفتوحة (`[cid, rid, rcid]`) مع السماح بمحادثة ثانية بالتوازي على نفس الحساب. |
| **`reinit_lock`** | قفل مستقل لكل كائن حساب في `web_accounts` | منع إغلاق العميل المشترك (`GeminiClient.close()`) أثناء وجود طلب آخر قيد البث ($C_{\text{web}, i} > 0$). |

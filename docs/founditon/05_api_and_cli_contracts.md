# 05. نقاط الربط القياسية وواجهات الإدارة ودليل التشغيل (`05_API_AND_CLI_CONTRACTS.md`)

## 1. نقاط ربط OpenAI القياسية (`http://localhost:8000/v1` — `api/openai_routes.py`)

* **`GET /v1/models`**: تُرجع القائمة المحدثة ديناميكياً لجميع النماذج المكتشفة عبر الحسابات النشطة في القناتين بالإضافة إلى لواحق التحكم في التفكير (`-low`, `-medium`, `-high`).
* **`POST /v1/chat/completions`**: نقطة الاتصال الموحدة لـ **Pi** و **Hermes**، وتدعم البث الحي (`stream: true` عبر `SSE` شاملاً `delta.reasoning_content` و `delta.tool_calls` المتعددة)، والوضع غير المتدفق (`stream: false`)، ومعاملات `tools`، و`tool_choice`، و`reasoning_effort`، و`response_format`، والوسائط المتعددة.

## 2. نقاط ربط الإدارة الديناميكية ولوحة التحكم (`http://localhost:8000/dashboard` — `api/dashboard_routes.py` & `static/index.html`)

* **`GET /api/status`**: تُرجع الحالة الحية لجميع الحسابات المسجلة في `oauth_accounts` و `web_accounts` مباشرة من كائنات الذاكرة، شاملة درجات الأولوية اللحظية، ومعامل قرب التجديد ($\tau_i$)، وعدد الطلبات الجارية ($C_i$ ومقام التوازي المشروط $D_{\text{conc}, i}$)، ونسب الاستهلاك (`5h`, `weekly`, `remaining_fraction`).
* **`POST /api/oauth-accounts/init`** و **`DELETE /api/oauth-accounts/{id}`**: إضافة حساب OAuth CLI جديد حياً عبر رابط مصادقة PKCE أو حذف حساب موجود في الذاكرة وحفظه ذرياً عبر قفل `accounts_file_lock` دون إيقاف الخادم.
* **`POST /api/web-accounts/add`** و **`DELETE /api/web-accounts/{id}`**: إضافة حساب ويب جديد حياً (عبر استيراد ملف Firefox أو لصق الكوكيز من نافذة خاصة مغلقة) مع إنشاء ملف الكوكيز المعزول الخاص به وفحص نماذجه واستهلاكه فوراً.
* **`POST /api/accounts/sync-quotas`**: إجبار الخادم على مزامنة نسب استهلاك `/usage` (`jSf9Qc`) لحسابات الويب وحصص `:fetchAvailableModels` لحسابات OAuth في الحال.

## 3. دليل التشغيل وأوامر الإضافة المرنة (`main.py` CLI)

```bash
# 1. تثبيت المكتبات الأساسية
pip install -U fastapi uvicorn httpx[http2] gemini-webapi[browser] lmdb pydantic apscheduler

# 2. تشغيل السيرفر المحلي (يعمل على Linux و Windows و macOS ويقبل الإضافة الحية من المتصفح)
python main.py --serve --port 8000

# 3. إضافة أي عدد من حسابات OAuth CLI (استخدم حسابات ثانوية) أو حسابات الويب في أي وقت
python main.py --add-oauth --label "<account_label>" --mode "http_v1internal"
python main.py --add-web --firefox-profile "<firefox_profile_name>" --label "<account_label>"
```

## 4. إعدادات الربط داخل Pi / Hermes

```json
{
  "provider": "openai-compatible",
  "base_url": "http://localhost:8000/v1",
  "api_key": "sk-gemini-hybrid-pool",
  "main_agent_model": "gemini-4-argon",
  "sub_agent_model": "gemini-3.8-flash",
  "fallback_model": "gemini-3.1-flash-lite",
  "max_parallel_sub_agents": 8
}
```

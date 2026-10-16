# 04. مستوى البيانات ومحولات البروتوكول وترطيب السياق (`04_PROTOCOL_AND_DATA_PLANE.md`)

بما أن بيئات الوكلاء (**Pi** و **Hermes**) تدير ذاكرتها وتقوم بعمليات ضغط السياق (Compaction) داخلياً، **يُحظر على البوابة تلخيص أو حذف أي رسالة من مصفوفة `messages`**، وتلتزم بالقواعد التنفيذية المحدثة التالية:

## 1. ضاغط مخرجات الأدوات غير المخلّ (`core/token_saver.py`)

* لتقليل استنزاف حصة الـ 5 ساعات دون المساس بمنطق المحادثة، تمر مخرجات الأدوات الخام (`role: "tool"`) اختيارياً عبر فلتر حتمي غير مخلّ (`RTK-Lite Lossless Filter`) يكتشف مخرجات `git diff`، و`grep`، و`find`، و`ls`، وسجلات الاختبارات المتكررة، ويزيل الأسطر المكررة المتتالية وحشو المسافات الفارغة (مما يوفر 20%–40% من حجم التوكنات المرسلة)، مع إمكانية تعطيله بترويسة `X-Token-Saver: off`.

## 2. محول البروتوكول، ودرع WAF، ومعالجة الأدوات الصفرية و`thoughtSignature` في قناة `OAuth CLI` (`core/oauth_cli_channel.py` & `core/schema_sanitizer.py`)

تطبق الوحدة خطوات التحويل الإلزامية التالية:

### أ. فلتر درع WAF لترويسات النظام (`WAF System Prompt Shield`)

* قبل بناء الطلب، تفحص الوحدة رسائل `system` و `developer` وتستبدل البصمات النصية التي تحظرها جدارية جوجل (ESF/WAF) بردود `429` زائفة؛ وتحديداً تحويل وسم `<system-conventions>` إلى `<conventions>` وفصل سطر `RFC 2119: MUST, REQUIRED...` عن وسم الافتتاح.

### ب. تطهير مخططات الأدوات والمعالجة المزدوجة للأدوات بدون `required` (`Zero-Required-Params & History Replay Fix`)

* تُمرر مصفوفة `tools` عبر دالة تنظيف عودية تحذف جميع مفاتيح JSON Schema العشرين غير المدعومة في `cloudcode-pa` (`additionalProperties`, `$schema`, `$id`, `$ref`, `$defs`, `definitions`, `examples`, `patternProperties`, `minLength`, `maxLength`, `minimum`, `maximum`, `multipleOf`, `pattern`, `format`, `minItems`, `maxItems`, `uniqueItems`, `minProperties`, `maxProperties`) وتسطح اتحادات `anyOf` و `oneOf`.
* **المعالجة الدقيقة لعلة توقف `functionCall` بعد التفكير:**
  يفشل `cloudcode-pa` في إصدار `functionCall` بعد التفكير إذا كانت مصفوفة `required` فارغة (`[]`) حتى لو وُجدت خواص اختيارية في `properties` (مثل أداة `take_snapshot` أو `list_dir`). ولحل ذلك دون إرباك النموذج أو إهدار التوكنات:
  1. **الحالة الأولى — أداة بلا خواص إطلاقاً (`properties: {}`):** يحقن المطهر خاصية `"_step": {"type": "string"}` ويضع `required: ["_step"]`، ثم يحذف المفتاح `"_step"` من استجابة النموذج قبل إرسالها للوكيل، **وعند إعادة بناء سجل المحادثة (`request.contents`) في الجولات التالية يعيد حقن `"_step": "done"` في `functionCall.args` التاريخية** لضمان تطابق السجل مع الـ Schema.
  2. **الحالة الثانية — أداة لها خواص اختيارية في `properties` لكن `required: []`:** لا يحقن المطهر معامل `_step` وهمياً، بل يرقّي أول خاصية اختيارية موجودة أصلاً في الأداة (مع إضافة `nullable: true` أو قيمة افتراضية فارغة `""` في وصفها) إلى `required`، مما يعالج العلة دون إضافة أي حقل غريب عن الأداة.

### ج. حقن `thoughtSignature` وتداخل صور الأدوات ومعالجة نهاية الدور

* تُحول أدوار `messages` من (`user`, `assistant`, `tool`) إلى هيكل `contents` (`user`, `model`, `functionResponse`).
* **إلزامية `thoughtSignature` في الجولات المتعددة:** لتفادي خطأ `400 Function call is missing a thought_signature in functionCall parts` الذي تفرضه نماذج Gemini 3/4، تسترجع البوابة البصمة الأصلية المحفوظة في جدول `db_signatures` داخل `LMDB` لمعرف `tool_call_id`، أو تحقن القيمة التجاوزية المعتمدة `"thoughtSignature": "skip_thought_signature_validator"` داخل كل جزء `functionCall`.
* **تداخل صور مخرجات الأدوات (`Tool Image Nesting`):** إذا أرجعت أداة في **Pi** صورة (`inline_data` / `image_url`) ضمن مخرجات `tool`، توضع الصورة كعنصر `inlineData` **داخل** مصفوفة `functionResponse.parts` وليس كجزء مجاور في `user.parts` لتجنب حذفها أو رفض الطلب بخطأ `400`.
* **حارس نهاية الدور النهائي (`Post-Assembly Model-Turn Guard`):** بعد الانتهاء الكامل من بناء مصفوفة `request.contents` وتصفية أي إشارات `compaction_trigger` غير قياسية، إذا كان العنصر الأخير `contents[-1]["role"] == "model"`، تلحق البوابة تلقائياً رسالة `{"role": "user", "parts": [{"text": "Continue."}]}` لمنع خطأ `400 Requests ending with a model turn are not supported`.

### د. الهيكل التمثيلي لغلاف الطلب الخالي من `requestType: "agent"` وفك تغليف البث (`v1internal Envelope`)

* يُرسل الطلب إلى `POST https://cloudcode-pa.googleapis.com/v1internal:streamGenerateContent?alt=sse` بالترويسات القياسية (`User-Agent: antigravity`, `X-Goog-Api-Client: google-cloud-sdk vscode_cloudshelleditor/0.1`) **مع حذف حقل `"requestType": "agent"` نهائياً** لتفادي دلو الحصص المقيد وأخطاء `429 RESOURCE_EXHAUSTED` الزائفة على حسابات Pro:
  ```json
  {
    "project": "<project_id>",
    "model": "<model_id>",
    "request": {
      "contents": "<list[Content]>",
      "systemInstruction": {
        "parts": [
          {
            "text": "<system_instruction_text>"
          }
        ]
      },
      "generationConfig": "<GenerationConfig>",
      "tools": [
        {
          "functionDeclarations": "<list[FunctionDeclaration]>"
        }
      ]
    },
    "userAgent": "antigravity",
    "requestId": "<request_id>"
  }
  ```
* عند استلام أحداث `SSE`، تفك الوحدة الحقل `data.response.candidates[0]` وتحول أجزاء `thought: true` إلى `delta.reasoning_content`، والنص إلى `delta.content`، و`functionCall` إلى `delta.tool_calls` (مع حفظ `thoughtSignature` في `db_signatures` داخل `LMDB`).

## 3. محاكي السياق الهجين العابر للأنظمة في قناة `Web Pool` (`core/prefix_differ.py`)

### أ. الوضع التكيفي للجلسات (`Adaptive Session Mode`)

* **للطلبات المنفردة البسيطة (Single-Turn بدون `tools` وبدون سجل سابق):** تستخدم البوابة `temporary=True` لعدم حفظ المحادثة في سجل حساب جوجل.
* **لسلاسل الوكلاء متعددة الجولات أو الطلبات المحتوية على `tools`:** تستخدم البوابة **`temporary=False`** حتى يحتفظ خادم جوجل ببيانات الجلسة (`chat.metadata = [cid, rid, rcid]`) ويسمح باستئنافها في الجولات التالية، بينما تقوم مهمة خلفية في `apscheduler` بحذف المحادثات من سجل جوجل تلقائياً (`await client.delete_chat(cid)`) بعد مرور 60 دقيقة من الخمول.

### ب. آلة حالة إعادة ترطيب السياق ثلاثية المستويات مع دعم جميع أنظمة التشغيل (`Cross-Platform 3-Tier Context Rehydration`)

تحسب البوابة بصمة تسلسلية (`SHA-256`) لكل رسالة في `messages = [m_1, ..., m_k]`. وعند اختيار حساب الويب $i$، تتبع أحد المسارات الثلاثة التالية:

1. **المستوى الأول — تطابق الحساب والبادئة (`Same-Account Prefix Hit`):** إذا وُجدت جلسة مفتوحة في `LMDB` (`db_sessions`) **على نفس الحساب المختار $i$** تطابق البادئة `[m_1 ... m_{k-d}]`، تستحوذ البوابة على قفل المحادثة `conversation_locks[cid]` وترسل **الرسائل الجديدة فقط (`Delta`: `[m_{k-d+1} ... m_k]`)** داخل نفس جلسة `ChatSession`، مع صياغة مخرجات الأدوات (`role: "tool"`) داخل وسم `<tool_response call_id="..." name="...">...</tool_response>`.
2. **المستوى الثاني — انتقال الجلسة لحساب بديل مع سياق متوسط ($\le 60,000$ حرف):** إذا انتقل الطلب إلى حساب ويب جديد $i$ وكان إجمالي السياق لا يتجاوز `60,000` حرف، تدمج البوابة السجل السابق `messages[:-1]` نصياً داخل كتلة `<conversation_history>...</conversation_history>` متبوعة بالرسالة النشطة `messages[-1]` لإنشاء جلسة جديدة على الحساب $i$، ثم **تربط البصمة التسلسلية الكاملة `[m_1 ... m_k]` في `db_sessions` بمعرف الجلسة الجديد `[cid, rid, rcid]` الخاص بالحساب $i$**.
3. **المستوى الثالث — التمرير الهجين للسياقات الضخمة ($> 60,000$ حرف) عبر مسار مؤقت متوافق مع كافة الأنظمة (`Cross-Platform Temp/RAM Passthrough`):**
   * **اختيار المسار المؤقت المتوافق مع Linux / Windows / macOS:** لتفادي انهيار الخادم بخطأ `FileNotFoundError` عند تشغيله على نظام Windows أو macOS حيث لا يوجد المسار `/dev/shm`، تحدد البوابة مجلد الملفات المؤقتة برمجياً عبر:
     ```python
     TEMP_HISTORY_DIR = "/dev/shm" if (os.path.exists("/dev/shm") and os.access("/dev/shm", os.W_OK)) else tempfile.gettempdir()
     ```
   * **منع تشتت الدور والتنظيف المضمون:** تفرز البوابة **الرسائل القديمة (`messages[:-3]`)** وتكتبها حرفياً في `os.path.join(TEMP_HISTORY_DIR, f"harness_history_{uuid}.txt")`، وتمررها عبر `files=[temp_file_path]` ثم تحذف الملف فور انتهاء الطلب داخل كتلة `finally:`، بالإضافة إلى مهمة تنظيف عند إقلاع الخادم تمسح أي ملفات `harness_history_*.txt` يتيمة متبقية على القرص في حال الإغلاق القسري (`SIGKILL`).
   * **تثبيت التعليمات والرسائل النشطة في الحقل المباشر:** تضع البوابة **`system` prompt، وتعريفات `tools`، وآخر 3 رسائل (`messages[-3:]`)** مباشرة في حقل النص الأساسي (`prompt`) مع إشارة صريحة للنموذج بأن السجل السابق مرفق في الملف.

## 4. جسر استدعاء الأدوات المتعددة ذو المخزن الاستباقي وبث التفكير للويب (`core/web_channel.py`)

* **بث التفكير الحي (`Live Reasoning Stream`):** أثناء البث التزايدي عبر `generate_content_stream` أو `send_message_stream` في `gemini-webapi v2.1.1`، ترصد الوحدة أي محتوى جديد في `chunk.thoughts` وتبثه فوراً كحقل `delta.reasoning_content` إلى الوكيل لمنع انقطاع الاتصال بسبب مهلة الصمت (`TTFT Timeout`) أثناء تفكير `gemini-4-argon`.
* **المحلل التدقيقي الاستباقي الداعم لاستدعاءات الأدوات المتعددة في رسالة واحدة (`Multi-Tool 64-Char Lookahead State-Machine Parser`):**
  * عند وجود مصفوفة `tools` في طلب موجه إلى `Web Pool`، تطلب البوابة من النموذج إصدار استدعاءات الأدوات داخل وسم `<tool_call>{"name": "...", "arguments": {...}}</tool_call>`.
  * نظراً لأن نماذج مثل `gemini-4-argon` تستدعي غالباً عدة أدوات متوازية في رسالة واحدة، يحتفظ المحلل بعداد `tool_call_index = 0` وعَلَم `has_emitted_tool_call = False`، ويمرر كل `chunk.text_delta` عبر آلة حالة من 3 أطوار متكررة دون إغلاق البث مبكراً:
    1. **`TEXT_STREAMING`:** يبث النص فوراً كـ `delta.content` حتى يصادف الرمز `<` أو ```` ``` ````. **(ملاحظة حاسمة: بمجرد أن يصبح `has_emitted_tool_call == True`، يكبح هذا الطور إرسال أي مسافات فارغة أو أسطر جديدة `\n` أو علامات ```` ``` ```` متبقية بين وسوم `<tool_call>` المتتالية حتى لا تختلط حزم `delta.content` مع `delta.tool_calls` لدى الوكيل)**.
    2. **`LOOKAHEAD_BUFFERING` (سعة 64 حرفاً):** يحجز الأحرف الواردة مؤقتاً في الذاكرة (بما يكفي لاحتواء ```` ```xml\n<tool_call> ```` والمسافات البادئة) دون إرسالها للوكيل؛ فإذا ثبت عدم احتوائها على `<tool_call>`، يفرغ المخزن كـ `delta.content` (فقط إذا كان `has_emitted_tool_call == False`) ويعود للطور الأول.
    3. **`TOOL_CAPTURING` (مع العودة التلقائية لالتقاط الأدوات التالية):** إذا طابقت البادئة `<tool_call>`، يحول المحلل القطع التالية إلى مجمع JSON داخلي حتى يرصد وسم الإغلاق `</tool_call>`، ويدعم تحليل كائن JSON مفرد أو مصفوفة JSON (`[{...}, {...}]`) عبر `pydantic`، ويصدر كل أداة ككائن `delta.tool_calls` بمعرف فريد `call_{uuid}` وفهرس متسلسل `index: tool_call_index` (`0, 1, 2...`)، ويضبط `has_emitted_tool_call = True`، ثم **يعود فوراً لانتظار أي `<tool_call>` تالٍ في نفس الرسالة**.
  * **ختم البث عند انتهاء توليد النموذج فقط:** لا يرسل المحلل حزمة الختم النهائية إلا بعد انتهاء مولد البث الأصلي من جوجل بالكامل؛ فإذا كان `has_emitted_tool_call == True` يختم البث بـ `finish_reason: "tool_calls"`، وإلا يختمه بـ `finish_reason: "stop"`.

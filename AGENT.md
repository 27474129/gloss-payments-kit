# AGENT.md — перенос оплаты Т-Банка (SberPay · СБП · T-Pay · карта) в кодовую базу Photogen

Инструкция для ИИ-агента владельца проекта. Человеку достаточно `README.md`. Грабли, на которые мы уже наступили, — `GOTCHAS.md` (прочитай **до** кода). Карта всех файлов — `FILES.md`.

**Как устроен репозиторий.** Структура папок = структура вашего Photogen (`app/…`, `scripts/…`, `tests/…`, `miniapp-ui-next/src/…`, `telegram-bot/…`, `max-bot-core/…`), поэтому *новые* файлы копируются «один в один» по тому же пути. Файлы, которые **есть и у вас** (`payments.py`, `max.py`, `yookassa_repo.py`, `support_refund.py`, `pending_payments.py`, файлы бота), лежат у нас **целиком**, но целиком ими свои файлы **не перезаписывай**: рядом с каждым лежит `<имя>.changes.md` — таблица «блок → что сделать», дифф «было → стало» и список недостающего у вас. Наши правки в таких файлах помечены `# --- T-BANK ---` … `# --- /T-BANK ---`.

## Правила (не обсуждаются)

1. Работаешь в своём репозитории, в ветке `feature/tbank-acq`. В `main`/прод ничего не вливаешь и не выкатываешь без слова владельца.
2. Секретов в репозитории нет и не будет (`TBANK_TERMINAL_KEY`, `TBANK_PASSWORD` — только env сервера). Не печатай их.
3. После переноса при **дефолтных** флагах поведение не меняется: Т-Банк выключен, пока не заданы ключи и флаг. Это проверяет шаг B9.
4. Запасной ЮKassa при сбое Т-Банка **нет** (решение владельца): ошибка Т-Банка при оформлении = ошибка покупателю; мониторить. Переключение провайдера флагом/процентом/списком — есть.
5. Если репозиторий отличается от описанного здесь (снимок ваших зеркал — бэкенд 09.11, TG-бот 25.09, MAX-бот 06.09) — **доверяй репозиторию** и запиши расхождение в отчёт владельцу.

## Что у вас уже есть (проверь командами, это снимок 09.11)

```bash
grep -rniE "tbank" app --include=*.py -l          # у вас уже есть app/services/tbank.py и /payments/tbank/webhook
grep -n "TBANK_" app/core/config.py              # TBANK_TERMINAL_KEY/PASSWORD/API_BASE/NOTIFICATION_URL/TEST_USER_IDS/ROLLOUT_PERCENT/CHECKOUT
grep -n "payment_method_type\|def save_payment_method\|provider" app/db/yookassa_repo.py   # скорее всего пусто
ls miniapp-ui/src/lib/miniapp.ts miniapp-ui/package.json     # Mini App «старый» (miniapp-ui); экрана оплаты в нём нет
alembic heads                                     # ваша голова цепочки (на 09.11 — 119_song_text_credit)
```

Вывод: у вас **уже работает** свой Т-Банк (`tb_<PaymentId>`, простая форма/СБП-ссылка). Мы его **заменяем** нашим слоем (один провайдерский слой вместо двух). Мини-апп: стек у `miniapp-ui` тот же (React 19, Tailwind 4, shadcn, `lib/miniapp.ts`), поэтому наш экран оплаты ложится почти без переделок (раздел D).

---

## A. Подготовка

- [ ] Ветка `feature/tbank-acq` от актуального `main`. Прогони тесты, **запомни список упавших до изменений**.
- [ ] `pg_dump` БД перед миграциями на любом окружении с реальными данными.
- [ ] **Платежи в полёте.** `SELECT count(*) FROM yookassa_payments WHERE yookassa_id LIKE 'tb_%' AND status='pending';` Выкатывай только когда ноль (ссылка на оплату живёт до суток; тихий час). Причина: наш `tbank_notify` зачитывает платёж по `provider_order_id`, которого у ваших `tb_`-строк нет. Остатки закрой сверкой с кабинетом Т-Бизнеса. Старые **завершённые** `tb_`-строки не трогай: возврат по ним остаётся «из кабинета Т-Бизнеса», как сейчас (см. `support_refund.py.changes.md`).

## B. Backend — пошаговый чек-лист

**B1. Миграции** (`app/db/migrations/versions/`)
- [ ] Только если в таблице `yookassa_payments` нет колонок `payment_method_type, card_first6, card_last4, card_type, card_country`: `TEMPLATE_yookassa_payment_method.py.txt` → переименуй в `<NNN>_yookassa_payment_method.py`, поставь `revision` и `down_revision` (= ваша голова из `alembic heads`).
- [ ] `TEMPLATE_tbank_acquiring.py.txt` → `<NNN+1>_tbank_acquiring.py`, `down_revision` = предыдущая. Добавляет `provider`, `provider_order_id`, `refunded_rubles` и два индекса; всё `IF NOT EXISTS`.
- [ ] Проверка: `alembic heads` — одна голова; `alembic upgrade head` на копии БД; `downgrade -1` и `upgrade head` снова.

**B2. Настройки** — `app/core/config.py`
- [ ] Вставь поля и свойства из `app/core/config.tbank-block.py.txt` в `class Settings` (одна копия каждого имени, `TBANK_CHECKOUT` удали). Сам `config.py` у нас не лежит (в нём десятки чужих настроек).
- [ ] В `env.example` — имена переменных; реальные значения не коммить.

**B3. Скопируй как есть** (новые файлы, у вас таких нет; путь тот же)
- [ ] `app/services/tbank_acq.py`, `tbank_methods.py`, `payment_gateway.py`, `pay_ticket.py`, `bot_pay.py`
- [ ] `app/static/pay/sberpay.svg`, `sbp.svg`, `tpay.svg`
- [ ] `scripts/backfill_tbank_method.py`
- [ ] `app/certs/russian_trusted_ca.pem` — у вас уже тот же файл (sha256 совпадает), копировать не нужно; Sub CA действует до 06.03.2027.
- [ ] `app/api/routes/miniapp_pay.py` — **с правками из `miniapp_pay.py.changes.md`** (вырезать несколько зависимостей нашего форка; без них файл не импортируется).
- [ ] Зависимости Python: `httpx`, `certifi`, `aioyookassa` в `pyproject.toml`.

**B4. Убери ваш старый Т-Банк**
- [ ] `app/services/yookassa.py`: удали `from app.services import tbank`, ветку `if tbank.use_for_user(user_id): …` в `create_sbp_payment`, ветку `if tbank.is_tbank_id(...)` в `get_payment_status`, вызов `tbank.close_client()` (в `lifespan`/`yookassa.close…`).
- [ ] Удали `app/services/tbank.py` и его тесты. Выбор провайдера теперь только в `payment_gateway`.
- [ ] `grep -rn "tbank_service\|from app.services import tbank\b\|services.tbank\b" app tests` — пусто.

**B5. Слей в ваши файлы** (каждый — по своему `.changes.md`: идёшь по таблице сверху вниз)
- [ ] `app/db/yookassa_repo.py` ← `yookassa_repo.py.changes.md` (`provider`/`provider_order_id` в двух `INSERT`, `test_owner`-ветка в `settle_succeeded_payment`, `save_payment_method`, `reopen_canceled`, `record_refund`, `list_by_operation_prefix`).
- [ ] `app/api/routes/payments.py` ← `payments.py.changes.md` (`yookassa_create`, `yookassa_webhook`, **`tbank_notify`** вместо вашего `tbank_webhook` на том же пути `/tbank/webhook`, `_save_tbank_method`, `_finalize_credited_payment`, `yookassa_status`).
- [ ] `app/api/routes/max.py` ← `max.py.changes.md` (создание платежа в боте MAX и статус).
- [ ] `app/services/support_refund.py` ← `support_refund.py.changes.md` (`tbank_cancel`, ветки в `plan_refunds`/`execute_refund`).
- [ ] `app/workers/pending_payments.py` ← `pending_payments.py.changes.md` (статус через `payment_gateway`).
- [ ] `app/main.py`: `from app.api.routes import miniapp_pay` и `app.include_router(miniapp_pay.router, prefix="/api/v1/miniapp", tags=["miniapp"])` (рядом с `miniapp.router`).
- [ ] Везде, где остался `yookassa_service.get_payment_status(` / `yookassa.get_payment_status(` для строки из БД: `grep -rn "get_payment_status(" app` — строка из `yookassa_payments` должна идти через `payment_gateway.get_payment_status(row)` и `payment_gateway.matches_local_payment(remote, row)`.
- [ ] Заглушки (раздел «Заглушки» ниже): `grep -rnE "YOUR_BRAND|your-domain\.example|your_bot|your_max_bot" app tests scripts` — замени на ваши значения.

**B6. Публичный путь нотификаций** (nginx/Caddy вашего API-хоста)
- [ ] Если наружу пускается список путей — добавь путь нотификаций (тот же `/api/v1/payments/tbank/webhook`, раз вы оставили ваш). Проверка снаружи: `curl -s -o /dev/null -w '%{http_code}' -X POST https://<your-domain>/api/v1/payments/tbank/webhook -d '{}'` → **403** (плохая подпись; при пустых ключах 503). Не 404/502.
- [ ] Публичные GET-страницы Mini App: `/api/v1/miniapp/pay-return`, `pay-open`, `pay-go`, `pay-choose`, `pay-bot` должны отдаваться на хосте Mini App.
- [ ] `TBANK_NOTIFICATION_URL` и `TBANK_RETURN_URL` в env — на ваши домены; в кабинете Т-Бизнеса URL уведомлений = `TBANK_NOTIFICATION_URL` (если путь остался прежним — менять не надо).

**B7. Тесты**
- [ ] Скопируй `tests/test_tbank_acquiring.py`, `test_tbank_buttons.py`, `test_tbank_bot_buttons.py`, `test_tbank_test_mode.py`, `test_pay_ticket.py`, `test_miniapp_pay.py`. Они используют фикстуры источника (`clean_db`, `_tg_user`, `_seed_tiers`, `auth_headers`, `client`) — перенеси их из своего `conftest.py`/`tests/` или адаптируй; HTTP-моки сеть не трогают.
- [ ] Зелёными должны быть: подпись по **обоим примерам из документации**; Init (копейки, `OrderId ≤ 50`, `PayType=O`); нотификация (повтор, `AUTHORIZED+CONFIRMED`, подделка, чужая сумма/терминал, `REJECTED→CONFIRMED`); выбор провайдера (по умолчанию ЮKassa, без ключей — ЮKassa, список, все, процент монотонен); сбой Т-Банка = ошибка, ЮKassa не вызывается; зачисление совпадает с ЮKassa для того же пакета; возврат и повтор после потерянного ответа.

**B8. Способ оплаты**: после выкатки `python scripts/backfill_tbank_method.py` (dry-run) → посмотри незнакомые `tbank_*` → `--apply`.

**B9. Проверка «по умолчанию ничего не изменилось»**: без ключей `choose_provider(x) == "yookassa"` для любого пользователя; бот и Mini App покупают через ЮKassa как раньше. Добавь свой тест на это.

## C. Переменные окружения (имена; значения не коммитим)

См. `env.example`. Обязательные: `TBANK_TERMINAL_KEY`, `TBANK_PASSWORD`. Остальное — флаги раскатки.

| Переменная | По умолчанию | Смысл |
|---|---|---|
| `PAYMENT_PROVIDER` | `yookassa` | `tbank` — все пользователи |
| `TBANK_ROLLOUT_PERCENT` | `0` | 0–100, стабильный хэш `users.id`; повышение только добавляет пользователей |
| `TBANK_FORCE_USER_IDS` | пусто | `users.id` через запятую — всегда Т-Банк (смоук владельца) |
| `TBANK_SEND_RECEIPT` | `false` | чеки 54-ФЗ не передаём (НПД) |
| `TBANK_NOTIFICATION_URL`, `TBANK_RETURN_URL` | — | публичные URL (нотификации; возврат из формы) |
| `TBANK_API_BASE` | `https://securepay.tinkoff.ru/v2` | боевой и тестовый терминал на одном хосте (ключи разные) |
| `TBANK_BUTTONS` / `TBANK_EMBED` / `TBANK_BOT_BUTTONS` | `true` / `true` / `false` | кнопки способов в шторке; форма карты в iframe; кнопки 2×2 в чате бота |
| `TBANK_TEST_MODE`, `TBANK_TEST_USER_IDS` | `false`, пусто | **только стенд:** тестовый терминал, платежи `status='test_owner'`. На проде не ставить |
| `TBANK_CA_BUNDLE`, `TBANK_WALLETS_ASSUME`, `MINIAPP_RETURN_ORIGINS` | пусто | редко нужны |
| `BRAND_NAME`, `MINIAPP_PAYWALL_INAPP` | — / `false` | название продукта в текстах; включить экран оплаты в Mini App |

Если у вас есть скрипт генерации env (аналог `gen-env`), платёжные флаги при повторном запуске должны **сохранять** текущее значение.

## D. Mini App — экран «Пополнить энергию» (если он вам нужен)

Ваш `miniapp-ui` на том же стеке (`@` → `src`, Tailwind 4, shadcn, `lib/miniapp.ts`), поэтому наши файлы ложатся без переписывания логики. Делай **только** если владелец хочет оплату внутри приложения; иначе пропусти раздел и оставь `MINIAPP_PAYWALL_INAPP=false` (backend-часть B всё равно нужна для ботов).

- [ ] Скопируй из `miniapp-ui-next/src/` в `miniapp-ui/src/` (тот же относительный путь): `PaywallScreen.tsx`, `components/PayButtons.tsx`, `components/BottomSheet.tsx`, `components/Energy.tsx`, `lib/paywall.ts`, `lib/pay.ts`, `assets/pay/sberpay.svg`, `sbp.svg`, `tpay.svg`.
- [ ] **Не копируй** `components/ui/button.tsx`, `components/ui/sonner.tsx`, `lib/utils.ts` — у вас они есть (проверено: `showMiniAppToast` экспортируется, `Button` имеет `variant`/`size lg`). `platform/bridge.ts`, `platform/telegram.ts`, `platform/max.ts` целиком тоже не копируй: возьми готовые **прослойки** из этого репозитория `miniapp-ui/src/platform/bridge.ts` и `miniapp-ui/src/platform/telegram.ts` — они реэкспортируют ваши функции из `lib/miniapp.ts` и добавляют недостающую `openExternalLink`. Оригинальный `miniapp-ui-next/src/platform/bridge.ts` оставлен для справки (см. `bridge.ts.changes.md`).
- [ ] Стили: в вашем `index.css` (`@theme`) нет трёх цветовых токенов — `--color-outline`, `--color-brand-text`, `--color-energy-plate`; их значения и остальные токены — в `miniapp-ui-next/src/styles/tokens.css` и `index.css` (бери только эти три, свой дизайн не затирай).
- [ ] Подключи в вашем корне приложения (`App.tsx`): `import PaywallScreen from "./PaywallScreen"`, `import { openPaywall, usePaywall } from "./lib/paywall"`; состояние `const paywall = usePaywall()`; рендер `{paywall ? <PaywallScreen balance={balance} notice={paywall.notice} resume={paywall.resume} source={paywall.source} /> : null}`; кнопки «пополнить» вызывают `openPaywall("header" | "edit" | "toast")`.
- [ ] Включи `MINIAPP_PAYWALL_INAPP=true` на стенде. `tsc -b` чисто. Подробности — `PaywallScreen.tsx.changes.md`, `paywall.ts.changes.md`.

## E. Кнопки 2×2 в чате бота (необязательно, у нас выключено флагом `TBANK_BOT_BUTTONS`)

Стандартная форма Т-Банка сама показывает SberPay/T-Pay и работает не хуже; свои кнопки дают прямой диплинк без лишнего экрана, но выигрыш **не доказан**. Делай, только если владелец попросит.

- [ ] Backend уже есть из раздела B (`bot_pay.py`, `pay-bot`, `pay-choose`, `bot_pay.fields_for_bot` в ответах create).
- [ ] TG: `telegram-bot/core/flows/payment.py.changes.md` и `telegram-bot/app/handlers/payment.py.changes.md`; тест `telegram-bot/tests/test_pay_buttons.py`.
- [ ] MAX: то же в `max-bot-core/…` (вашего MAX-бота в нашем снимке нет — сверяй с реальным репозиторием).
- [ ] `TBANK_BOT_BUTTONS=true` — только после смоука на телефоне.

## F. Раскатка (только по слову владельца)

1. Выкатить код с **дефолтными** флагами; миграции → реплики/воркер; убедиться, что ЮKassa работает как раньше.
2. Стенд с ключами **тестового** терминала: `TBANK_TEST_MODE=1`, `TBANK_TEST_USER_IDS=<users.id тестера>`. Платёж → `status='test_owner'`, энергия начислена, **ничего** не посчитано как деньги/покупка. На тестовом терминале видна только карта.
3. Боевые ключи, `TBANK_FORCE_USER_IDS=<users.id владельца>`: оплата с телефона из каждого бота и из Mini App по каждой кнопке (SberPay, СБП, T-Pay, карта) и отказ. Смотри: строка `provider='tbank'`, `succeeded`, баланс, пуш, `payment_method_type`, возврат кнопкой.
4. `TBANK_ROLLOUT_PERCENT=10` → сутки → 25 → 50 → 100 (или `PAYMENT_PROVIDER=tbank`). Каждое повышение — по слову владельца, не в вечерний пик. После шага сверяй число и сумму оплат в БД и кабинете Т-Бизнеса и **ошибки создания платежа** (запасного провайдера нет).
5. Метрики до/после: конверсия «оплатил / дошёл до оплаты», доля способов (`payment_method_type`), комиссия по способам (`GOTCHAS.md`).

**Откат** (≈ минута, без простоя): `PAYMENT_PROVIDER=yookassa`, `TBANK_ROLLOUT_PERCENT=0`, `TBANK_FORCE_USER_IDS=` → перезапуск реплик и воркера. Созданные платежи Т-Банка дозавершатся (нотификации и воркер смотрят на `provider` строки). Кодом — откат образа; миграции откатывать не нужно.

## Заглушки

Мы заменили свои названия на заглушки, чтобы код и тесты оставались рабочими. Найди и замени **все**:

```bash
grep -rnE "YOUR_BRAND|your-domain\.example|your_bot|your_max_bot|app-pay-return|app-pay-pending|app:balance" app tests scripts miniapp-ui-next telegram-bot max-bot-core
```

- `your-domain.example` → ваш домен (`app.…` — Mini App, `api.…` — API, `edge.…` — CDN/шлюз, если есть); в документах это `<your-domain>`.
- `your_bot`, `your_max_bot` → username ваших ботов (в продовом коде они берутся из `settings.BOT_USERNAME` / `MAX_BOT_USERNAME`).
- `YOUR_BRAND` → название продукта в текстах страниц возврата/ошибок (или `settings.BRAND_NAME`). Если оставить — пользователи увидят «YOUR_BRAND».
- `app-pay-return`, `app-pay-pending`, `app:balance` — служебные имена (`postMessage`, `localStorage`, событие баланса): менять не обязательно, но они **должны совпадать** между `miniapp_pay.py` и `lib/paywall.ts`.
- Числа 7001–7003 в тестах — выдуманные `users.id`, оставь.

## Что решать самому (минимум) и что сказать владельцу

* Если `grep` из начала не совпал со снимком — опиши расхождение.
* Фикстуры тестов (B7), адаптация `miniapp_pay.py` (вырезки по `.changes.md`), стили трёх токенов (D).
* Проверить на устройстве то, что мы не проверяли: диплинки SberPay/СБП/T-Pay из Telegram и MAX на телефоне, ввод карты в iframe (клавиатура, автозаполнение), 3-D Secure в iframe, `openLink` на живом клиенте.
* Тариф и условия акции подтвердить в договоре (`README.md`); комиссию в аналитике считать по способам.

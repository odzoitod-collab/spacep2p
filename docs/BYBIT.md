# Bybit: реквизиты из чата ордера

Модуль `bot/services/bybit.py` — документированный Bybit P2P API и разбор реквизитов продавца из чата ордера.
**К боту пока не подключён**: ни один экран, задача или обработчик его не вызывает, на работу бота он не влияет.
Тесты — `tests/test_bybit_parser.py`.

Полная автоматика на его основе (привязка ордера, выдача реквизитов покупателю, PDF-чек в чат Bybit, отметка
оплаты, завершение сделки, админ-панель «Bybit», таблицы `bybit_*`) лежит в ветке **`bybit-requisites`**. Она
написана на старой базе (времён xRocket, до операторов и BEP-20) и служит образцом для переноса, а не для слияния
как есть.

## Что умеет модуль

| Функция | Что делает |
|---|---|
| `BybitClient.order(order_id)` | детали ордера (`/v5/p2p/order/info`) |
| `BybitClient.session(user_mask_id)`, `.messages(session_id, since_ms)` | чат с контрагентом; история только целиком, иначе ошибка `chat_history_incomplete` |
| `BybitClient.upload_pdf`, `.send_pdf`, `.mark_paid` | загрузка чека, отправка в чат, отметка оплаты; денежные действия не повторяются сами — при неясном ответе `BybitError(uncertain=True)` |
| `extract_requisites(detail, messages)` | реквизиты продавца: тип (`card` / `sbp`), номер, банк, получатель, `payment_id` / `payment_type` для отметки оплаты |
| `validate_order(detail, deal, ad_id, uid, order_id)` | ордер совпадает со сделкой: аккаунт, объявление, покупка USDT за RUB, сумма, объём, без комиссии и купонов |
| `gross_quantity(deal)` | объём USDT для покупки: `amount_rub / rate`, до комиссии площадки |
| `advertisement_id(link)` | номер объявления из ссылки `bybit.com/…/trade/otc/item|advertisement`; чужие домены и неоднозначные ссылки отклоняются |

### Правила парсера

- Берётся только текст **продавца** (`targetNickName`) после открытия **этого** ордера; свои сообщения и старые
  сообщения той же переписки не учитываются.
- Карта — 16–19 цифр с верной контрольной суммой (Луна); СБП — российский номер (`+7…`).
- Банк — из списка `BANK_ALIASES` (Сбер, Т-Банк/Тинькофф, Альфа, ВТБ, Райффайзен, Озон, Газпромбанк, Совкомбанк).
- Номер без банка или банк без номера — `None` (ждать дальше). Два разных номера или банка — `ambiguous_requisites`.
- Способ оплаты ордера (`paymentTermList`) должен быть один или совпадать по номеру; номер/банк в чате, отличные
  от зарегистрированного способа, — `chat_payment_mismatch`. Получатель — `realName` способа или `sellerRealName`.
- Любая ошибка — `BybitError` с коротким кодом; сырые ответы, ключи и текст чата в сообщения ошибок не попадают.

## Ключи и проверка

`.env`: `BYBIT_ACCOUNTS` — JSON-массив `{"name", "uid", "api_key", "api_secret"}` (HMAC-ключи; RSA не
поддерживаются), `BYBIT_TESTNET=true` — тестовая сеть. Ключи только в `.env`: не в БД и не в Telegram.

Проверка ключа без денежных действий (только чтение списка активных ордеров):
```bash
.venv/bin/python -m scripts.check_bybit account-1
```

## Интеграция — что осталось

1. **Где подключать.** Сейчас мерчант присылает ссылку на ордер Bybit (`deal.bybit_url`, вида
   `…/fiat/trade/otc/orderList/<id>`), оператор принимает ордер и выдаёт реквизиты покупателю вручную
   (`orq:give`, `bot/handlers/orders.py`; в мини-приложении — `giveSheet`). Естественное место — подставлять
   туда распознанные реквизиты: `order()` → `session(targetUserMaskId)` → `messages()` → `extract_requisites()`.
2. **Номер ордера из ссылки.** `advertisement_id` разбирает ссылки на объявления; для ссылки ордера
   (`orderList/<id>`) разбор нужно добавить по тем же правилам (только домены Bybit, https, один номер).
3. **Чей ордер.** API видит только ордера аккаунта, чьи ключи заданы: нужны ключи аккаунта, на котором ордер
   открыт (UID в `BYBIT_ACCOUNTS` = `userId` ордера).
4. **Неоднозначность — человеку.** `None` — реквизитов ещё нет, спросить позже; `BybitError` — показать оператору
   и оставить ручную выдачу. Автоматически не угадывать.
5. **Опционально — дальше по ветке:** чек в чат Bybit и отметка оплаты (`upload_pdf` / `send_pdf` / `mark_paid`)
   с маркерами попыток до вызова и сверкой после неясного ответа — как в `bybit_orders.py` ветки.

Не реализовано и не подтверждено документацией: автоматический вход по чужому объявлению и отмена ордера
покупателем; OCR скриншотов с реквизитами; запрос уточнений у продавца.

## Документированные методы

| Операция | Источник |
|---|---|
| Детали ордера | [order/info](https://bybit-exchange.github.io/docs/p2p/order/order-detail) |
| Активные ордера | [pending/simplifyList](https://bybit-exchange.github.io/docs/p2p/order/pending-order) |
| Сессия чата | [getSessionId](https://bybit-exchange.github.io/docs/p2p/chat/get-session-id) |
| Сообщения | [listpage_v1](https://bybit-exchange.github.io/docs/p2p/chat/get-message-list) |
| Загрузка файла | [upload_file](https://bybit-exchange.github.io/docs/p2p/chat/upload-chat-file) |
| Отправка сообщения | [send_v1](https://bybit-exchange.github.io/docs/p2p/chat/send-chat-msg) |
| Отметка оплаты | [order/pay](https://bybit-exchange.github.io/docs/p2p/order/mark-order-as-paid) |

Сверка с документацией — 11 октября 2026 года. На реальном аккаунте ответы API и методы чата ещё не проверены.

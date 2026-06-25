# Web UI Redesign — спецификация (Mistral.ai-стиль)

**Дата:** 2026-06-25
**Статус:** Draft (ожидает review)
**Референс:** интерфейс Mistral.ai (вкладки Chat / Work / Code + Agents + Context + Projects + Connectors, mode-selector Fast/Think/Research в input)

---

## 1. Summary

Переработать веб-интерфейс CorpClaw Lite от текущей 3-панельной схемы (Files слева · Chat в центре · Inspector справа с mode-toggle «Диалог/Выполнение» в топбаре) к навигационной модели в стиле Mistral.ai:

- **Левый sidebar-навигация** с переключением разделов (Chat / Work + Extensions + Agent Context + профиль), списком чатов, кнопкой нового чата.
- **File manager** уезжает из левой колонки в **bottom drawer** (раскрывается снизу, а не слева).
- **Preview** остаётся справа, с явной кнопкой скрытия.
- **Mode selector** (Fast / Think / Research) переносится из топбара в область input. Это новая **ось глубины обработки**; прежняя ось tools on/off (бывшие «Диалог/Выполнение») привязывается к навигации Chat/Work (Work=tools on, Chat=tools off).
- **Навигация Chat / Work** разделяет **историю чатов** на две независимые ленты (своя история на раздел).
- **Раздел Extensions** (Skills / MCP / Plugins) и **Agent Context** (instructions) — новые разделы управления, поверх hot-reload.

Это **программа из 6 подсистем**, реализуемых последовательно. Каждый этап — отдельный spec → plan → implementation цикл. Данный документ — **общая спецификация программы** + детальный дизайн первого этапа (Layout & Navigation).

---

## 2. Контекст: что есть сейчас (ground truth)

### 2.1 Frontend (`frontend/web/src/`, React 18 + TS + Vite)

| Файл | Строк | Ответственность |
|------|------:|-----------------|
| `App.tsx` | 410 | Root shell: login-gate, 3-pane workspace, topbar, mode-toggle, context-meter, new-session, user-menu |
| `chat/ChatPanel.tsx` | 166 | Список сообщений, бабблы, approval-карточки, status-line, composer (textarea + send) |
| `chat/useWebChatSession.ts` | 538 | WebSocket-хук: lifecycle, send/loadOlder/reset/approve, event-reducer |
| `chat/MarkdownMessage.tsx` | 99 | Рендер assistant-маркдауна |
| `files/FileExplorer.tsx` | 1027 | **Левый** файловый менеджер: tree, list/grid/details, search, drag-drop, context-menu, upload-queue, action-modals |
| `files/FilePreview.tsx` | 133 | Правый preview: image/text/metadata, copy/download/expand |
| `files/fileUtils.tsx` | 36 | Иконки файлов, форматирование размера |
| `inspector/InspectorPanel.tsx` | 390 | **Правая** панель «Операционный центр», 3 таба: Обзор / Выполнение / Файл(preview) |
| `hooks/useResizablePanels.ts` | 149 | Resizable-панели (CSS-vars в localStorage) |
| `components/Modal.tsx` | 40 | Базовый модал |
| `api.ts` | 211 | REST/CSRF-слой + XHR-upload с прогрессом |
| `contracts.ts` | 572 | Runtime-валидаторы API-payloads + WS-событий |
| `types.ts` | 143 | TS domain-типы |
| `i18n/ru.ts` | 75 | Русская локализация (весь UI — RU) |
| `styles.css` | 2580 | Единый глобальный стайлшит, CSS grid + CSS custom properties |

**Текущий layout** (`App.tsx:218-362`): CSS-grid 5 колонок `[Files] [handle] [Main] [handle] [Inspector]`. Ширины панелей — CSS-переменные (`--files-width`, `--preview-width`), персистятся в `localStorage` key `corpclaw.web.panelLayout`.

**Текущий mode-toggle** (`App.tsx:393-410`, `SegmentedMode`): 2 кнопки «Выполнение»/«Диалог» в топбаре. При смене шлёт WS `{type:"mode_change", mode}`. **UI идентичен в обоих режимах** — меняется только backend-флаг `tools_enabled`.

**Новая сессия** (`App.tsx:187-192`): НЕ создаёт новый тред — шлёт `{type:"reset_context"}`, очищая текущий контекст.

**Истории чатов нет.** Один активный чат на пользователя. «Показать более ранние» (`ChatPanel.tsx:40-46`) — пагинация внутри единственного чата.

### 2.2 Backend (`src/corpclaw_lite/channels/web/`, aiohttp + SQLite)

| Файл | Строк | Ответственность |
|------|------:|-----------------|
| `runner.py` | 25 | Entry-point: `run_web_channel()` → `WebChannelOrchestrator` |
| `orchestrator.py` | 1551 | aiohttp-сервер: все HTTP/WS handlers, auth-middleware, session/ws-ticket mgmt, WS-цикл чата |
| `chat_store.py` | 601 | UI-транскрипт в SQLite (`web_chat_sessions` + `web_chat_messages`), **отдельно** от агент-памяти |
| `files.py` | 469 | Workspace-filesystem операции (list/preview/upload/mkdir/rename/move/copy/delete/search/tree) |

**Ключевые факты для дизайна:**

1. **`mode` влияет только на `tools_enabled`.** `service.py:221`: `tools_enabled=(mode == "execute")`. Дальше — в `loop.py:688-697`: при `tools_enabled=True` строится tool-schema и ReAct-цикл может звать инструменты; при `False` — `tools_schema=None`, LLM просто генерит разговорный ответ. **Это единственное различие.** Никаких отдельных промптов/моделей/путей кода.

2. **Память агента — single-thread per user.** `SQLiteMemory` (`memory/sqlite.py:46-61`) keyed by `user_id` only, **нет `conversation_id`/`session_id`/`thread_id`**. Один rolling-history на пользователя. `MemoryConsolidator` суммаризирует старую половину при `count >= threshold`.

3. **`WebChatStore` — это UI-лог, НЕ агент-память.** Таблицы `web_chat_sessions` + `web_chat_messages`. **Unique active session per user** (`idx_web_chat_sessions_active`). Архив (`chat_archived_session_ttl_days: 30`, `chat_max_archived_sessions_per_user: 20`) существует, но **не exposed в UI**.

4. **Агент-уровня «режима» (Fast/Think/Research) НЕТ.** Deep research = субагент `research-agent`, вызывается **моделью** через `dispatch_subagent`. Deep/normal детектится **keyword-matching'ом** в `subagent.py:64` (`_research_mode_for_task`), НЕ caller-ом. `ResearchRuntime.resolve_mode` (`research.py:336-348`) умеет принимать **явное** значение, но никто его не передаёт.

5. **Registries имеют list-API, но веб-эндпоинтов управления нет:**
   - `SkillRegistry.list_all()` (`skills/registry.py:63`), `get_allowed_skills(user)` (`:71`)
   - `SubagentRegistry.list_all()` (`subagents/registry.py:78`), `get_allowed_subagents(user)` (`:85`)
   - `MCPManager.get_server_names()` (`mcp/manager.py:109`), `load_config_raw()` (`:113`) — но `_server_tools` приватный
   - `ToolRegistry.list_all()`/`items()`/`get()`

6. **Prompt assembly — в channel-слое**, не в `AgentLoop`. `service.py:178-205` собирает `base_prompt + dept_prompt + user_prompt + skill_block` → передаёт как `system_prompt=` в `run()`. `BootstrapLoader` грузит `config/bootstrap/*.md`.

7. **Auth:** cookie `corpclaw_lite_session` (HttpOnly, SameSite=Strict, 12h TTL) + CSRF-токен (`X-CSRF-Token`) + one-time WS-ticket (`POST /api/ws-ticket`, 30s TTL). Brute-force lockout (5 fail / 5 per min → 300s). **Per-user single in-flight request** (`try_start_user_request`).

8. **Frontend build:** Vite, `dist/` сервируется aiohttp как static. Dev: Vite `:5173` проксирует `/api`+`/ws` → backend `:8090`. Никаких SSR-шаблонов для основного UI (только inline login-fallback и «not built»-стаб).

### 2.3 Эндпоинты (полный список, `_build_app` orchestrator.py:219-249)

REST: `/api/session`, `/api/login`, `/api/logout`, `/api/ws-ticket`, `/api/workspace/overview`, `/api/files{,/tree,/search,/preview,/upload,/mkdir,/rename,/move,/copy,/delete}`, `DELETE /api/files`, `/api/files/{download,inline}`, `/api/download/{token}`.
WS: `/ws/chat` (inbound: `mode_change, load_history_before, reset_context, approve, deny, message`; `/new` → reset).

---

## 3. Зафиксированные архитектурные решения

| Решение | Выбор | Обоснование |
|---------|-------|-------------|
| **Модель памяти** | **UI-мультисессия, single-thread агент-памяти** | 1 пользователь → 1 активный чат. Остальные чаты в истории — **read-only просмотр**. История = способ хранить частые/повторяющиеся задачи и восстанавливать контекст. Переключение между чатами/режимами возможно, но активный только один. **Точка расширения:** заложить на будущее возможность 2–3 одновременных чатов. Без правок ядра SQLiteMemory → нулевой риск. |
| **Модель режимов (две ортогональные оси)** | **Chat/Work + Fast/Think/Research** | Две независимые оси, не одна. **Ось 1 — Chat/Work** (навигация/история): раздельные ленты чатов, **tools on/off привязан сюда** — Work = инструменты включены (наследник «Выполнение»), Chat = выключены (наследник «Диалог»). **Ось 2 — Fast/Think/Research** (глубина обработки в input): Fast = быстрый ответ без thinking; Think = с reasoning/thinking; Research = глубокое исследование через research-субагента. **Доступность режимов по разделам:** Chat → только Fast/Think (Research невозможен, т.к. требует tools); Work → Fast/Think/Research. Это устраняет двойственность осей и убирает ручной tools-toggle. |

**Поведение при переключении чатов (detail):**
- В истории — много чатов. Открываем любой → его транскрипт грузится в основную область (read-only просмотр прошлых сообщений).
- **Нет отдельной кнопки «сделать активным».** Активным становится тот чат, **куда пользователь отправил сообщение** — он автоматически поднимается вверх истории. Отправка сообщения в любой другой чат, пока агент занят в активном, **блокируется** (composer disabled + подсказка «дождитесь завершения активного чата»).
- Таким образом: активный чат всегда ровно один, определяется последним отправленным сообщением; остальные — read-only просмотр, пока активный не освободится.
- **Блокировка ввода в не-активные чаты (while agent busy):** жёсткая — composer полностью `disabled` (серый), с тултипом «дождитесь завершения активного чата». Набор текста/черновики в не-активных чатах запрещён — простота и очевидность приоритетнее.
- «Новый чат» → создаёт свежий чат и открывает его composer (но не становится активным, пока не отправлено первое сообщение). Если в момент отправки другой чат ещё активен — блокируется по тому же правилу.

---

## 4. Целевая структура (high-level)

```
┌──────────────────────────────────────────────────────────────────────┐
│ [⌂ Chat]  [⚒ Work]                          [👁 preview]   [ профиль ]│  ← topbar (минимум)
├────────────┬─────────────────────────────────────────────────────────┤
│            │                                                          │
│  SIDEBAR   │              MAIN AREA (полная ширина)                   │
│ (навигация)│                                                          │
│            │  ┌─ chat ─────────────────────────────────────────────┐ │
│ • Chat/Work│  │ [user]   Нормализуй Excel...                       │ │
│   selector │  │ ┌─ activity ────────────────────── ▾ ────────────┐ │ │
│ • ─────    │  │ │ ⏺ Читаю report.xlsx · 4 шага · 12s              │ │ │
│ • Extensions│ │ └──────────────────────────────────────────────────┘ │ │
│ • Context  │  │ [assistant] 📎 report_normalized.xlsx → preview    │ │
│ • ─────    │  │ [assistant] Готово, вот результат...               │ │
│ + New chat │  └──────────────────────────────────────────────────────┘ │
│ • чат 1    │  ┌──────────────────────────────────────────────────────┐ │
│ • чат 2    │  │ composer...                                    📤    │ │
│ • 📁 папка │  │ 📊 12.4k/32k (39%) [сжать]        Fast ▾          │ │
│   (future) │  └──────────────────────────────────────────────────────┘ │
│ • ─────    ├──────────────────────────────────────────────────────────┤
│ 👤 профиль │          FILE MANAGER (bottom drawer, свёрнут)            │
│  (bottom-L)│                                                          │
└────────────┴──────────────────────────────────────────────────────────┘

         Preview — OVERLAY (не в grid): slide-in справа ▒▒▒▒▒▒▒▒▒▒▒
         либо fullscreen-модал ▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓▓ (поверх чата)
```

**Sidebar-навигация (Mistral-стиль):**
- Верх: **раздел Chat** / **раздел Work** (переключатель; разделяет историю чатов)
- Под переключателем: **разделы-ссылки управления** — **Extensions**, **Agent Context** (сразу под mode-selector, как у Mistral)
- Под ними: кнопка **+ New chat** + **список чатов** текущего раздела
- Список чатов поддерживает (фундамент): **папки** (grouping, на будущее), **rename**, **delete** (контекстное меню / inline-edit)
- Низ (bottom-left): **профиль** (имя, департамент, logout) — как у Mistral/WebUI

**Main area** — чат на **полной ширине** (preview больше не отъедает колонку). Read-only, если не активный.

**Mode selector (Fast/Think/Research)** — внутри composer'а, справа. Доступен только когда чат активен.

**Context-size-индикатор** — под composer'ом, per-chat.

**File manager** — bottom drawer, раскрывается кнопкой снизу. Не занимает боковую колонку.

**Preview — OVERLAY, не колонка:**
- По умолчанию скрыт. Файл в ответе агента / кнопка в topbar → открыть.
- **2 режима открытия:** (a) **slide-in справа** (панель-оверлей поверх правой части чата, resizable ширина), (b) **fullscreen-модал** (поверх всего). Переключение между режимами — кнопка в самом preview.
- Закрытие — крестик / Esc / клик вне (для fullscreen).

---

## 5. Декомпозиция на подпроекты

Каждый — отдельный spec → plan → impl. Порядок = зависимости.

### Этап 1 — Layout & Navigation (детальный дизайн в §6, ниже)
**Scope:** перекрой layout под sidebar-навигацию, bottom-drawer file-manager, preview справа с hide. **Чистый frontend**, минимальный backend. Фундамент для остальных.
- FE: реструктуризация `App.tsx` + `styles.css` grid, выделение `Sidebar`, `BottomDrawer`, переиспользование существующих `ChatPanel`/`FileExplorer`/`FilePreview`.
- BE: без изменений (preview/file-mode-change уже работают через существующие API).
- **Зависимости:** нет.

### Этап 2 — История чатов (мультисессия, своя на раздел Chat/Work) + привязка tools к разделу
**Scope:** список чатов в sidebar, переключение (открытие = read-only просмотр; активация = отправкой сообщения, авто-подъём вверх), rename/delete, «new chat». Своя история на раздел Chat/Work. Auto-naming по первому сообщению (~25 символов). Фундамент папок (`folder_id`). **Привязка tools on/off к разделу:** Work → `tools_enabled=True`, Chat → `tools_enabled=False` (наследники «Выполнение»/«Диалог» соответственно) — это убирает ручной tools-toggle.
- FE: `ChatList` компонент, состояние активного/просматриваемого чата, gating отправки при занятом активном. SectionSwitcher (Chat/Work) теперь реально переключает раздел + определяет `tools_enabled`.
- BE: `chat_store.py` — expose архивных сессий + поле `section` (chat/work) + rename/delete + «switch active»; новые REST-эндпоинты (`GET /api/chats`, `POST /api/chats`, `PATCH /api/chats/{id}`, `DELETE /api/chats/{id}`, `POST /api/chats/{id}/activate`); WS-расширение (загрузка конкретного чата по id). Маппинг section→`tools_enabled` в `service.py`.
- **Зависимости:** Этап 1 (нужен sidebar для списка).

### Этап 3 — Mode selector (Fast / Think / Research) в input
**Scope:** selector глубины обработки в composer. **Внимание:** это НЕ замена оси tools on/off — та привязана к Chat/Work (Этап 2). Fast/Think/Research — ортогональная ось глубины. Research форсит `deep_research` явно (не keyword-детекция).
- FE: dropdown в composer (заменяет `SegmentedMode`); **доступные опции зависят от раздела** — в Chat только Fast/Think, в Work все три.
- BE: thread явного `mode` (глубина) в `AgentLoop.run()` + `service.py`; для Research — передача явного `research_mode` в `DispatchSubagentTool`/`SubagentDispatcher.dispatch` (переиспользовать `ResearchRuntime.resolve_mode`); Fast/Think → mapping на thinking-config через `SamplingProfile`.
- **Зависимости:** Этап 1 (composer в main area) + Этап 2 (раздел Chat/Work определяет tools + доступные modes).

### Этап 4 — Раздел Extensions (Skills / MCP / Plugins)
**Scope:** grid-страница управления расширениями: список, toggle вкл/выкл, trigger reload, статус hot-reload. По образцу Mistral Connectors.
- FE: новая страница `Extensions`, grid-карточки.
- BE: новые REST-эндпоинты (`GET /api/extensions/{skills,mcp,plugins}`, `POST /api/extensions/.../reload`, опц. `POST /api/extensions/.../toggle`). Переиспользовать `SkillRegistry.list_all`, `SubagentRegistry.list_all`, `MCPManager.get_server_names` + публичный `MCPManager.list_server_tools` (добавить). Toggle-state хранить в настройках пользователя (опц.).
- **Зависимости:** Этап 1 (навигация).

### Этап 5 — Раздел Agent Context (instructions)
**Scope:** модал/страница «Agent Context»: tone, personal instructions (additional system prompt), live-preview финального system prompt. Переиспользовать `memory_facts` + onboarding-логику.
- FE: страница `AgentContext`, форма + live preview.
- BE: REST (`GET/PUT /api/agent-context`); persist в `memory_facts` (или новую таблицу user-instructions); инжект в prompt-assembly (`service.py:178-205`).
- **Зависимости:** Этап 1.

### Этап 6 — Изоляция памяти по разговору (опционально, будущий)
**Scope:** добавить `conversation_id` в `SQLiteMemory`, чтобы каждый чат имел независимую агент-память.
- **ТОЛЬКО** если выяснится, что общая rolling-history мешает (например, контекст «протекает» между чатами). Часто можно обойтись reset'ом при переключении.
- **Зависимости:** Этап 2.

---

## 6. Детальный дизайн — Этап 1: Layout & Navigation

### 6.1 Цель этапа
Перекроить визуальный каркас под Mistral-стиль **без изменения бизнес-логики**. После этапа 1: sidebar-навигация слева (Chat/Work + Extensions/Context + chats-list-placeholder + профиль bottom-left), main area на **полной ширине** (чат + activity-card + composer с context-bar + mode), file manager как bottom drawer, **preview как overlay** (slide-in справа или fullscreen). Все существующие функции (чат, превью, файлы) работают на тех же API. Навигационные разделы (Chats list / Extensions / Agent Context) — **плейсхолдеры-заглушки**, наполняемые на этапах 2–5.

### 6.2 Принципы
- **Изоляция изменений:** не трогать `useWebChatSession.ts`, `ChatPanel.tsx`, `FileExplorer.tsx`, `FilePreview.tsx`, `InspectorPanel.tsx` внутренности — только их расположение/wrappers. Минимизировать риск регрессий.
- **Zero BE changes** на этом этапе. Все нужные API уже есть.
- **Сохранить существующие имена CSS-class'ов** где возможно, чтобы не переписывать 2580 строк `styles.css` целиком — вводить новые class'ы для нового layout, оставляя старые для переиспользуемых компонентов.
- **Перф-бюджет:** layout должен работать без jank на resize (как сейчас через `useResizablePanels`).

### 6.3 Архитектура компонентов (target)

```
App.tsx
├── LoginView (без изменений)
└── Workspace (переписан)
    ├── Sidebar (NEW)
    │   ├── SectionSwitcher [Chat | Work]                    ← плейсхолдер, реально работает на этапе 2
    │   ├── ManagementNav [Extensions, Agent Context]        ← плейсхолдеры (этапы 4/5)
    │   ├── ChatListPlaceholder ("+ New chat" + coming-soon) ← этап 2
    │   └── UserProfile (bottom-left: имя, департ, logout)   ← переехал из topbar
    ├── MainArea (полная ширина)
    │   ├── Topbar (минимум: title чата + drawer-toggle + preview-toggle)
    │   ├── ChatPanel (переиспользуется; + activity-card inline)
    │   │   └── ActivityCard (NEW, inline между сообщениями) ← бывший Inspector "Выполнение"
    │   ├── ComposerArea
    │   │   ├── textarea + send
    │   │   ├── ContextSizeBar (per-chat context-size)       ← бывший Inspector "Обзор" (только размер)
    │   │   └── ModeSelector (Fast/Think/Research)            ← замена SegmentedMode (этап 3)
    │   └── BottomDrawer (NEW wrapper)
    │       └── FileExplorer (переиспользуется as-is, в drawer-режиме)
    └── PreviewOverlay (NEW, absolute-positioned поверх main)
        └── FilePreview (переиспользуется as-is; mode: side | expanded)
    (InspectorPanel — расформирован: preview→overlay, Выполнение→activity-card, Обзор→убран)
```

### 6.4 Grid-layout (target CSS)

```css
/* Старый: 5 колонок [Files][handle][Main][handle][Inspector] */
/* Новый: 2 колонки [Sidebar][Main] + drawer внутри Main + preview как overlay */

.workspace {
  display: grid;
  grid-template-columns: var(--sidebar-width) minmax(0, 1fr);
  grid-template-rows: minmax(0, 1fr);
  grid-template-areas: "sidebar main";
}
.sidebar { grid-area: sidebar; }
.main-area { grid-area: main; display: grid; grid-template-rows: minmax(0, 1fr) auto; } /* chat + drawer */

/* Preview — НЕ в grid, это overlay поверх main */
.preview-overlay {
  position: absolute;
  top: 0; right: 0; bottom: 0;
  width: var(--preview-overlay-width);   /* slide-in справа, resizable */
  z-index: 50;
  box-shadow: -8px 0 24px rgba(0,0,0,0.4);
}
.preview-overlay.fullscreen {            /* fullscreen-модал */
  inset: 0;
  width: auto;
  z-index: 60;
}
```

**Resize-handles:**
- Sidebar ↔ Main: вертикальный handle (как сейчас `--files-width`).
- Chat ↔ Drawer: горизонтальный handle (новый `--drawer-height`).
- Preview-overlay (slide-in режим): вертикальный handle на левом краю overlay.
- `useResizablePanels.ts` расширяется: измерения `sidebar`, `drawer`, `preview-overlay` (прежнее `preview` переименовано/адаптировано под overlay-модель).

**Preview-overlay детально:**
- По умолчанию не отрендерен (`preview === null`).
- `previewMode: "side"` → рендерится как `position:absolute` панель справа (`--preview-overlay-width`, default ~40% ширины main, min 320px, max ~70%). Чат под ней не сжимается (overlay перекрывает), но скроллится — это даёт максимум ширины когда preview закрыт.
- `previewMode: "expanded"` → fullscreen (`inset:0`), поверх всего, с собственной шапкой и кнопками.
- Переключение side↔expanded — кнопка в шапке preview (как сейчас `FilePreview` уже умеет).
- Закрытие: крестик / Esc / (для fullscreen) клик по backdrop.

### 6.5 Sidebar — детально (этап 1 — плейсхолдеры)

**Структура сверху вниз:**
```tsx
<Sidebar>
  <SectionSwitcher value={section} onChange={setSection} />   {/* Chat | Work */}
  <nav className="sidebar-management">                        {/* сразу под selector'ом */}
    <button>⚙ Extensions</button>                            {/* этап 4 */}
    <button>🧠 Agent Context</button>                        {/* этап 5 */}
  </nav>
  <div className="sidebar-chats">
    <button className="new-chat-btn">+ Новый чат</button>     {/* сейчас = reset_context */}
    <ChatList>                                               {/* этап 2 */}
      {/* группировка по папкам (future), чаты с rename/delete */}
    </ChatList>
    <div className="coming-soon">История чатов — скоро</div> {/* только этап 1 */}
  </div>
  <UserProfile />                                            {/* bottom-left, как Mistral */}
</Sidebar>
```

**Порядок секций (важно):** Chat/Work selector → **Extensions / Agent Context** (управление) → chats list (+New). Это соответствует Mistral-паттерну, где управление доступно сразу под переключателем режима, а список чатов — ниже.

**UserProfile в bottom-left:** имя + департамент + dropdown (logout, будущие настройки). Переехал из topbar'а.

**Контракт ChatList (для этапа 2, но фундамент заложить в структуре данных уже сейчас):**
- **Auto-naming:** название чата = **первое сообщение пользователя**, обрезанное до ~25 символов (без LLM-вызова, детерминированно). Если сообщение короче — целиком. Без trailing-whitespace. Пример: «Нормализуй Excel-файл с пр…»
- **Rename:** inline-edit (двойной клик / контекстное меню) — пользователь может задать своё имя.
- **Delete:** контекстное меню (иконка ⋮ на ховере) → confirm → удалить.
- **Папки (future, заложить фундамент):** `chat.folder_id` поле в данных; группировка в UI — на этапе после 2. На этапе 1/2 chat-list плоский, но схема БД уже содержит `folder_id NULLABLE`.
- **Активный чат** визуально выделен (background), поднят вверх списка.

**Важно для этапа 1:** `SectionSwitcher`, management-кнопки, ChatList — **визуальные плейсхолдеры**. Chat/Work не разделяет историю до этапа 2. Кнопка «Новый чат» сохраняет текущее поведение (`reset_context`). Auto-naming/rename/delete/papки — этап 2.

### 6.6 BottomDrawer — детально

```tsx
<BottomDrawer open={drawerOpen} onToggle={toggleDrawer}>
  <div className="drawer-header">
    <span>Файлы</span>
    <button onClick={onToggle}>{drawerOpen ? "Свернуть" : "Развернуть"}</button>
  </div>
  {drawerOpen && (
    <div className="drawer-body">
      <FileExplorer ... />  {/* переиспользуется, но в режиме без своей боковой колонки */}
    </div>
  )}
</BottomDrawer>
```

- Свёрнут по умолчанию (`drawerOpen: false`) — даёт максимум места чату.
- Развёрнут — `--drawer-height` (resizable, default ~40vh, min ~150px, max ~75vh).
- `FileExplorer` в drawer-режиме: его внутренний tree/list остаётся, но `mode="side"` больше не используется (только inline внутри drawer). Опция `expanded` (fullscreen) переносится на drawer (drawer на 100% высоты).

### 6.7 Topbar — минимизация

Текущий topbar перегружен (files-toggle, title, context-meter, mode-toggle, new-session, user-menu). В новом layout topbar совсем минимальный — почти всё переехало в sidebar/composer:
- Files-toggle → убирается (drawer управляется из main area снизу).
- Title → название текущего чата (этап 2) или брэнд.
- **Mode-toggle (Диалог/Выполнение) убирается из topbar'а.** Его смысл (tools on/off) привязывается к Chat/Work (Этап 2). Новый selector Fast/Think/Research (Этап 3) — отдельная ось глубины, живёт в composer.
- New-session → переезжает в sidebar («+ Новый чат»).
- **User-menu → убирается из topbar** (переехал в bottom-left sidebar'а, §6.5).
- **Context-meter → убирается** из topbar (закреплён под composer'ом, §6.9).
- **«Обзор» полностью убирается** — debug-фича.
- **Preview-toggle → в правом верхнем углу topbar'а** (место освободилось после ухода user-menu). Открывает preview-overlay. Когда preview открыт — кнопка переключает mode (side↔expanded) или закрывает.

```tsx
<Topbar>
  <div className="topbar-title">{activeChat?.title ?? "CorpClaw Lite"}</div>
  <div className="topbar-actions">
    <button onClick={toggleDrawer} title="Файлы">{drawerOpen ? <ChevronDown/> : <ChevronUp/>}</button>
    <button onClick={togglePreview} title="Preview">
      {preview ? (previewMode === "expanded" ? <Minimize2/> : <Maximize2/>) : <Eye/>}
    </button>
  </div>
</Topbar>
```

### 6.8 InspectorPanel — расформирование

Текущий `InspectorPanel` = 3 таба (Обзор / Выполнение / Файл-preview). В новом layout панели Inspector больше нет — её содержимое расформировывается:

- **Таб «Файл» (preview)** → выносится в **`PreviewOverlay`** — overlay-панель поверх main (slide-in справа или fullscreen, §6.4), НЕ в grid-колонке.
- **Таб «Выполнение» (run-timeline, approvals, live status)** → переезжает **внутрь чата** как **collapsible activity-card**. После каждого запроса пользователя появляется промежуточная карточка между его сообщением и ответом модели: показывает текущее действие агента, по клику раскрывается в полную историю действий (timeline). Пока агент работает — карточка «живая» (обновляется), после ответа — сворачивается в компактную сводку, но остаётся раскрытой по клику.
- **Таб «Обзор» (debug-метрики: usage, модель, recent artifacts/files)** → **полностью убирается** из пользовательского UI. Это debug-фича для разработчиков/инженеров. Единственное, что из него переходит к пользователю — **размер занятого контекста** (см. §6.9).

### 6.8.1 Activity-card в чате (детально)

«Выполнение» становится частью самого разговора, а не отдельной панелью:

```
┌─ chat ──────────────────────────────────────────────┐
│ [user]   Нормализуй Excel...                         │
│ ┌─ activity ──────────────────────────── ▾ ▸ ─────┐ │  ← collapsible card
│ │ ⏺ Читаю файл report.xlsx                         │ │     «live», пока агент работает
│ │ ⏺ Анализирую структуру...                        │ │
│ │ ▸ 4 шага выполнено                               │ │     collapsed по умолчанию после ответа
│ └──────────────────────────────────────────────────┘ │
│ [assistant] Готово, вот результат...                 │
│                                                       │
│ [user]   следующий запрос...                         │
└───────────────────────────────────────────────────────┘
```

- **States:** `running` (живой, auto-expand или последняя строка), `done` (collapsed summary «N шагов · Xs»), `awaiting_approval` (inline approval-кнопки, как сейчас).
- **Клик по header** → toggle expand/collapse всей timeline.
- Переиспользует текущий event-flow из `useWebChatSession.ts` (`runEvents`, `approvals`, `status`) — dataSource тот же, меняется только место рендера (inline в message-list вместо отдельной панели).

### 6.9 Context-size: per-chat, под composer'ом

Вместо context-meter'а в topbar'е — компактный индикатор **под полем ввода**, отражающий размер контекста **текущего открытого чата** (а не глобальный/последний запрос):

```tsx
<ComposerArea>
  <textarea ... />           {/* composer */}
  <button>Отправить</button>
  <ContextSizeBar chat={activeChat} />   {/* под composer'ом */}
</ComposerArea>
<ContextSizeBar>
  📊 Контекст чата: 12.4k / 32k (39%)  [сжать]   {/* + кнопка compact при необходимости */}
</ContextSizeBar>
```

**Per-chat context storage (BE — этап 2, но UI-контракт зафиксировать сейчас):**
- Каждый чат хранит свой **последний известный размер контекста** (tokens used / limit / ratio) — persistится в `web_chat_sessions` (или `web_chat_messages.metadata`).
- При открытии чата из истории → индикатор сразу показывает его контекст, **до** отправки нового сообщения.
- **Цель UX:** пользователь видит, насколько «тяжёлым» будет его новый запрос для данного чата, и понимает — стоит ли сразу нажать «сжать» (compact), или контекста хватит.
- Compact-кнопка рядом с индикатором триггерит контекстную компрессию (переиспользовать существующий `ContextCompressor`).
- При переключении между чатами — индикатор обновляется под выбранный чат.

### 6.10 Migration-стратегия CSS
- Не переписывать `styles.css` (2580 строк) целиком.
- Ввести новый top-level layout через **новые class'ы** (`.workspace-v2`, `.sidebar`, `.bottom-drawer`, `.preview-overlay`), оставив старые `.workspace`, `.files-track` и т.д. для переиспользуемых компонентов (`FileExplorer`/`FilePreview`/`ChatPanel` их не используют на top-level).
- Глобальные переменные темы (цвета, отступы) — переиспользовать.
- Feature-flag на случай отката: env/`localStorage` `corpclaw.web.layoutV2`.

### 6.11 Что НЕ делаем на этапе 1
- Никаких реальных чат-листов / переключения чатов (этап 2).
- Никакой реальной смены режимов Fast/Think/Research (этап 3).
- Никаких Extensions/Agent Context страниц (этапы 4/5).
- Backend не трогаем.

### 6.12 Acceptance criteria (этап 1)
- [ ] Левый sidebar: Chat/Work selector → Extensions/Context management-links → +New chat + chats-placeholder → UserProfile bottom-left.
- [ ] Main area занимает полную ширину (sidebar + main, 2 колонки).
- [ ] File manager — bottom drawer (свёрнут/развёрнут, resizable высота).
- [ ] **Preview — overlay** (slide-in справа, resizable + fullscreen-режим), открывается кнопкой topbar / кликом по файлу; не в grid-колонке.
- [ ] **Activity-card** inline в чате (между user-сообщением и ответом): collapsible, live-статус, collapsed-сводка.
- [ ] **«Обзор» убран** из пользовательского UI.
- [ ] **Context-size-индикатор** под composer'ом (на этапе 1 — текущий `contextUsage`; per-chat хранилище — этап 2).
- [ ] Существующий чат работает без регрессий (отправка, approval, превью файлов, upload).
- [ ] Resize работает (sidebar, drawer, preview-overlay).
- [ ] Responsive-режимы сохранены (узкий экран → коллапс).
- [ ] `uv run ruff/pyright/pytest` — зелёные; `npm run build` — зелёный, `dist/` сервируется.

---

## 7. Открытые вопросы → Visual Companion

Часть вопросов уже закрыта решениями выше:
- ~~Куда деть «Обзор»/«Выполнение»~~ → Выполнение = inline activity-card в чате; Обзор убран (debug-only).
- ~~Context-meter~~ → per-chat индикатор под composer'ом.

Остались для visual companion:
1. **Layout sidebar'a:** какие секции, в каком порядке, какие иконки, сворачиваемый ли.
2. **Bottom drawer:** высота по умолчанию, поведение сворачивания (peek-bar vs. полностью прячется), tabbed (Files + будущие) vs. single-purpose.
3. **Preview pane справа:** single preview (текущий вид) — уточнить детали (кнопки, expand-to-fullscreen).
4. **Activity-card:** точный вид collapsed/expanded, как показывается «live» статус, как отображаются approvals внутри.
5. **Context-size-индикатор:** точный вид (text / bar / оба), поведение кнопки compact.
6. **Composer + mode selector:** как именно Fast/Think/Research располагается в composer (dropdown, segmented, pills).
7. **Стилистика:** текущая тёмная тема — сохранить, или подтянуть ближе к Mistral (если есть конкретные отличия).

---

## 8. Risks & Mitigations

| Риск | Mitigation |
|------|-----------|
| `styles.css` (2580 строк) — трудно трогать, high regression | Новые class'ы поверх, feature-flag, не переписывать существующие component-стили |
| `FileExplorer` (1027 строк) захардкожен под боковую колонку | Проверить на этапе 1 impl, при необходимости — prop `variant: "drawer"\|"side"` |
| Resize-логика `useResizablePanels` — 3 измерения теперь | Расширить существующий хук третьим измерением, не переписывать |
| Перетекание контекста между чатами (т.к. агент-память общая, single-thread) | При отправке в новый чат — reset context предыдущего; чтение истории — read-only. Этап 6 (изоляция `conversation_id`) — если станет проблемой |
| Mode-toggle удаление ломает WS-протокол (`mode_change`) | Сохранить WS-сообщения, маппить Chat/Work + Fast/Think/Research; либо явно версионировать протокол |

---

## 9. Порядок работы после approval

1. **Этот spec** → user review → фиксируем.
2. **Visual companion** → собираем визуальное видение (закрываем §7).
3. **writing-plans skill** → детальный implementation-plan на Этап 1 (layout).
4. Этап 1 impl → verification → merge.
5. Этап 2: новый spec (история чатов) → ... → повтор цикла.

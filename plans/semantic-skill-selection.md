# Semantic Skill Selection

## Summary

Заменить текущий подход «вставляем **все** разрешённые скилы в system prompt» на семантическое подключение **только релевантных** скилов на основе пользовательского сообщения.

## Проблема

Сейчас в runner.py **все** скилы, разрешённые для отдела пользователя, инжектятся в system prompt:

```python
allowed_skills = skill_registry.get_allowed_skills(user)
skill_block = build_skill_block(allowed_skills, plugin_skills)
system_prompt = (system_prompt or "") + skill_block
```

При 5 скилах это ~2-3K токенов. При 20-50 скилах — 10-30K, что **критично для локальных LLM** с окном 8-16K токенов (Qwen 3.5 4B = 32K, но quality degrades > 8K).

## Подходы

### Подход A: TF-IDF + Cosine Similarity (рекомендуется)

| Плюсы | Минусы |
|-------|--------|
| Ноль внешних зависимостей | Не понимает семантику ("таблица" != "Excel") |
| Мгновенно (~1ms) | Требует ключевые слова в description |
| Полностью offline | |

**Как работает:**
1. При загрузке каждого скила — строим TF-IDF вектор из `description` + `id` + ключевых слов из `instructions`
2. При запросе — строим TF-IDF вектор сообщения пользователя
3. Cosine similarity -> top-K скилов выше порога

**Реализация:** ~80 строк чистого Python, используя `collections.Counter` и базовую математику.

### Подход B: Keyword Tags в манифесте

| Плюсы | Минусы |
|-------|--------|
| Простейшая реализация (~30 строк) | Автор скила должен вручную задать keywords |
| Предсказуемое поведение | Не ловит перефразирование |
| Нулевая задержка | |

**Как работает:**
1. Добавляем поле `keywords` в YAML frontmatter скила:
   ```yaml
   keywords: [excel, xlsx, csv, normalize, таблица, данные, дубликаты]
   ```
2. При запросе — проверяем пересечение слов сообщения с keywords
3. Если пересечение >= 1 -> скил подключается.

### Подход C: Embedding Model (lightweight)

| Плюсы | Минусы |
|-------|--------|
| Лучшее качество matching | +200MB RAM (модель ~90MB + ONNX runtime) |
| Понимает семантику | Зависимость sentence-transformers или onnxruntime |
| Работает offline | Первый запуск — download модели |

Для проекта, ориентированного на минимум зависимостей и локальные LLM, подход C избыточен на данном этапе. Рассматривать его стоит только при 100+ скилах.

## Рекомендация: Подход A+B (гибрид)

Комбинируем оба: **keywords для точного matching + TF-IDF как fallback** для нечётких запросов.

1. Если есть keyword hit -> скил подключается 100%
2. Если keyword hit нет -> TF-IDF score > threshold -> подключается
3. Всегда подключаются скилы с пустым `keywords` (backward compat) или с `always: true`

## Архитектура

```
                  SkillMatcher
                  ────────────
                  match(message,
                    allowed_skills)
                    -> list[Skill]

                  _keyword_match()
                  _tfidf_match()
                  _rebuild_index()

              uses                   uses
    SkillRegistry          build_skill_block()
```

### Новые/изменённые файлы

| Файл | Действие | Описание |
|------|----------|----------|
| `src/corpclaw_lite/extensions/skills/matcher.py` | **NEW** | SkillMatcher — ядро семантического подбора |
| `src/corpclaw_lite/extensions/skills/base.py` | EDIT | Добавить поле `keywords: list[str]` |
| `src/corpclaw_lite/extensions/skills/loader.py` | EDIT | Парсить `keywords` из frontmatter |
| `src/corpclaw_lite/channels/telegram/runner.py` | EDIT | Использовать `SkillMatcher.match()` вместо `get_allowed_skills()` |
| `src/corpclaw_lite/agent/prompt.py` | — | Без изменений |
| `skills/*.md` | EDIT | Добавить `keywords` в frontmatter |
| `config/settings.yaml` | EDIT | Новая секция `skills:` |
| `tests/test_skill_matcher.py` | **NEW** | Unit тесты |

## Детальный дизайн

### 1. Skill dataclass — новые поля

```python
@dataclass(frozen=True)
class Skill:
    id: str
    description: str
    allowed_for: list[str]
    instructions: str
    path: Path | None = None
    version: str = "1.0.0"
    keywords: list[str] = field(default_factory=list)  # NEW
    always: bool = False                                 # NEW — всегда инжектить
```

### 2. SkillMatcher — core

```python
class SkillMatcher:
    """Selects relevant skills based on user message."""

    def __init__(
        self,
        top_k: int = 3,
        keyword_boost: float = 0.5,
        tfidf_threshold: float = 0.15,
    ) -> None: ...

    def match(self, message: str, allowed_skills: list[Skill]) -> list[Skill]:
        """Return the top-K most relevant skills for the message.

        Priority:
        1. Skills with always=True -> always included
        2. Skills with keyword match -> included
        3. TF-IDF score > threshold -> included (up to top_k total)
        """
        ...
```

### 3. Frontmatter пример

```yaml
---
id: excel_normalizer
description: Normalize and clean Excel/CSV files — fix headers, types, duplicates
version: "1.0.0"
allowed_for: ["marketing", "finance", "hr", "analytics", "default"]
keywords:
  - excel
  - xlsx
  - csv
  - таблица
  - нормализ       # стемминг-префикс: ловит "нормализуй", "нормализация"
  - дубликат
  - очисти         # ловит "очисти", "очистка"
  - данные
  - столбец
  - колонк
---
```

### 4. Изменения в runner.py

```python
# BEFORE:
allowed_skills = skill_registry.get_allowed_skills(user)
skill_block = build_skill_block(allowed_skills, plugin_skills)

# AFTER:
allowed_skills = skill_registry.get_allowed_skills(user)
all_plugin_skills = [
    p.skill for p in plugin_registry.get_allowed_plugins(user) if p.skill
]
matcher = SkillMatcher()  # или инициализировать один раз при старте
matched = matcher.match(message, allowed_skills + all_plugin_skills)
skill_block = build_skill_block(matched, [])  # уже merged
```

### 5. Настройки в config/settings.yaml

```yaml
skills:
  selection_mode: "semantic"   # "all" | "semantic"
  top_k: 3                     # макс. скилов в промпте
  tfidf_threshold: 0.15        # минимальный TF-IDF score
  keyword_boost: 0.5           # бонус за keyword match
```

С `selection_mode: "all"` — старое поведение (backward compatible).

## Алгоритм TF-IDF (без зависимостей)

```
1. Tokenize: lowercase -> split по пробелам/пунктуации -> убрать стоп-слова
2. Для каждого скила: документ = id + " " + description + " " + первые 200 chars instructions
3. Построить IDF словарь по всем скил-документам
4. При запросе:
   a. Tokenize сообщение
   b. TF-IDF вектор сообщения
   c. Cosine similarity с каждым скил-документом
   d. Sort -> top_k
```

Русские стоп-слова: предлоги, местоимения, частицы (~50 слов, захардкодить).

## Шаги реализации

- [x] 1. Добавить `keywords` и `always` поля в Skill dataclass
- [x] 2. Обновить SkillLoader для парсинга `keywords` и `always`
- [x] 3. Создать SkillMatcher с keyword + TF-IDF логикой
- [x] 4. Добавить SkillsSettings в config/settings.py
- [x] 5. Обновить runner.py для использования SkillMatcher
- [x] 6. Обновить cli.py (если есть skill injection) — не нужно, CLI не использует скилы
- [x] 7. Добавить `keywords` в существующие скилы
- [x] 8. Написать unit тесты (18 passed)
- [x] 9. Интеграционный тест: "нормализуй Excel" -> excel_normalizer попадает, translator — нет

## Оценка

- **Новый код:** ~150 строк (matcher.py) + ~50 строк тестов + мелкие правки
- **Зависимости:** ноль новых
- **RAM:** +0MB (всё в оперативке, словари ~KB)
- **Латентность:** <1ms на matching
- **Совместимость:** полная обратная совместимость через `selection_mode: "all"`

## Notes

- При hot-reload скила нужно пересчитать TF-IDF индекс — дёшево, <1ms
- Стемминг-префиксы в keywords (`нормализ` вместо `нормализуй`) — простое решение для русского морфологии без NLP-библиотек
- При росте до 100+ скилов -> рассмотреть переход на embedding model (Подход C)

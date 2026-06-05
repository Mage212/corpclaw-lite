---
id: meeting_summary
description: "Structure meeting notes into organized summaries with action items and decisions"
version: "1.0.0"
allowed_for:
  - "*"
keywords:
  - meeting
  - встреч
  - собрани
  - минут
  - minutes
  - summary
  - саммари
  - итог
  - action
  - действ
  - задач
  - заметк
  - notes
  - протокол
  - agenda
  - повестк
  - участник
  - participant
  - решен
  - decision
  - вопрос
  - question
scope:
  - document-agent
---

# Meeting Summary Skill

## Context

Use this skill when the user shares meeting notes, transcript text, or asks
to summarize a meeting. The skill structures raw notes into a clear format
with sections for summary, decisions, and action items.

## Instructions

1. Read the provided meeting notes or text carefully. If notes are in a file,
   use `read_file` to access them first.
2. Structure the output into localized sections for:
   - Summary (2-4 sentences: what was discussed, key topics)
   - Key decisions (bulleted list of decisions made)
   - Action items (table: task, assignee, deadline if mentioned)
   - Open questions (unresolved items needing follow-up)
3. For each action item, try to identify: what needs to be done, who is
   responsible, and the deadline or timeframe.
4. Match the input language. Use section titles in the same language as the
   user's notes or request.
5. Keep the summary concise but complete. Don't omit important details.
6. Offer to save the summary to a file using `write_file` if the notes are
   long or the user may want to share them.
7. If the notes are in a PDF file, use `pdf_reader` first to extract text.

## Examples

**Input:** "We met today about Q3 planning. Sarah will prepare the budget by Friday. Decision: increase marketing spend by 15%. John raised concerns about timeline."
**Output:**
### Summary
Discussed Q3 planning including budget allocation and timeline concerns.

### Key Decisions
- Increase marketing spend by 15%

### Action Items
| Task | Assignee | Deadline |
|------|----------|----------|
| Prepare Q3 budget | Sarah | Friday |

### Open Questions
- Timeline concerns raised by John need resolution

**Input:** "Встреча по проекту X. Иван сказал что бэкенд готов к 15 числу. Мария займётся тестированием. Решено: релиз 20 числа. Остался вопрос по нагрузке."
**Output:**
### Краткое описание
Обсуждали статус проекта X — готовность бэкенда, тестирование и дату релиза.

### Ключевые решения
- Релиз проекта X назначен на 20 число

### Задачи
| Задача | Ответственный | Срок |
|--------|---------------|------|
| Завершить бэкенд | Иван | 15 число |
| Провести тестирование | Мария | до 20 числа |

### Открытые вопросы
- Вопрос по нагрузке не решён — нужен дополнительный анализ

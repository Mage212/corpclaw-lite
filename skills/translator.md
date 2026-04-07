---
id: translator
description: Translate text between languages
version: "1.0.0"
allowed_for:
  - "*"
keywords:
  - перевед
  - перевод
  - translat
  - язык
  - language
  - английск
  - english
  - немецк
  - german
  - французск
  - french
  - испанск
  - spanish
  - китайск
  - chinese
  - русск
  - russian
---

# Translator Skill

## Context

Use this skill when the user asks you to translate text from one language to another.
This skill applies to any department needing multilingual communication.

## Instructions

1. Identify the source language (if not specified, detect automatically).
2. Identify the target language from the user's request.
3. Translate the provided text accurately, preserving tone and formatting.
4. If the text contains domain-specific terminology, keep technical terms in context.
5. Return only the translated text unless the user asked for explanation.

## Examples

**Input:** "Переведи на английский: Добро пожаловать в нашу компанию."
**Output:** "Welcome to our company."

**Input:** "Translate to German: Please submit your report by Friday."
**Output:** "Bitte reichen Sie Ihren Bericht bis Freitag ein."

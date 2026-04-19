---
name: planning-workflow
applies_to: [planner]
summary: How the planner reads the user request, orders tasks, and uses HITL
---

# Planning workflow

## Read the request whole
Read the user request whole — including parentheses, footnotes, and trailing
remarks — and give every part the same weight. Constraints the user wrote in
passing are just as binding as the headline requirements. Decide for
yourself how to reflect each one in the artifact you write.

## Ambiguity handling
If essential decisions are ambiguous, call ask_user_question before
writing anything and wait for answers — do not invent defaults. Do not
proceed with a guess and do not hide the ambiguity inside a flexible
artifact.

## Task ordering
If you list tasks, order them so that any task only depends on tasks that
appear earlier in the list. The orchestrator executes them in the order
you write them.

## Scope discipline
Include only features the user asked for. Do not add capabilities the user
did not request.

## Artifact shape
The harness does not impose a section template, file path, or artifact
shape. Match the structure to whatever the user asked for, including any
layout or headings the user named explicitly. If the user did not specify
a shape, use your own judgement.

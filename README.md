# Course Notes Orchestrator Skill

This repository contains the `course-notes-orchestrator` Codex skill.

The skill helps turn course materials into structured Markdown notes. It is designed for workflows that combine ASR transcripts, slide images, and Word documents with embedded images.

## Contents

- `course-notes-orchestrator/SKILL.md`: skill instructions and workflow
- `course-notes-orchestrator/scripts/course_notes_pipeline.py`: helper script for scanning, sampling, planning, chunk materialization, and merging
- `course-notes-orchestrator/agents/openai.yaml`: display metadata for the skill

## Install

Copy or symlink the skill folder into your Codex skills directory:

```bash
cp -R course-notes-orchestrator ~/.codex/skills/
```

Restart Codex after installing so the skill is discovered.

## Usage

In Codex, ask for course note organization with source paths, for example:

```text
Use $course-notes-orchestrator to organize this ASR transcript and slide image folder into Markdown notes.
```

The skill will first scan the provided files, build a style guide, create a chunk plan, ask for confirmation, and then delegate chunk writing and quality checks.

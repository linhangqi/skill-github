---
name: course-notes-orchestrator
description: Use when the user provides course materials and asks to organize them into Markdown notes, especially ASR transcripts plus slide images or Word files with embedded slides. Trigger on requests like “帮我整理课程笔记”, “把这些录音转写和课件整理成 markdown”, or when the user drops transcript/image paths and wants a structured review note. This skill handles scanning, chunk planning, subprocess delegation with `codex exec`, quality checks, final merge, and cleanup.
---

# Course Notes Orchestrator

## Overview

Use this skill when the task is not "summarize one file quickly", but "organize a course package into publishable Markdown notes" with explicit staging:

1. Scan inputs first.
2. Build a global style guide from samples.
3. Split text and images into chunks.
4. Delegate chunk writing to independent `codex exec` subprocesses.
5. Quality-check chunk outputs.
6. Merge and polish a final Markdown note.

The main agent is the scheduler. Do not write the chunk note body directly unless the user explicitly asks to skip subprocess delegation.

Primary helper script:

- `scripts/course_notes_pipeline.py`

Use it for deterministic prep work before calling `codex exec`.

## Input Contract

Ask for the input paths before doing any processing.

Preferred input model:

- One or more ASR transcript paths
- One image folder path

Supported text sources:

- `.docx`
- `.txt`
- `.md`

Supported image sources:

- `.png`
- `.jpg`
- `.jpeg`
- `.webp`

If the user does not have a separate image folder, allow this fallback:

- A `.docx` file can act as both text source and image source if it contains embedded images.

Before processing, confirm:

- which files belong to the same course
- whether a `.docx` with images should replace the separate image-folder requirement

Do not start scanning or summarizing before the paths are clear.

## Workflow

### 1. Scan Inputs Only

First do a file scan, not a content summary.

For transcript files:

- normalize all transcript content into `/tmp/asr_full_text.txt`
- if input is `.docx`, convert to plain text first
- report file type, total characters, total lines, and paragraph count
- do not summarize the lecture yet

For image sources:

- if the user gave an image folder, list image count, filenames, formats, and whether natural filename sorting works
- if images live inside `.docx`, inspect the package and extract embedded media into a temp directory
- do not interpret image content during the scan step

If any path is missing, invalid, unsupported, or unreadable, stop and report the concrete issue.

Preferred command:

```bash
python3 scripts/course_notes_pipeline.py scan \
  --transcript "/path/to/asr1.docx" \
  --transcript "/path/to/asr2.docx" \
  --image-dir "/path/to/slide-images"
```

If images are embedded in a Word file instead of a separate folder:

```bash
python3 scripts/course_notes_pipeline.py scan \
  --transcript "/path/to/asr.docx" \
  --embedded-images-docx "/path/to/slides.docx"
```

### 2. Build a Global Style Guide

Create a small sample set before chunking:

- text sample: start 5%, middle 5%, end 5%
- image sample: at most 10 images in order

Write:

- `/tmp/asr_sample.txt`
- `/tmp/sample_imgs/`

Then run one `codex exec` subprocess to create:

- `/tmp/course_style_guide.md`

The style guide should include:

1. likely course theme
2. likely chapter structure
3. terminology normalization
4. title naming rules
5. note tone rules
6. forbidden patterns

The style-guide subprocess must not write the final note.

If the subprocess fails once, retry once.

Prep command:

```bash
python3 scripts/course_notes_pipeline.py sample
```

## 3. Create a Chunk Plan

Split `/tmp/asr_full_text.txt` by paragraph boundaries.

Default chunk rules:

- target size about 2000 Chinese characters
- dense technical sections can be reduced to 1200-1800
- keep 10%-15% overlap
- record start and end line numbers for each chunk

Image grouping rules:

- prefer timestamp alignment if filenames contain time-like patterns
- otherwise sort naturally by filename
- if no better signal exists, group every 5 images
- map image groups proportionally to text chunks

Output a readable chunk plan and stop for confirmation.

Only continue after the user explicitly replies with something like:

- `确认`
- `开始`

Planning command:

```bash
python3 scripts/course_notes_pipeline.py plan
```

## 4. Generate Chunk Inputs

For each chunk, create:

- `/tmp/chunk_text_{n}.txt`
- `/tmp/chunk_imgs_{n}/`
- `/tmp/chunk_prompt_{n}.md`

Each chunk prompt must:

- tell the subprocess it is only responsible for chunk `{n}/{N}`
- require reading `/tmp/course_style_guide.md`
- require using both transcript and images
- ban fabrication, fluffy summary, and process narration
- ask for Markdown only
- allow `[待续]` only when the topic clearly continues

Materialization command:

```bash
python3 scripts/course_notes_pipeline.py materialize
```

## 5. Delegate Writing to `codex exec`

Run each chunk in an independent subprocess.

Preferred pattern:

```bash
codex exec --ephemeral --skip-git-repo-check -o "/tmp/chunk_output_{n}.md" "$(cat /tmp/chunk_prompt_{n}.md)"
```

Notes:

- use `--ephemeral` to avoid session-file issues
- use `--skip-git-repo-check` when running outside a trusted repo
- attach images with `-i` when the subprocess should inspect them directly
- each subprocess must be isolated from the others

Failure policy:

1. retry once
2. if still failing, record the failed chunk
3. continue the rest of the job
4. do not abort the whole workflow because one chunk failed

## 6. Quality Check Each Chunk

After each chunk finishes, check:

- file exists
- file is not empty
- content length is not trivially short
- has Markdown headings
- does not mainly contain process narration
- does not contain tool-failure language

Then run a separate `codex exec` quality-check subprocess that returns only:

- `PASS`
- `FAIL: ...`

If the chunk fails QC, rerun that chunk once.

Be careful with over-broad regex checks. Do not flag legitimate content such as `模型` just because it contains the character sequence `我`.

## 7. Merge All Chunk Outputs

After chunk generation is done, concatenate chunk outputs in order into:

- `/tmp/notes_raw.md`

Concat command:

```bash
python3 scripts/course_notes_pipeline.py concat --total-chunks N
```

Then run one final-editor `codex exec` subprocess that:

- reads `/tmp/notes_raw.md`
- reads `/tmp/course_style_guide.md`
- merges adjacent duplicate sections
- removes chunk seams
- resolves `[待续]`
- normalizes titles and terminology
- adds a course title
- adds a Markdown table of contents
- applies Chinese chapter numbering
- writes the final result to `~/Desktop/notes.md`

## 8. Final QC and Cleanup

Check the final file for:

- file exists
- not empty
- has a course title
- has a table of contents
- has numbered sections
- no obvious large duplicate blocks
- no `/tmp/chunk` residue
- no `[待续]`
- no tool-failure language

If the final result is visibly wrong, report that clearly instead of pretending success.

On success, clean:

- `/tmp/chunk_text_*.txt`
- `/tmp/chunk_imgs_*`
- `/tmp/chunk_prompt_*.md`
- `/tmp/chunk_output_*.md`
- `/tmp/notes_raw.md`
- `/tmp/asr_sample.txt`
- `/tmp/sample_imgs/`

Usually keep:

- `/tmp/course_style_guide.md`
- `/tmp/asr_full_text.txt`

unless the user asks for full cleanup.

Cleanup command:

```bash
python3 scripts/course_notes_pipeline.py cleanup --keep-core
```

## Operational Notes

- Prefer shell tools for scanning and file prep.
- Prefer `textutil` for `.docx` to text conversion on macOS.
- For `.docx` images, inspect or extract `word/media/*`.
- Prefer `rg` for file and text search.
- If `codex exec` fails due to repo trust or session persistence, switch to `--skip-git-repo-check --ephemeral`.
- If local `codex exec` usage limits interrupt the full workflow, report exactly which stage was blocked.
- The helper script handles scan/sample/plan/materialize/concat/cleanup. Keep `codex exec` focused on style-guide generation, chunk writing, QC, and final editing.

## Output Contract

When the workflow completes, report:

- total chunk count
- success count
- failure count
- final output path
- any failed chunks
- any known quality caveats

Keep the report short and factual.

## Resources

### scripts/

- `scripts/course_notes_pipeline.py`: deterministic helper for transcript normalization, embedded-image extraction, sample generation, chunk planning, chunk material creation, raw-output concatenation, and cleanup.

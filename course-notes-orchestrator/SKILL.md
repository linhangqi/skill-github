---
name: course-notes-orchestrator
description: Use when the user provides course materials and asks to organize them into Markdown notes, especially ASR transcripts plus slide images or Word files with embedded slides. Trigger on requests like “帮我整理课程笔记”, “把这些录音转写和课件整理成 markdown”, or when the user drops transcript/image paths and wants a structured review note. This skill handles scanning, style/glossary/slide-digest prep, semantic chunk planning, subprocess delegation with `codex exec`, source mapping, quality checks, structure editing, independent final writing, final editorial review, revision, and cleanup.
---

# Course Notes Orchestrator

## Overview

Use this skill when the task is not "summarize one file quickly", but "organize a course package into publishable Markdown notes" with explicit staging:

1. Scan inputs first.
2. Build a global style guide from samples.
3. Build a glossary and slide digest before chunk writing.
4. Build a semantic chunk plan first; use character-count chunking only as a fallback or guardrail.
5. Delegate chunk writing to independent `codex exec` subprocesses.
6. Preserve source mapping for each chunk.
7. Quality-check chunk outputs.
8. Run a structure-editor pass to produce a final outline.
9. Run an independent final-writer pass to write the complete note.
10. Run a separate reviewer pass, then a final revision.
11. Deliver a polished Markdown note plus a short quality report.

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

Before processing, only ask the user to confirm when the course paths or grouping are ambiguous:

- which files belong to the same course
- whether a `.docx` with images should replace the separate image-folder requirement

If the target folder and course materials are clear from the user's request or current workspace, proceed without a separate confirmation.

## Workflow

### 1. Scan Inputs

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

### 3. Build Glossary and Slide Digest

Before chunk planning, create two lightweight prep artifacts when the course has noisy ASR, technical terms, English acronyms, screenshots, slides, charts, tables, or many images:

- `/tmp/course_glossary.md`
- `/tmp/slide_digest.md`

Run one `codex exec` subprocess for the glossary. It should read `/tmp/asr_sample.txt` and `/tmp/course_style_guide.md`, and optionally inspect more transcript snippets if needed. It should output:

1. canonical term
2. likely variants or ASR mishearings
3. short definition from course context
4. preferred Chinese/English spelling
5. terms that must not be normalized because they may be distinct

Run a separate `codex exec` subprocess for the slide digest when images exist. It should inspect the sample or full slide image set as needed and output:

1. image filename
2. visible title or likely topic
3. key concepts, formulas, charts, tables, or diagrams
4. likely matching transcript topic
5. OCR uncertainty or unreadable regions

Do not let either subprocess write final notes. Treat these artifacts as guidance, not ground truth. If image inspection is unavailable or too costly, create a best-effort digest from filenames and any extracted text, and explicitly mark it as low confidence.

### 4. Create a Semantic Chunk Plan

Default to semantic chunking, not fixed-size chunking. The goal is to keep complete teaching topics together and attach images to the topic they explain.

Semantic chunking rules:

- Identify topic boundaries from section titles, slide titles, repeated keywords, stage names, process steps, table titles, and obvious shifts in the lecture.
- Prefer chunks such as "竞品分析核心作用", "模型选型的方法流程", "设计评测集", "定义评分维度", "成本分析" over arbitrary 2000-character ranges.
- Keep adjacent paragraphs with the same topic in the same chunk.
- Attach slide images to the chunk whose topic they support; consult `/tmp/slide_digest.md` before falling back to filename order.
- Each chunk should record `topic`, `previous_topic`, `next_topic`, line range, character count, and image list.
- Use character limits as guardrails: merge very small adjacent chunks when they are related, and split very large topics into subtopics.

Default guardrails:

- target size about 2600 Chinese characters
- max size about 4200 Chinese characters
- minimum useful size about 600 Chinese characters unless the chunk is image-heavy
- dense technical sections can be reduced to 1200-1800
- avoid overlap unless a topic genuinely continues; semantic context replaces most overlap
- record start and end line numbers for each chunk

Image assignment rules:

- prefer timestamp alignment if filenames contain time-like patterns
- otherwise sort naturally by filename and use slide titles/topic flow
- if extracted text is sparse but images are many, create image-driven semantic chunks so all images are included
- as a last resort, map images proportionally across semantic chunks

Preferred command:

```bash
python3 scripts/course_notes_pipeline.py semantic-plan
```

For long or noisy courses, use the two-stage heading validation workflow. First let the script find likely heading boundaries with local context:

```bash
python3 scripts/course_notes_pipeline.py candidate-headings
```

This writes `/tmp/candidate_headings.json` with line numbers, original text, before/after context snippets, trigger rules, and initial confidence. Ask a `codex exec` subprocess to read only this candidate list plus `/tmp/course_style_guide.md`, then write `/tmp/validated_headings.json` in this shape:

```json
[
  {
    "line": 120,
    "is_boundary": true,
    "topic": "设计评测集",
    "merge_with_previous": false,
    "reason": "从候选模型池转入评测集设计"
  }
]
```

The validation subprocess should decide only whether each candidate is a true chapter/topic boundary, normalize topic names, and mark adjacent false splits with `merge_with_previous`. It should not read the full transcript or generate the final chunk plan.

Then turn the confirmed boundaries into a checked plan:

```bash
python3 scripts/course_notes_pipeline.py semantic-plan \
  --from-headings-json /tmp/validated_headings.json
```

For difficult courses, first ask one `codex exec` subprocess to draft `/tmp/semantic_plan_draft.json` from `/tmp/asr_full_text.txt`, `/tmp/course_style_guide.md`, and the slide images. Then validate and normalize it:

```bash
python3 scripts/course_notes_pipeline.py semantic-plan \
  --from-json /tmp/semantic_plan_draft.json
```

The draft plan should include topic, start/end lines, image filenames, optional source id, and a brief reason for each chunk.


The helper writes `/tmp/chunk_plan.tsv` and `/tmp/chunk_plan.txt`.

Output a readable semantic chunk plan and continue by default. Do not stop for user confirmation unless the user explicitly asked to review the plan before execution, the plan has obvious ambiguity, or continuing would require a separate permission/data-handling confirmation from the runtime.

Fallback command for very regular transcripts or if semantic planning fails:

```bash
python3 scripts/course_notes_pipeline.py plan
```

### 5. Generate Chunk Inputs

For each chunk, create:

- `/tmp/chunk_text_{n}.txt`
- `/tmp/chunk_imgs_{n}/`
- `/tmp/chunk_prompt_{n}.md`
- `/tmp/chunk_source_{n}.json` when possible

Each chunk prompt must:

- tell the subprocess it is only responsible for chunk `{n}/{N}`
- require reading `/tmp/course_style_guide.md`
- require reading `/tmp/course_glossary.md` if it exists
- require reading `/tmp/slide_digest.md` if it exists
- include current topic, previous topic, and next topic when available
- require using both transcript and images
- require citing its source range at the end of the chunk output
- ban fabrication, fluffy summary, and process narration
- ask for Markdown only
- allow `[待续]` only when the topic clearly continues
- organize the chunk naturally for the topic instead of forcing a fixed heading template
- cover the relevant items when they are present in the source: core question, key concepts, teacher reasoning, examples/cases, slide points, and possible ASR uncertainty
- include a short `来源` section at the end for internal traceability

Each source JSON should include chunk number, topic, start/end line, image list, source confidence, and any known caveat. Keep it internal unless the user asks for traceability.

Materialization command:

```bash
python3 scripts/course_notes_pipeline.py materialize
```

### 6. Delegate Writing to `codex exec`

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
- for image-heavy chunks, prefer attaching only the images assigned to that chunk plus any immediately adjacent slide needed for context
- if a chunk has high ASR uncertainty, ask the subprocess to preserve uncertainty notes instead of inventing missing content

Failure policy:

1. retry once
2. if still failing, record the failed chunk
3. continue the rest of the job
4. do not abort the whole workflow because one chunk failed

### 7. Quality Check Each Chunk

After each chunk finishes, check:

- file exists
- file is not empty
- content length is not trivially short
- has Markdown headings
- does not mainly contain process narration
- does not contain tool-failure language
- includes a `来源` section with transcript lines and image filenames
- uses glossary spellings for important terms when `/tmp/course_glossary.md` exists
- mentions relevant slide content when images were assigned

Then run a separate `codex exec` quality-check subprocess that returns only:

- `PASS`
- `FAIL: ...`

If the chunk fails QC, rerun that chunk once.

Be careful with over-broad regex checks. Do not flag legitimate content such as `模型` just because it contains the character sequence `我`.

### 8. Merge Chunk Outputs and Build Final Outline

After chunk generation is done, concatenate chunk outputs in order into:

- `/tmp/notes_raw.md`

Concat command:

```bash
python3 scripts/course_notes_pipeline.py concat --total-chunks N
```

Then run one structure-editor `codex exec` subprocess. It should read:

- `/tmp/notes_raw.md`
- `/tmp/course_style_guide.md`
- `/tmp/course_glossary.md` and `/tmp/slide_digest.md` if they exist
- `/tmp/chunk_plan.txt` if it exists

It must write `/tmp/final_outline.md` and must not write the final note. The outline should include:

- final course title
- final chapter and section hierarchy
- which raw chunk sections should be merged, moved, or dropped
- where examples, formulas, charts, comparisons, and teacher reasoning should appear
- where slide-heavy material needs explicit coverage
- terminology decisions that matter for the final note
- source or uncertainty caveats to preserve

The structure-editor is allowed to make editorial decisions about organization, but not to invent content absent from the chunks, transcript, or slide digest.

### 9. Independent Final Writer

Run one independent final-writer `codex exec` subprocess. It should read:

- `/tmp/notes_raw.md`
- `/tmp/final_outline.md`
- `/tmp/course_style_guide.md`
- `/tmp/course_glossary.md` if it exists
- `/tmp/slide_digest.md` if it exists
- `/tmp/chunk_plan.txt` if it exists

The final writer should write `notes.md` in the current course folder as a complete course note, not as a mechanical concatenation of chunk outputs. Do not also copy or write the final note to `~/Desktop/notes.md` unless the user explicitly asks for a desktop-root copy. It should:

- follow `/tmp/final_outline.md` as the main structure
- merge adjacent duplicate sections
- remove chunk seams
- resolve `[待续]`
- normalize titles and terminology
- add a course title
- add a Markdown table of contents
- apply Chinese chapter numbering
- keep useful examples, comparisons, formulas, and teacher reasoning instead of compressing everything into summary bullets
- incorporate slide digest content where it clarifies the lecture
- do not include image filenames, raw slide filenames, or lines like `对应课件图：...` in the public final note unless the user explicitly asks for traceability
- preserve uncertainty notes only when they help the reader avoid false confidence
- remove internal `来源` sections from the public final note unless the user asked for traceability
- avoid adding concepts, cases, or conclusions not supported by the source artifacts

### 10. Editorial Review and Final Revision

Run a separate reviewer `codex exec` subprocess after the first final note is produced. The reviewer should read:

- `notes.md` in the current course folder
- `/tmp/notes_raw.md`
- `/tmp/final_outline.md`
- `/tmp/course_style_guide.md`
- `/tmp/course_glossary.md` if it exists
- `/tmp/slide_digest.md` if it exists
- `/tmp/chunk_plan.txt` if it exists

The reviewer must not rewrite the full note. It should write `/tmp/notes_review.md` with only:

- `PASS` or `NEEDS_REVISION`
- duplicate or missing sections
- terminology inconsistencies
- chunk boundary artifacts
- slide/image coverage gaps
- places where the final writer deviated from `/tmp/final_outline.md` without good reason
- obvious ASR uncertainty that should remain marked
- title/TOC/numbering problems
- concrete revision instructions

If the reviewer returns `NEEDS_REVISION`, run one final revision subprocess that reads `notes.md` in the current course folder and `/tmp/notes_review.md`, then rewrites the same course-folder `notes.md`. Do not run endless review loops; at most one reviewer pass and one revision pass unless the user explicitly asks for deeper editing.

### 11. Final QC and Cleanup

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
- no public-facing internal source map unless requested
- major glossary terms are consistently spelled
- slide-heavy sections are not silently dropped

If the final result is visibly wrong, report that clearly instead of pretending success.

On success, clean:

- `/tmp/chunk_text_*.txt`
- `/tmp/chunk_imgs_*`
- `/tmp/chunk_prompt_*.md`
- `/tmp/chunk_output_*.md`
- `/tmp/chunk_source_*.json`
- `/tmp/notes_raw.md`
- `/tmp/asr_sample.txt`
- `/tmp/sample_imgs/`

Usually keep:

- `/tmp/course_style_guide.md`
- `/tmp/course_glossary.md`
- `/tmp/slide_digest.md`
- `/tmp/final_outline.md`
- `/tmp/notes_review.md`
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
- Use `semantic-plan` by default. Use fixed-size `plan` only for simple transcripts, debugging, or as a fallback.
- Treat character counts as guardrails, not as the primary chunk boundary.
- Prefer "author + reviewer" subprocess roles over asking multiple subprocesses to independently write the same final note.
- Prefer "structure editor + final writer + reviewer + revision editor" for long or complex courses.
- Keep the main agent as scheduler and final arbiter; chunk subprocesses should write local sections, not global conclusions.
- If `codex exec` fails due to repo trust or session persistence, switch to `--skip-git-repo-check --ephemeral`.
- If local `codex exec` usage limits interrupt the full workflow, report exactly which stage was blocked.
- The helper script handles scan/sample/semantic-plan/plan/materialize/concat/cleanup. Keep `codex exec` focused on style-guide generation, glossary, slide digest, difficult semantic-plan drafting, chunk writing, QC, structure editing, independent final writing, editorial review, and final revision.

## Output Contract

When the workflow completes, report:

- total chunk count
- success count
- failure count
- final output path
- any failed chunks
- whether glossary, slide digest, and reviewer pass were used
- whether structure-editor and independent final-writer passes were used
- any known quality caveats

Keep the report short and factual.

## Resources

### scripts/

- `scripts/course_notes_pipeline.py`: deterministic helper for transcript normalization, embedded-image extraction, sample generation, semantic/fallback chunk planning, chunk material creation, raw-output concatenation, and cleanup.

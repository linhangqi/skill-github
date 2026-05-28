#!/usr/bin/env python3
import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path


SUPPORTED_TEXT = {".docx", ".txt", ".md"}
SUPPORTED_IMAGES = {".png", ".jpg", ".jpeg", ".webp"}


def fail(message: str) -> None:
    print(f"ERROR: {message}", file=sys.stderr)
    raise SystemExit(1)


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def natural_key(name: str):
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


def normalize_text_source(src: Path, dest: Path) -> None:
    ext = src.suffix.lower()
    if ext not in SUPPORTED_TEXT:
        fail(f"Unsupported transcript type: {src}")
    ensure_parent(dest)
    if ext == ".docx":
        subprocess.run(
            ["textutil", "-convert", "txt", "-output", str(dest), str(src)],
            check=True,
        )
    else:
        shutil.copyfile(src, dest)


def count_text(text: str) -> dict:
    lines = text.splitlines()
    paragraphs = [p for p in re.split(r"(?:\r?\n){2,}", text) if re.search(r"\S", p)]
    chars = len(re.sub(r"[\r\n]", "", text))
    return {"chars": chars, "lines": len(lines), "paragraphs": len(paragraphs)}


def extract_docx_images(docx_path: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    extracted = []
    with zipfile.ZipFile(docx_path) as zf:
        for member in zf.namelist():
            lower = member.lower()
            if not lower.startswith("word/media/"):
                continue
            ext = Path(member).suffix.lower()
            if ext not in SUPPORTED_IMAGES:
                continue
            target = out_dir / Path(member).name
            with zf.open(member) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(target)
    return sorted(extracted, key=lambda p: natural_key(p.name))


def collect_images_from_dir(image_dir: Path) -> list[Path]:
    if not image_dir.is_dir():
        fail(f"Image directory not found: {image_dir}")
    images = [p for p in image_dir.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_IMAGES]
    return sorted(images, key=lambda p: natural_key(p.name))


def sample_text(text: str, percent: float = 0.05) -> str:
    size = len(text)
    if size == 0:
        return ""
    chunk = max(int(size * percent), 1000 if size > 3000 else 1)
    chunk = min(chunk, size)
    middle = max((size - chunk) // 2, 0)
    parts = [
        text[:chunk],
        text[middle : middle + chunk],
        text[-chunk:],
    ]
    return "\n\n--- SAMPLE BREAK ---\n\n".join(parts)


def parse_paragraphs(lines: list[str]) -> list[dict]:
    paragraphs = []
    start = 1
    current = []
    chars = 0
    for idx, line in enumerate(lines, start=1):
        if re.match(r"^\s*$", line):
            if current:
                paragraphs.append(
                    {
                        "start": start,
                        "end": idx - 1,
                        "text": "".join(current),
                        "chars": chars,
                    }
                )
            start = idx + 1
            current = []
            chars = 0
            continue
        current.append(line)
        chars += len(re.sub(r"[\s\r\n]", "", line))
    if current:
        paragraphs.append(
            {
                "start": start,
                "end": len(lines),
                "text": "".join(current),
                "chars": chars,
            }
        )
    return paragraphs


def chunk_paragraphs(paragraphs: list[dict], target: int = 1800, max_chars: int = 2200, overlap_ratio: float = 0.12):
    chunks = []
    i = 0
    while i < len(paragraphs):
        j = i
        total = 0
        while j < len(paragraphs) and (total < target or j == i) and total + paragraphs[j]["chars"] <= max_chars:
            total += paragraphs[j]["chars"]
            j += 1
        if j == i:
            total += paragraphs[j]["chars"]
            j += 1
        chunks.append(
            {
                "start": paragraphs[i]["start"],
                "end": paragraphs[j - 1]["end"],
                "chars": total,
            }
        )
        back = 0
        next_i = j
        while next_i > i + 1 and back < int(total * overlap_ratio):
            next_i -= 1
            back += paragraphs[next_i]["chars"]
        i = j if next_i <= i else next_i
    while len(chunks) > 1 and chunks[-1]["chars"] < 1200:
        chunks[-2]["end"] = chunks[-1]["end"]
        chunks[-2]["chars"] += chunks[-1]["chars"]
        chunks.pop()
    return chunks


def looks_like_heading(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return False
    if re.match(r"^#{1,6}\s+\S+", stripped):
        return True
    if re.match(r"^(第[一二三四五六七八九十百\d]+[章节课部分]|[一二三四五六七八九十]+[、.．]|\d+[、.．])", stripped):
        return True
    if re.search(r"(目标|定义|作用|框架|流程|方法|步骤|案例|示例|总结|结论|分析|评测|选型|设计|规则|成本|立项|架构|能力|阶段|模型|竞品)$", stripped):
        return len(stripped) <= 40
    return len(stripped) <= 24 and not re.search(r"[，。！？；,.!?;]", stripped)


def heading_signal(text: str) -> tuple[list[str], float]:
    stripped = text.strip()
    rules = []
    score = 0.0
    if not stripped:
        return rules, score
    if re.match(r"^#{1,6}\s+\S+", stripped):
        rules.append("markdown_heading")
        score += 0.95
    if re.match(r"^(第[一二三四五六七八九十百\d]+[章节课部分]|[一二三四五六七八九十]+[、.．]|\d+[、.．])", stripped):
        rules.append("numbered_heading")
        score += 0.85
    if re.search(r"(目标|定义|作用|框架|流程|方法|步骤|案例|示例|总结|结论|分析|评测|选型|设计|规则|成本|立项|架构|能力|阶段|模型|竞品)$", stripped) and len(stripped) <= 40:
        rules.append("keyword_suffix")
        score += 0.65
    if len(stripped) <= 24 and not re.search(r"[，。！？；,.!?;]", stripped):
        rules.append("short_punctuation_free")
        score += 0.45
    return rules, min(score, 0.99)


def line_snippet(lines: list[str], start: int, end: int, limit: int = 360) -> str:
    start = max(start, 1)
    end = min(end, len(lines))
    if start > end:
        return ""
    text = "".join(lines[start - 1 : end])
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def candidate_headings_from_lines(lines: list[str], context_lines: int = 3) -> list[dict]:
    candidates = []
    for idx, line in enumerate(lines, start=1):
        text = line.strip()
        rules, confidence = heading_signal(text)
        if not rules:
            continue
        candidates.append(
            {
                "line": idx,
                "text": text,
                "before": line_snippet(lines, idx - context_lines, idx - 1),
                "after": line_snippet(lines, idx + 1, idx + context_lines),
                "rules": rules,
                "confidence": round(confidence, 2),
            }
        )
    return candidates


def infer_topic(text: str, fallback: str) -> str:
    for line in text.splitlines():
        stripped = re.sub(r"^#{1,6}\s*", "", line.strip())
        stripped = re.sub(r"^[一二三四五六七八九十\d]+[、.．]\s*", "", stripped)
        if stripped:
            return stripped[:48]
    return fallback


def normalize_heading_validations(raw: object, lines: list[str]) -> list[dict]:
    if isinstance(raw, dict):
        raw = raw.get("headings", raw.get("candidates", []))
    if not isinstance(raw, list):
        fail("Validated headings JSON must be a list or an object with a headings/candidates list.")

    accepted = []
    seen = set()
    for idx, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            fail(f"Validated heading item {idx} must be an object.")
        if not item.get("is_boundary", False):
            continue
        line = int(item.get("line", 0))
        if line < 1 or line > len(lines):
            fail(f"Validated heading line out of range at item {idx}: {line}")
        if line in seen:
            continue
        seen.add(line)
        topic = str(item.get("topic") or infer_topic(lines[line - 1], f"Topic {len(accepted) + 1}")).strip()
        accepted.append(
            {
                "line": line,
                "topic": topic[:80],
                "merge_with_previous": bool(item.get("merge_with_previous", False)),
                "reason": str(item.get("reason", ""))[:240],
            }
        )
    accepted.sort(key=lambda item: item["line"])
    return accepted


def semantic_chunks_from_headings(
    headings: list[dict],
    lines: list[str],
    target: int = 2600,
    max_chars: int = 4200,
    min_chars: int = 600,
) -> list[dict]:
    if not headings:
        return semantic_chunks_from_paragraphs(parse_paragraphs(lines), target=target, max_chars=max_chars, min_chars=min_chars)

    boundaries = []
    for item in headings:
        if item["merge_with_previous"] and boundaries:
            boundaries[-1]["topic"] = f"{boundaries[-1]['topic']} / {item['topic']}"[:80]
            continue
        boundaries.append(dict(item))

    if not boundaries or boundaries[0]["line"] != 1:
        first_text = "".join(lines[: boundaries[0]["line"] - 1]) if boundaries else "".join(lines)
        if re.search(r"\S", first_text):
            boundaries.insert(0, {"line": 1, "topic": infer_topic(first_text, "课程导入"), "merge_with_previous": False, "reason": "synthetic_intro"})

    chunks = []
    for idx, boundary in enumerate(boundaries):
        start = boundary["line"]
        end = (boundaries[idx + 1]["line"] - 1) if idx + 1 < len(boundaries) else len(lines)
        if end < start:
            continue
        text = "".join(lines[start - 1 : end])
        chars = len(re.sub(r"[\s\r\n]", "", text))
        chunks.append({"start": start, "end": end, "chars": chars, "topic": boundary["topic"]})

    split_chunks = []
    for chunk in chunks:
        if chunk["chars"] <= max_chars:
            split_chunks.append(chunk)
            continue
        paragraphs = [p for p in parse_paragraphs(lines[chunk["start"] - 1 : chunk["end"]]) if p["chars"]]
        for para in paragraphs:
            para["start"] += chunk["start"] - 1
            para["end"] += chunk["start"] - 1
        subchunks = chunk_paragraphs(paragraphs, target=target, max_chars=max_chars, overlap_ratio=0.05)
        for sub_idx, subchunk in enumerate(subchunks, start=1):
            split_chunks.append({**subchunk, "topic": f"{chunk['topic']}（{sub_idx}/{len(subchunks)}）"})

    merged = []
    for chunk in split_chunks:
        if merged and chunk["chars"] < min_chars and merged[-1]["chars"] + chunk["chars"] <= max_chars:
            merged[-1]["end"] = chunk["end"]
            merged[-1]["chars"] += chunk["chars"]
            merged[-1]["topic"] = f"{merged[-1]['topic']} / {chunk['topic']}"[:80]
        else:
            merged.append(chunk)
    return merged


def semantic_chunks_from_paragraphs(
    paragraphs: list[dict],
    target: int = 2600,
    max_chars: int = 4200,
    min_chars: int = 600,
) -> list[dict]:
    if not paragraphs:
        return []

    units = []
    current = []
    for para in paragraphs:
        first_line = next((line for line in para["text"].splitlines() if line.strip()), "")
        starts_topic = looks_like_heading(first_line)
        current_chars = sum(item["chars"] for item in current)
        if starts_topic and current and current_chars >= min_chars:
            units.append(current)
            current = [para]
        elif current and current_chars + para["chars"] > max_chars:
            units.append(current)
            current = [para]
        else:
            current.append(para)
    if current:
        units.append(current)

    chunks = []
    for unit in units:
        if sum(p["chars"] for p in unit) <= max_chars:
            text = "".join(p["text"] for p in unit)
            chunks.append(
                {
                    "start": unit[0]["start"],
                    "end": unit[-1]["end"],
                    "chars": sum(p["chars"] for p in unit),
                    "topic": infer_topic(text, f"Topic {len(chunks) + 1}"),
                }
            )
            continue

        sub = chunk_paragraphs(unit, target=target, max_chars=max_chars, overlap_ratio=0.08)
        for idx, item in enumerate(sub, start=1):
            text = "".join(p["text"] for p in unit if p["start"] >= item["start"] and p["end"] <= item["end"])
            topic = infer_topic(text, f"Topic {len(chunks) + 1}")
            if len(sub) > 1:
                topic = f"{topic}（{idx}/{len(sub)}）"
            chunks.append({**item, "topic": topic})

    merged = []
    for chunk in chunks:
        if merged and chunk["chars"] < min_chars and merged[-1]["chars"] + chunk["chars"] <= max_chars:
            merged[-1]["end"] = chunk["end"]
            merged[-1]["chars"] += chunk["chars"]
            merged[-1]["topic"] = f"{merged[-1]['topic']} / {chunk['topic']}"[:80]
        else:
            merged.append(chunk)
    return merged


def image_names_for_chunk(images: list[Path], idx: int, total: int) -> list[str]:
    if not images:
        return []
    start_idx = int((idx - 1) * len(images) / total)
    end_idx = int(idx * len(images) / total) - 1
    end_idx = max(start_idx, min(end_idx, len(images) - 1))
    return [img.name for img in images[start_idx : end_idx + 1]]


def write_plan_outputs(chunks: list[dict], images: list[Path], plan_tsv: Path, plan_readable: Path, line_count: int) -> None:
    ensure_parent(plan_tsv)
    with plan_tsv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(
            [
                "chunk",
                "topic",
                "previous_topic",
                "next_topic",
                "start_line",
                "end_line",
                "chars",
                "images",
                "image_start",
                "image_end",
            ]
        )
        total = len(chunks)
        for idx, chunk in enumerate(chunks, start=1):
            names = chunk["images"] if "images" in chunk else image_names_for_chunk(images, idx, total)
            writer.writerow(
                [
                    idx,
                    chunk.get("topic", f"Chunk {idx}"),
                    chunks[idx - 2].get("topic", "") if idx > 1 else "",
                    chunks[idx].get("topic", "") if idx < total else "",
                    chunk["start"],
                    chunk["end"],
                    chunk.get("chars", ""),
                    ",".join(names),
                    names[0] if names else "",
                    names[-1] if names else "",
                ]
            )

    readable_lines = [
        f"Text: {sum(int(c.get('chars') or 0) for c in chunks)} chars, {line_count} lines, {len(chunks)} semantic chunks",
        f"Images: {len(images)} files",
        "",
    ]
    for idx, chunk in enumerate(chunks, start=1):
        names = chunk["images"] if "images" in chunk else image_names_for_chunk(images, idx, len(chunks))
        image_range = f"{names[0]} ~ {names[-1]}" if names else "(none)"
        readable_lines.extend(
            [
                f"Chunk {idx}: {chunk.get('topic', f'Chunk {idx}')}",
                f"- Text: lines {chunk['start']}-{chunk['end']}",
                f"- Images: {image_range}",
                "",
            ]
        )
    ensure_parent(plan_readable)
    plan_readable.write_text("\n".join(readable_lines), encoding="utf-8")
    print(plan_readable.read_text(encoding="utf-8"), end="")


def normalize_semantic_plan(draft: list[dict], lines: list[str], images: list[Path]) -> list[dict]:
    image_names = {img.name for img in images}
    normalized = []
    for idx, item in enumerate(draft, start=1):
        start = int(item.get("start_line", item.get("start", 0)))
        end = int(item.get("end_line", item.get("end", 0)))
        if start < 1 or end < start or end > len(lines):
            fail(f"Invalid semantic plan line range at item {idx}: {start}-{end}")
        names = item.get("images", [])
        if isinstance(names, str):
            names = [part.strip() for part in names.split(",") if part.strip()]
        missing = [name for name in names if name not in image_names]
        if missing:
            fail(f"Images from semantic plan not found: {', '.join(missing)}")
        text = "".join(lines[start - 1 : end])
        normalized.append(
            {
                "start": start,
                "end": end,
                "chars": len(re.sub(r"[\s\r\n]", "", text)),
                "topic": str(item.get("topic") or infer_topic(text, f"Topic {idx}"))[:80],
                "images": names,
            }
        )
    return normalized


def write_chunk_prompt(
    path: Path,
    n: int,
    total: int,
    style_guide: str,
    topic: str = "",
    previous_topic: str = "",
    next_topic: str = "",
) -> None:
    context = ""
    if topic or previous_topic or next_topic:
        context = f"""
## Semantic Context

- Current topic: {topic or f"part {n}"}
- Previous topic: {previous_topic or "(none)"}
- Next topic: {next_topic or "(none)"}

Use this context to avoid duplicate headings and to keep the chunk focused on the current topic.
"""
    body = f"""You are a course note formatting assistant.

This is part {n}/{total} of the full course.

Read the global style guide first:

{style_guide}

Then process this chunk.
{context}

## Input Files

- Text: /tmp/chunk_text_{n}.txt
- Image folder: /tmp/chunk_imgs_{n}/
- Source map: /tmp/chunk_source_{n}.json
- Optional glossary: /tmp/course_glossary.md
- Optional slide digest: /tmp/slide_digest.md

## Rules

- Extract core knowledge from the ASR text
- Filter filler speech, small talk, repetition, and transitions
- Inspect images and extract titles, tables, definitions, and key concepts
- Merge duplicate image/text information
- Preserve image-only information when useful
- Use glossary spellings when /tmp/course_glossary.md exists
- Use /tmp/slide_digest.md as guidance when it exists, but do not treat it as more authoritative than the source text/images
- Organize the chunk naturally for its topic instead of forcing a fixed heading template
- Cover relevant items when present: core question, key concepts, teacher reasoning, examples/cases, slide points, and possible ASR uncertainty
- Include a short source section with transcript lines and image filenames
- Do not fabricate facts
- Do not output process narration
- Keep the note dense and review-friendly
- Use [Pending continuation] only if the current semantic topic clearly continues into the next chunk

## Output Requirement

Output only the final Markdown body.
"""
    ensure_parent(path)
    path.write_text(body, encoding="utf-8")


def cmd_scan(args) -> None:
    text_out = Path(args.text_out)
    image_out = Path(args.image_out)
    report_out = Path(args.report_out) if args.report_out else None
    work_dir = Path(tempfile.mkdtemp(prefix="course-notes-", dir="/tmp"))
    combined = []
    report_lines = []

    for idx, item in enumerate(args.transcript, start=1):
        src = Path(item).expanduser()
        if not src.exists():
            fail(f"Transcript not found: {src}")
        normalized = work_dir / f"asr_src_{idx}.txt"
        normalize_text_source(src, normalized)
        text = normalized.read_text(encoding="utf-8", errors="ignore")
        combined.append(text)
        stats = count_text(text)
        report_lines.append(
            "\t".join(
                [
                    "TRANSCRIPT",
                    str(src),
                    src.suffix.lower(),
                    str(stats["chars"]),
                    str(stats["lines"]),
                    str(stats["paragraphs"]),
                ]
            )
        )

    ensure_parent(text_out)
    text_out.write_text("\n".join(combined), encoding="utf-8")
    merged_stats = count_text(text_out.read_text(encoding="utf-8", errors="ignore"))
    report_lines.append(
        "\t".join(
            [
                "TRANSCRIPT_COMBINED",
                str(text_out),
                ".txt",
                str(merged_stats["chars"]),
                str(merged_stats["lines"]),
                str(merged_stats["paragraphs"]),
            ]
        )
    )

    images = []
    if args.image_dir:
        images = collect_images_from_dir(Path(args.image_dir).expanduser())
        image_out.mkdir(parents=True, exist_ok=True)
        for img in images:
            shutil.copy2(img, image_out / img.name)
        image_source = str(Path(args.image_dir).expanduser())
    elif args.embedded_images_docx:
        image_source = str(Path(args.embedded_images_docx).expanduser())
        if not Path(args.embedded_images_docx).expanduser().exists():
            fail(f"Embedded image docx not found: {args.embedded_images_docx}")
        if image_out.exists():
            shutil.rmtree(image_out)
        images = extract_docx_images(Path(args.embedded_images_docx).expanduser(), image_out)
    else:
        image_source = ""

    if images:
        sorted_ok = [img.name for img in images] == sorted([img.name for img in images], key=natural_key)
        report_lines.append(
            "\t".join(
                [
                    "IMAGES",
                    image_source,
                    str(len(images)),
                    ",".join(sorted({img.suffix.lower().lstrip(".") for img in images})),
                    "yes" if sorted_ok else "no",
                ]
            )
        )
        for img in images:
            report_lines.append("\t".join(["IMAGE_FILE", img.name, img.suffix.lower()]))

    output = "\n".join(report_lines) + "\n"
    if report_out:
        ensure_parent(report_out)
        report_out.write_text(output, encoding="utf-8")
    print(output, end="")


def cmd_sample(args) -> None:
    text = Path(args.text).read_text(encoding="utf-8", errors="ignore")
    sample_out = Path(args.text_sample_out)
    ensure_parent(sample_out)
    sample_out.write_text(sample_text(text), encoding="utf-8")

    image_dir = Path(args.image_dir)
    sample_dir = Path(args.image_sample_dir)
    if sample_dir.exists():
        shutil.rmtree(sample_dir)
    sample_dir.mkdir(parents=True, exist_ok=True)
    images = collect_images_from_dir(image_dir)
    if not images:
        return
    picks = min(len(images), args.max_images)
    if picks == 1:
        chosen = [images[0]]
    else:
        chosen = [images[int(i * (len(images) - 1) / (picks - 1))] for i in range(picks)]
    seen = set()
    for img in chosen:
        if img.name in seen:
            continue
        seen.add(img.name)
        shutil.copy2(img, sample_dir / img.name)
        print(img.name)


def cmd_plan(args) -> None:
    text_path = Path(args.text)
    image_dir = Path(args.image_dir)
    plan_tsv = Path(args.plan_tsv)
    plan_readable = Path(args.plan_readable)
    lines = text_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    paragraphs = parse_paragraphs(lines)
    chunks = chunk_paragraphs(paragraphs, target=args.target_chars, max_chars=args.max_chars, overlap_ratio=args.overlap_ratio)
    images = collect_images_from_dir(image_dir)
    groups = max((len(images) + args.images_per_group - 1) // args.images_per_group, 1) if images else 0

    ensure_parent(plan_tsv)
    with plan_tsv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(["chunk", "start_line", "end_line", "chars", "image_group", "image_start", "image_end"])
        for idx, chunk in enumerate(chunks, start=1):
            names = image_names_for_chunk(images, idx, len(chunks))
            if names:
                group = int((idx - 1) * groups / len(chunks)) + 1
                image_start = names[0]
                image_end = names[-1]
            else:
                group = 0
                image_start = ""
                image_end = ""
            writer.writerow([idx, chunk["start"], chunk["end"], chunk["chars"], group, image_start, image_end])

    readable_lines = [
        f"Text: {sum(c['chars'] for c in chunks)} chars, {len(lines)} lines, {len(chunks)} chunks",
        f"Images: {len(images)} files, {groups} groups",
        "",
    ]
    for idx, chunk in enumerate(chunks, start=1):
        names = image_names_for_chunk(images, idx, len(chunks))
        image_range = f"{names[0]} ~ {names[-1]}" if names else "(none)"
        readable_lines.extend(
            [
                f"Chunk {idx}:",
                f"- Text: lines {chunk['start']}-{chunk['end']}",
                f"- Images: {image_range}",
                "",
            ]
        )
    ensure_parent(plan_readable)
    plan_readable.write_text("\n".join(readable_lines), encoding="utf-8")
    print(plan_readable.read_text(encoding="utf-8"), end="")


def cmd_candidate_headings(args) -> None:
    text_path = Path(args.text)
    lines = text_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    candidates = candidate_headings_from_lines(lines, context_lines=args.context_lines)
    payload = {
        "text": str(text_path),
        "line_count": len(lines),
        "candidate_count": len(candidates),
        "candidates": candidates,
    }
    out = Path(args.output)
    ensure_parent(out)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Wrote {len(candidates)} candidate headings to {out}")


def cmd_semantic_plan(args) -> None:
    text_path = Path(args.text)
    image_dir = Path(args.image_dir)
    lines = text_path.read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    images = collect_images_from_dir(image_dir)

    if args.from_json:
        raw = json.loads(Path(args.from_json).read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw = raw.get("chunks", [])
        if not isinstance(raw, list):
            fail("Semantic JSON plan must be a list or an object with a chunks list.")
        chunks = normalize_semantic_plan(raw, lines, images)
    elif args.from_headings_json:
        raw = json.loads(Path(args.from_headings_json).read_text(encoding="utf-8"))
        headings = normalize_heading_validations(raw, lines)
        chunks = semantic_chunks_from_headings(
            headings,
            lines,
            target=args.target_chars,
            max_chars=args.max_chars,
            min_chars=args.min_chars,
        )
    else:
        paragraphs = parse_paragraphs(lines)
        chunks = semantic_chunks_from_paragraphs(
            paragraphs,
            target=args.target_chars,
            max_chars=args.max_chars,
            min_chars=args.min_chars,
        )
        if len(chunks) == 1 and images and len(images) > args.max_images_per_chunk:
            # Image-heavy decks often have little extracted text. Split by slide groups while
            # keeping the full text available only in the first chunk to avoid losing context.
            split = []
            groups = (len(images) + args.max_images_per_chunk - 1) // args.max_images_per_chunk
            for idx in range(groups):
                start_idx = idx * args.max_images_per_chunk
                end_idx = min((idx + 1) * args.max_images_per_chunk, len(images))
                split.append(
                    {
                        "start": chunks[0]["start"] if idx == 0 else chunks[0]["end"],
                        "end": chunks[0]["end"],
                        "chars": chunks[0]["chars"] if idx == 0 else 0,
                        "topic": f"{chunks[0]['topic']}（课件图 {start_idx + 1}-{end_idx}）",
                        "images": [img.name for img in images[start_idx:end_idx]],
                    }
                )
            chunks = split

    write_plan_outputs(chunks, images, Path(args.plan_tsv), Path(args.plan_readable), len(lines))


def cmd_materialize(args) -> None:
    text_lines = Path(args.text).read_text(encoding="utf-8", errors="ignore").splitlines(keepends=True)
    image_dir = Path(args.image_dir)
    images = {img.name: img for img in collect_images_from_dir(image_dir)}
    plan_rows = []
    with Path(args.plan_tsv).open("r", encoding="utf-8", newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        plan_rows = list(reader)
    total = len(plan_rows)
    for row in plan_rows:
        n = int(row["chunk"])
        start = int(row["start_line"])
        end = int(row["end_line"])
        Path(f"/tmp/chunk_text_{n}.txt").write_text("".join(text_lines[start - 1 : end]), encoding="utf-8")
        chunk_img_dir = Path(f"/tmp/chunk_imgs_{n}")
        if chunk_img_dir.exists():
            shutil.rmtree(chunk_img_dir)
        chunk_img_dir.mkdir(parents=True, exist_ok=True)
        explicit_images = []
        if "images" in row and row["images"]:
            explicit_images = [name.strip() for name in row["images"].split(",") if name.strip()]
            for name in explicit_images:
                if name not in images:
                    fail(f"Image from plan not found in image dir: {name}")
            for name in explicit_images:
                shutil.copy2(images[name], chunk_img_dir / name)
        else:
            for key in ("image_start", "image_end"):
                if row.get(key) and row[key] not in images:
                    fail(f"Image from plan not found in image dir: {row[key]}")
        if not explicit_images and row.get("image_start") and row.get("image_end"):
            ordered = collect_images_from_dir(image_dir)
            copying = []
            started = False
            for img in ordered:
                if img.name == row["image_start"]:
                    started = True
                if started:
                    copying.append(img)
                if img.name == row["image_end"]:
                    break
            for img in copying:
                shutil.copy2(img, chunk_img_dir / img.name)
        write_chunk_prompt(
            Path(f"/tmp/chunk_prompt_{n}.md"),
            n,
            total,
            args.style_guide,
            row.get("topic", ""),
            row.get("previous_topic", ""),
            row.get("next_topic", ""),
        )
        source = {
            "chunk": n,
            "total_chunks": total,
            "topic": row.get("topic", ""),
            "previous_topic": row.get("previous_topic", ""),
            "next_topic": row.get("next_topic", ""),
            "start_line": start,
            "end_line": end,
            "images": explicit_images
            or [p.name for p in sorted(chunk_img_dir.iterdir(), key=lambda p: natural_key(p.name)) if p.is_file()],
            "source_confidence": "planned",
            "caveat": "",
        }
        Path(f"/tmp/chunk_source_{n}.json").write_text(
            json.dumps(source, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    print(f"Created {total} chunk materials.")


def cmd_concat(args) -> None:
    out = Path(args.output)
    ensure_parent(out)
    with out.open("w", encoding="utf-8") as dst:
        for i in range(1, args.total_chunks + 1):
            src = Path(f"/tmp/chunk_output_{i}.md")
            if not src.exists():
                continue
            dst.write(src.read_text(encoding="utf-8", errors="ignore"))
            dst.write("\n\n")
    print(out)


def cmd_cleanup(args) -> None:
    file_patterns = [
        "/tmp/chunk_text_*.txt",
        "/tmp/chunk_prompt_*.md",
        "/tmp/chunk_output_*.md",
        "/tmp/chunk_exec_*.log",
        "/tmp/chunk_qc_*.log",
        "/tmp/chunk_results.log",
        "/tmp/chunk_failures.log",
        "/tmp/asr_sample.txt",
        "/tmp/notes_raw.md",
        "/tmp/chunk_plan.tsv",
        "/tmp/chunk_plan.txt",
        "/tmp/candidate_headings.json",
    ]
    dir_patterns = ["/tmp/chunk_imgs_*", "/tmp/sample_imgs"]
    for pattern in file_patterns:
        for path in Path("/").glob(pattern.lstrip("/")):
            if path.exists():
                path.unlink()
    for pattern in dir_patterns:
        for path in Path("/").glob(pattern.lstrip("/")):
            if path.exists():
                shutil.rmtree(path)
    if not args.keep_core:
        for path in [Path("/tmp/course_style_guide.md"), Path("/tmp/asr_full_text.txt"), Path("/tmp/course_all_imgs")]:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            elif path.exists():
                path.unlink()
    print("Cleanup complete.")


def build_parser():
    parser = argparse.ArgumentParser(description="Course notes orchestration helper.")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan")
    scan.add_argument("--transcript", action="append", required=True, help="Transcript path; repeat for multiple files.")
    scan.add_argument("--image-dir")
    scan.add_argument("--embedded-images-docx")
    scan.add_argument("--text-out", default="/tmp/asr_full_text.txt")
    scan.add_argument("--image-out", default="/tmp/course_all_imgs")
    scan.add_argument("--report-out", default="/tmp/course_scan_report.txt")
    scan.set_defaults(func=cmd_scan)

    sample = sub.add_parser("sample")
    sample.add_argument("--text", default="/tmp/asr_full_text.txt")
    sample.add_argument("--image-dir", default="/tmp/course_all_imgs")
    sample.add_argument("--text-sample-out", default="/tmp/asr_sample.txt")
    sample.add_argument("--image-sample-dir", default="/tmp/sample_imgs")
    sample.add_argument("--max-images", type=int, default=10)
    sample.set_defaults(func=cmd_sample)

    plan = sub.add_parser("plan")
    plan.add_argument("--text", default="/tmp/asr_full_text.txt")
    plan.add_argument("--image-dir", default="/tmp/course_all_imgs")
    plan.add_argument("--plan-tsv", default="/tmp/chunk_plan.tsv")
    plan.add_argument("--plan-readable", default="/tmp/chunk_plan.txt")
    plan.add_argument("--target-chars", type=int, default=1800)
    plan.add_argument("--max-chars", type=int, default=2200)
    plan.add_argument("--overlap-ratio", type=float, default=0.12)
    plan.add_argument("--images-per-group", type=int, default=5)
    plan.set_defaults(func=cmd_plan)

    candidate_headings = sub.add_parser("candidate-headings")
    candidate_headings.add_argument("--text", default="/tmp/asr_full_text.txt")
    candidate_headings.add_argument("--output", default="/tmp/candidate_headings.json")
    candidate_headings.add_argument("--context-lines", type=int, default=3)
    candidate_headings.set_defaults(func=cmd_candidate_headings)

    semantic_plan = sub.add_parser("semantic-plan")
    semantic_plan.add_argument("--text", default="/tmp/asr_full_text.txt")
    semantic_plan.add_argument("--image-dir", default="/tmp/course_all_imgs")
    semantic_plan.add_argument("--plan-tsv", default="/tmp/chunk_plan.tsv")
    semantic_plan.add_argument("--plan-readable", default="/tmp/chunk_plan.txt")
    semantic_inputs = semantic_plan.add_mutually_exclusive_group()
    semantic_inputs.add_argument("--from-json", help="Optional model-authored semantic plan JSON to validate and normalize.")
    semantic_inputs.add_argument("--from-headings-json", help="Optional model-validated heading boundary JSON to turn into a semantic plan.")
    semantic_plan.add_argument("--target-chars", type=int, default=2600)
    semantic_plan.add_argument("--max-chars", type=int, default=4200)
    semantic_plan.add_argument("--min-chars", type=int, default=600)
    semantic_plan.add_argument("--max-images-per-chunk", type=int, default=8)
    semantic_plan.set_defaults(func=cmd_semantic_plan)

    materialize = sub.add_parser("materialize")
    materialize.add_argument("--text", default="/tmp/asr_full_text.txt")
    materialize.add_argument("--image-dir", default="/tmp/course_all_imgs")
    materialize.add_argument("--plan-tsv", default="/tmp/chunk_plan.tsv")
    materialize.add_argument("--style-guide", default="/tmp/course_style_guide.md")
    materialize.set_defaults(func=cmd_materialize)

    concat = sub.add_parser("concat")
    concat.add_argument("--output", default="/tmp/notes_raw.md")
    concat.add_argument("--total-chunks", type=int, required=True)
    concat.set_defaults(func=cmd_concat)

    cleanup = sub.add_parser("cleanup")
    cleanup.add_argument("--keep-core", action="store_true")
    cleanup.set_defaults(func=cmd_cleanup)

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
import argparse
import csv
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


def write_chunk_prompt(path: Path, n: int, total: int, style_guide: str) -> None:
    body = f"""You are a course note formatting assistant.

This is part {n}/{total} of the full course.

Read the global style guide first:

{style_guide}

Then process this chunk.

## Input Files

- Text: /tmp/chunk_text_{n}.txt
- Image folder: /tmp/chunk_imgs_{n}/

## Rules

- Extract core knowledge from the ASR text
- Filter filler speech, small talk, repetition, and transitions
- Inspect images and extract titles, tables, definitions, and key concepts
- Merge duplicate image/text information
- Preserve image-only information when useful
- Do not fabricate facts
- Do not output process narration
- Keep the note dense and review-friendly
- Use [Pending continuation] only if the topic clearly continues

## Output Format

```markdown
## Topic Title

> One-sentence summary.

Main body in clear written style.

### Key Concepts

- **Concept A**: explanation

### Method / Process

1. Step one
2. Step two

### Example or Analogy

> Valuable teacher example or analogy.

### Table

| Item | Meaning | Notes |
|---|---|---|
|  |  |  |
```

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
            if groups:
                group = int((idx - 1) * groups / len(chunks)) + 1
                start_idx = (group - 1) * args.images_per_group
                end_idx = min(group * args.images_per_group, len(images)) - 1
                image_start = images[start_idx].name
                image_end = images[end_idx].name
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
        if groups:
            group = int((idx - 1) * groups / len(chunks)) + 1
            start_idx = (group - 1) * args.images_per_group
            end_idx = min(group * args.images_per_group, len(images)) - 1
            image_range = f"{images[start_idx].name} ~ {images[end_idx].name}"
        else:
            image_range = "(none)"
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
        for key in ("image_start", "image_end"):
            if row[key] and row[key] not in images:
                fail(f"Image from plan not found in image dir: {row[key]}")
        if row["image_start"] and row["image_end"]:
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
        write_chunk_prompt(Path(f"/tmp/chunk_prompt_{n}.md"), n, total, args.style_guide)
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

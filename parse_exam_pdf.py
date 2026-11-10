import argparse
import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pdfplumber


QUESTION_RE = re.compile(r"^\s*(\d+)\.\s*(.*)$")
OPTION_RE = re.compile(r"^\s*([•√])\s*(.*)$")
HEADER_MARKERS = ("imtahan testinin sualları", "fənn :")
MAX_OPTION_COUNT = 5


@dataclass
class LineItem:
    page_number: int
    top: float
    bottom: float
    x0: float
    text: str
    kind: str = "text"


@dataclass
class ImageItem:
    page_number: int
    top: float
    bottom: float
    path: str
    width: float
    height: float
    kind: str = "image"


@dataclass
class OptionData:
    key: str
    text_parts: List[str] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    is_correct: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "text": join_parts(self.text_parts),
            "images": self.images,
            "is_correct": self.is_correct,
        }


@dataclass
class QuestionData:
    number: int
    page_start: int
    text_parts: List[str] = field(default_factory=list)
    images: List[str] = field(default_factory=list)
    options: List[OptionData] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "number": self.number,
            "page_start": self.page_start,
            "text": join_parts(self.text_parts),
            "images": self.images,
            "options": [option.to_dict() for option in self.options],
        }


def join_parts(parts: List[str]) -> str:
    cleaned = [part.strip() for part in parts if part and part.strip()]
    return "\n".join(cleaned)


def normalize_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", text or "")
    return re.sub(r"\s+", " ", normalized).strip()


def normalized_fold(text: str) -> str:
    return normalize_text(text).casefold()


def is_header_line(text: str) -> bool:
    normalized = normalized_fold(text)
    return any(marker in normalized for marker in HEADER_MARKERS)


def make_safe_stem(path: Path) -> str:
    normalized = unicodedata.normalize("NFKD", path.stem)
    ascii_name = normalized.encode("ascii", "ignore").decode("ascii")
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", ascii_name).strip("._")
    return safe_name or "parsed_pdf"


def is_question_start(
    line: LineItem,
    current_question: Optional[QuestionData],
    current_option: Optional[OptionData],
    expected_question_number: int,
) -> Optional[re.Match[str]]:
    match = QUESTION_RE.match(line.text)
    if not match:
        return None

    number = int(match.group(1))
    if number == expected_question_number:
        return match
    if current_question is None or number <= current_question.number:
        return None

    if line.x0 > 18:
        return None

    if current_option is not None or len(current_question.options) >= 3:
        return match
    return None


def is_bullet_only_option_line(line: LineItem) -> bool:
    match = OPTION_RE.match(line.text)
    return bool(match and not match.group(2).strip())


def build_text_lines(page: pdfplumber.page.Page) -> List[LineItem]:
    words = page.extract_words(use_text_flow=True, keep_blank_chars=False)
    if not words:
        return []

    lines: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    last_top: Optional[float] = None

    for word in words:
        top = round(word["top"], 1)
        if last_top is None or abs(top - last_top) <= 3:
            current.append(word)
        else:
            lines.append(current)
            current = [word]
        last_top = top

    if current:
        lines.append(current)

    result: List[LineItem] = []
    for raw_line in lines:
        raw_line = sorted(raw_line, key=lambda item: item["x0"])
        text = " ".join(item["text"] for item in raw_line)
        line = LineItem(
            page_number=page.page_number,
            top=min(item["top"] for item in raw_line),
            bottom=max(item["bottom"] for item in raw_line),
            x0=min(item["x0"] for item in raw_line),
            text=normalize_text(text),
        )
        if line.text:
            result.append(line)
    return merge_numbered_lines(result)


def merge_numbered_lines(lines: List[LineItem]) -> List[LineItem]:
    merged: List[LineItem] = []
    index = 0

    while index < len(lines):
        current = lines[index]
        if (
            re.match(r"^\d+\.$", current.text)
            and index + 1 < len(lines)
            and lines[index + 1].page_number == current.page_number
            and abs(lines[index + 1].top - current.top) <= 12
        ):
            next_line = lines[index + 1]
            merged.append(
                LineItem(
                    page_number=current.page_number,
                    top=min(current.top, next_line.top),
                    bottom=max(current.bottom, next_line.bottom),
                    x0=min(current.x0, next_line.x0),
                    text=f"{current.text} {next_line.text}",
                )
            )
            index += 2
            continue

        merged.append(current)
        index += 1

    return merged


def export_page_images(
    page: pdfplumber.page.Page,
    output_dir: Path,
    page_items: List[LineItem],
) -> List[ImageItem]:
    images: List[ImageItem] = []
    seen: set[Tuple[int, int, int, int]] = set()

    for index, image in enumerate(page.images):
        bbox = (
            max(0, image["x0"]),
            max(0, image["top"]),
            min(page.width, image["x1"]),
            min(page.height, image["bottom"]),
        )
        rounded = tuple(int(value) for value in bbox)
        if rounded in seen:
            continue
        seen.add(rounded)

        width = bbox[2] - bbox[0]
        height = bbox[3] - bbox[1]
        if width < 8 or height < 8:
            continue

        image_name = f"page_{page.page_number:03d}_img_{index:03d}.png"
        image_path = output_dir / image_name
        page.crop(bbox).to_image(resolution=200).save(image_path)

        images.append(
            ImageItem(
                page_number=page.page_number,
                top=bbox[1],
                bottom=bbox[3],
                path=image_path.as_posix(),
                width=width,
                height=height,
            )
        )
    return images


def assign_image_target(
    image: ImageItem,
    current_question: Optional[QuestionData],
    current_option: Optional[OptionData],
    last_question_bottom: Optional[float],
    line_lookup: List[LineItem],
    carryover_option: Optional[OptionData] = None,
) -> Tuple[str, Optional[str]]:
    if carryover_option is not None:
        return ("option", carryover_option.key)

    first_line = min(line_lookup, key=lambda line: line.top, default=None)
    if (
        current_option is not None
        and first_line is not None
        and image.page_number == first_line.page_number
        and image.bottom <= first_line.top
    ):
        return ("option", current_option.key)

    nearby_line = min(
        line_lookup,
        key=lambda line: abs(((line.top + line.bottom) / 2) - ((image.top + image.bottom) / 2)),
        default=None,
    )

    if nearby_line:
        option_match = OPTION_RE.match(nearby_line.text)
        if option_match and current_option is not None:
            return ("option", current_option.key)
        question_match = QUESTION_RE.match(nearby_line.text)
        if question_match and current_question is not None:
            return ("question", None)

    if (
        current_option is not None
        and current_question is not None
        and image.page_number > current_question.page_start
    ):
        return ("option", current_option.key)

    if current_option is not None and last_question_bottom is not None and image.top >= last_question_bottom:
        return ("option", current_option.key)
    return ("question", None)


def parse_pdf(pdf_path: Path, output_root: Path) -> Dict[str, Any]:
    pdf_output_dir = output_root / make_safe_stem(pdf_path)
    images_dir = pdf_output_dir / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    questions: List[QuestionData] = []
    current_question: Optional[QuestionData] = None
    current_option: Optional[OptionData] = None
    option_counter = 0
    last_question_bottom: Optional[float] = None
    pending_question_lines: List[str] = []
    deferred_option_prefix: List[str] = []
    deferred_question_prefix: List[str] = []
    expected_question_number = 1

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            lines = build_text_lines(page)
            page_images = export_page_images(page, images_dir, lines)

            elements: List[Any] = [*lines, *page_images]
            elements.sort(key=lambda item: (item.top, 0 if item.kind == "text" else 1))

            for index, element in enumerate(elements):
                if element.kind == "text":
                    text = normalize_text(element.text)
                    if not text or is_header_line(text):
                        continue

                    next_text = next(
                        (
                            candidate
                            for candidate in elements[index + 1 :]
                            if candidate.kind == "text" and normalize_text(candidate.text)
                        ),
                        None,
                    )
                    next_question_text = next(
                        (
                            candidate
                            for candidate in elements[index + 1 :]
                            if candidate.kind == "text"
                            and normalize_text(candidate.text)
                            and is_question_start(
                                candidate,
                                current_question,
                                current_option,
                                expected_question_number,
                            )
                            is not None
                        ),
                        None,
                    )
                    previous_text = next(
                        (
                            candidate
                            for candidate in reversed(elements[:index])
                            if candidate.kind == "text" and normalize_text(candidate.text)
                        ),
                        None,
                    )

                    was_option_context = current_option is not None or option_counter >= MAX_OPTION_COUNT
                    question_match = is_question_start(
                        element,
                        current_question,
                        current_option,
                        expected_question_number,
                    )
                    if question_match:
                        matched_number = int(question_match.group(1))
                        current_question = QuestionData(
                            number=matched_number,
                            page_start=element.page_number,
                        )
                        questions.append(current_question)
                        current_option = None
                        option_counter = 0
                        last_question_bottom = element.bottom
                        expected_question_number = matched_number + 1
                        if pending_question_lines:
                            current_question.text_parts.extend(pending_question_lines)
                            pending_question_lines = []
                        if deferred_question_prefix:
                            current_question.text_parts.extend(deferred_question_prefix)
                            deferred_question_prefix = []
                        remainder = question_match.group(2).strip()
                        if remainder:
                            current_question.text_parts.append(remainder)
                        continue

                    option_match = OPTION_RE.match(text)
                    if option_match and current_question is not None:
                        if pending_question_lines:
                            if was_option_context:
                                pending_question_lines = []
                            else:
                                current_question.text_parts.extend(pending_question_lines)
                                pending_question_lines = []
                        option_counter += 1
                        current_option = OptionData(
                            key=chr(64 + option_counter),
                            is_correct=option_match.group(1) == "√",
                        )
                        current_question.options.append(current_option)
                        remainder = option_match.group(2).strip()
                        if remainder:
                            current_option.text_parts.append(remainder)
                        elif deferred_option_prefix:
                            current_option.text_parts.extend(deferred_option_prefix)
                            deferred_option_prefix = []
                        continue

                    if (
                        next_text is not None
                        and is_bullet_only_option_line(next_text)
                        and 0 < next_text.top - element.top <= 10
                        and element.x0 >= 40
                    ):
                        deferred_option_prefix.append(text)
                        continue

                    if (
                        next_question_text is not None
                        and 0 < next_question_text.top - element.top <= 40
                        and element.x0 >= 40
                        and current_question is not None
                        and len(current_question.options) >= 4
                        and not (
                            previous_text is not None
                            and is_bullet_only_option_line(previous_text)
                            and 0 <= element.top - previous_text.top <= 12
                        )
                    ):
                        deferred_question_prefix.append(text)
                        continue

                    if current_option is not None:
                        if option_counter > MAX_OPTION_COUNT:
                            pending_question_lines.append(text)
                        else:
                            current_option.text_parts.append(text)
                    elif current_question is not None:
                        if pending_question_lines:
                            current_question.text_parts.extend(pending_question_lines)
                            pending_question_lines = []
                        current_question.text_parts.append(text)
                        last_question_bottom = element.bottom
                else:
                    if current_question is None:
                        continue
                    carryover_option = None
                    first_line = min(lines, key=lambda line: line.top, default=None)
                    if (
                        current_option is not None
                        and first_line is not None
                        and element.page_number == first_line.page_number
                        and element.bottom <= first_line.top
                    ):
                        carryover_option = current_option
                    target_kind, _ = assign_image_target(
                        element,
                        current_question,
                        current_option,
                        last_question_bottom,
                        lines,
                        carryover_option,
                    )
                    if target_kind == "option" and current_option is not None:
                        current_option.images.append(element.path)
                    else:
                        current_question.images.append(element.path)

    result = {
        "source_pdf": pdf_path.as_posix(),
        "question_count": len(questions),
        "questions": [question.to_dict() for question in questions],
    }

    json_path = pdf_output_dir / f"{pdf_path.stem}.json"
    json_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract exam questions, options, and related images from PDF files."
    )
    parser.add_argument(
        "pdfs",
        nargs="*",
        help="PDF files to parse. If omitted, all PDFs in the current directory are used.",
    )
    parser.add_argument(
        "--output-dir",
        default="parsed_output",
        help="Directory where JSON files and extracted images will be written.",
    )
    args = parser.parse_args()

    output_root = Path(args.output_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)

    pdf_paths = [Path(item) for item in args.pdfs] if args.pdfs else sorted(Path(".").glob("*.pdf"))
    if not pdf_paths:
        raise SystemExit("No PDF files found.")

    summary = []
    for pdf_path in pdf_paths:
        parsed = parse_pdf(pdf_path.resolve(), output_root)
        summary.append(
            {
                "pdf": pdf_path.as_posix(),
                "question_count": parsed["question_count"],
                "json": (output_root / pdf_path.stem / f"{pdf_path.stem}.json").as_posix(),
            }
        )

    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

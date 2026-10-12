"""Knowledge base per course: transcripts (lecture) + reading texts (supplement).

One Markdown file per course::

    knowledge/<slug>.md

Each lecture/supplement appends one section (deduplicated by ``item_id``).
The file doubles as the knowledge base for quiz solving: when an
assessment is solved with ``--llm``, the collected text is injected into
the LLM prompt so answers are grounded in the actual course material
instead of the model's general knowledge.
"""

import html
import re
from pathlib import Path
from urllib.parse import urljoin
from loguru import logger

COURSERA_ORIGIN = "https://www.coursera.org"
SECTION_SEPARATOR = "\n\n---\n\n"


def sanitize_filename(name: str, max_len: int = 80) -> str:
    """Make a menu name safe to use as a file name (keeps it readable)."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name or "").strip()
    cleaned = re.sub(r"\s+", "-", cleaned)
    cleaned = re.sub(r"-{2,}", "-", cleaned).strip("-")
    if not cleaned:
        cleaned = "item"
    return cleaned[:max_len]


def srt_to_text(srt: str) -> str:
    """Strip SRT/VTT timestamps + counters, return flowing plain text."""
    lines = []
    for raw in (srt or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.upper() == "WEBVTT":
            continue
        if line.isdigit():
            continue
        if "-->" in line:
            continue
        if line.startswith(("NOTE ", "STYLE", "REGION")):
            continue
        # VTT cue settings after timestamp are already skipped above.
        if lines and lines[-1] == line:
            continue  # dedupe consecutive repeats
        lines.append(line)
    text = " ".join(lines)
    return re.sub(r"\s+", " ", text).strip()


def cml_to_markdown(cml: str) -> str:
    """Convert Coursera CML (XML-ish) supplement content to Markdown."""
    if not cml:
        return ""
    out = cml

    # Headings: <heading level="1">..</heading>
    def _heading(m):
        level = m.group(1)
        try:
            hashes = "#" * min(int(level), 6)
        except ValueError:
            hashes = "##"
        return f"\n\n{hashes} {m.group(2).strip()}\n\n"

    out = re.sub(r'<heading\s+level="(\d+)">(.*?)</heading>', _heading, out, flags=re.DOTALL)
    # Paragraphs
    out = re.sub(r"<text>(.*?)</text>", lambda m: f"\n\n{m.group(1).strip()}\n\n", out, flags=re.DOTALL)
    # Links: <a href="..">label</a>
    out = re.sub(
        r'<a\s+href="([^"]+)">(.*?)</a>',
        lambda m: f"[{m.group(2).strip()}]({m.group(1).strip()})",
        out,
        flags=re.DOTALL,
    )
    # List items
    out = re.sub(r"<li>(.*?)</li>", lambda m: f"\n- {m.group(1).strip()}", out, flags=re.DOTALL)
    # Table cells/rows -> pipe rows
    out = re.sub(r"</tr\s*>", "\n", out)
    out = re.sub(r"</td\s*>", " | ", out)
    out = re.sub(r"<table[^>]*>", "\n\n", out)
    out = re.sub(r"</table\s*>", "\n\n", out)
    # Code / images
    out = re.sub(r"<pre>(.*?)</pre>", lambda m: f"\n\n```\n{m.group(1).strip()}\n```\n\n", out, flags=re.DOTALL)
    out = re.sub(r"<code>(.*?)</code>", lambda m: f"`{m.group(1).strip()}`", out, flags=re.DOTALL)
    out = re.sub(r'<img[^>]*src="([^"]+)"[^>]*>', lambda m: f"\n\n![]({m.group(1).strip()})\n\n", out)
    # Drop any remaining tags
    out = re.sub(r"<[^>]+>", "", out)
    out = html.unescape(out)
    out = re.sub(r"[ \t]+\n", "\n", out)
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out.strip()


def fetch_lecture_transcript(session, base_url: str, course_id: str, item_id: str,
                              lang: str = "en") -> str:
    """Download a lecture's subtitle track and return it as plain text."""
    r = session.get(
        base_url + f"onDemandLectureVideos.v1/{course_id}~{item_id}",
        params={"includes": "video"},
        timeout=60,
    )
    if r.status_code != 200:
        return ""
    try:
        videos = r.json().get("linked", {}).get("onDemandVideos.v1", [])
    except ValueError:
        return ""
    if not videos:
        return ""
    subtitles = videos[0].get("subtitles", {}) or {}
    # Preferred language -> English -> any available track.
    track = subtitles.get(lang) or subtitles.get("en")
    if not track and subtitles:
        track = next(iter(subtitles.values()))
    if not track:
        return ""
    sub = session.get(urljoin(COURSERA_ORIGIN, track), timeout=60)
    if sub.status_code != 200 or not sub.text.strip():
        return ""
    return srt_to_text(sub.text)


def fetch_supplement_markdown(session, base_url: str, course_id: str, item_id: str) -> str:
    """Download a reading's CML asset and return it as Markdown."""
    r = session.get(
        base_url + f"onDemandSupplements.v1/{course_id}~{item_id}",
        params={"includes": "asset"},
        timeout=60,
    )
    if r.status_code != 200:
        return ""
    try:
        assets = r.json().get("linked", {}).get("openCourseAssets.v1", [])
    except ValueError:
        return ""
    for asset in assets:
        definition = asset.get("definition", {}) or {}
        value = definition.get("value", "")
        if value:
            return cml_to_markdown(value)
    return ""


def default_knowledge_dir() -> Path:
    return Path.cwd() / "knowledge"


def knowledge_path(knowledge_dir: str | Path, course_slug: str) -> Path:
    safe = sanitize_filename(course_slug, max_len=100).lower() or "course"
    base = Path(knowledge_dir) if knowledge_dir else default_knowledge_dir()
    return base / f"{safe}.md"


def _marker(item_id: str) -> str:
    return f"<!-- skipera:item_id={item_id} -->"


def _build_section(order: int, item: dict, kind: str, text: str, url: str = "") -> str:
    lines = [
        f"## {order:03d} — {item.get('name', '')} ({kind})",
        _marker(item.get("id", "")),
    ]
    if url:
        lines.append(f"URL: {url}")
    lines += ["", text.strip()]
    return "\n".join(lines)


def _replace_section(content: str, item_id: str, section: str) -> str:
    """Swap the section carrying ``item_id`` with a fresh one."""
    chunks = content.split(SECTION_SEPARATOR)
    marker = _marker(item_id)
    for i, chunk in enumerate(chunks):
        if marker in chunk:
            chunks[i] = section
            break
    else:
        chunks.append(section)
    return SECTION_SEPARATOR.join(chunks)


def has_knowledge(course_slug: str, knowledge_dir: str | Path, item_id: str) -> bool:
    """True when the course file already holds a section for ``item_id``."""
    path = knowledge_path(knowledge_dir, course_slug)
    if not path.is_file():
        return False
    try:
        return _marker(item_id) in path.read_text(encoding="utf-8")
    except OSError:
        return False


def save_knowledge(course_slug: str, knowledge_dir: str | Path, order: int,
                   item: dict, kind: str, text: str, url: str = "",
                   overwrite: bool = False) -> str | None:
    """Append one section to ``knowledge/<slug>.md`` (skips existing items)."""
    if not text or not text.strip():
        return None
    base = Path(knowledge_dir) if knowledge_dir else default_knowledge_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = knowledge_path(base, course_slug)
    section = _build_section(order, item, kind, text, url)
    marker = _marker(item.get("id", ""))

    if path.exists():
        content = path.read_text(encoding="utf-8")
        if marker in content:
            if not overwrite:
                return str(path)  # cached — do not refetch/rewrite
            content = _replace_section(content, item.get("id", ""), section)
            path.write_text(content, encoding="utf-8")
            logger.info(f"Knowledge diperbarui: {path} ({item.get('name', '')})")
            return str(path)
        content = content.rstrip("\n") + SECTION_SEPARATOR + section + "\n"
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(f"# Knowledge: {course_slug}\n" + SECTION_SEPARATOR + section + "\n",
                        encoding="utf-8")
    logger.info(f"Knowledge tersimpan: {path} (+{len(text)} karakter)")
    return str(path)


def load_knowledge_context(knowledge_dir: str | Path, course_slug: str,
                           max_chars: int = 40000) -> str:
    """Read ``knowledge/<slug>.md``, keeping the most recent ``max_chars``.

    Assessment material usually sits right before its quiz, so when the
    budget is exceeded the tail (most recent sections) wins over older ones.
    """
    path = knowledge_path(knowledge_dir, course_slug)
    if not path.is_file():
        return ""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    # Drop the title header; sections carry their own headings.
    content = re.sub(r"\A# Knowledge:.*?\n", "", content, count=1).strip()
    if len(content) > max_chars:
        tail = content[-max_chars:]
        # Avoid starting mid-section: jump to the next section heading.
        cut = tail.find("\n## ")
        if 0 < cut < len(tail) // 2:
            tail = tail[cut + 1:]
        content = tail
    logger.info(f"Knowledge context: {path} ({len(content)} karakter)")
    return content

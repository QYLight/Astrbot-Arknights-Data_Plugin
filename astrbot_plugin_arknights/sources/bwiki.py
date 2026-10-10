from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from html.parser import HTMLParser
from typing import Iterable
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")
API_URL = "https://wiki.biligame.com/arknights/api.php"
ACTIVITY_PAGE = "活动关卡"
GACHA_PAGE = "用户:418054283/sandbox/寻访池一览"
GACHA_PAGE_FALLBACKS = ("寻访池一览", "卡池一览", "首页当前寻访池")


@dataclass(frozen=True)
class GameEvent:
    name: str
    event_type: str
    start_at: int
    end_at: int
    source_url: str


@dataclass(frozen=True)
class FetchResult:
    events: list[GameEvent]
    diagnostics: str


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.tables: list[list[list[str]]] = []
        self._table: list[list[str]] | None = None
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "table":
            self._table = []
        elif tag == "tr" and self._table is not None:
            self._row = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"td", "th"} and self._cell is not None and self._row is not None:
            self._row.append(_clean_text("".join(self._cell)))
            self._cell = None
        elif tag == "tr" and self._row is not None and self._table is not None:
            if self._row:
                self._table.append(self._row)
            self._row = None
        elif tag == "table" and self._table is not None:
            if self._table:
                self.tables.append(self._table)
            self._table = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _page_url(page: str) -> str:
    return "https://wiki.biligame.com/arknights/" + page


def _fetch_page_html(page: str, timeout: int = 20) -> str:
    query = urlencode(
        {
            "action": "parse",
            "page": page,
            "prop": "text",
            "format": "json",
            "formatversion": "2",
        }
    )
    request = Request(
        f"{API_URL}?{query}",
        headers={"User-Agent": "AstrBot-Arknights-Demo/0.1"},
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if "error" in payload:
        raise RuntimeError(payload["error"].get("info", "BWIKI API 返回错误"))
    return payload["parse"]["text"]


def _parse_datetime(
    value: str,
    default_year: int | None = None,
    default_month: int | None = None,
    default_day: int | None = None,
) -> datetime | None:
    value = re.sub(r"\s+", "", value)
    patterns = (
        (
            r"(?P<year>\d{4})年(?P<month>\d{1,2})月(?P<day>\d{1,2})日?"
            r"(?:[日周一二三四五六])?(?P<hour>\d{1,2}):(?P<minute>\d{2})"
        ),
        (
            r"(?P<year>\d{4})[-/.](?P<month>\d{1,2})[-/.](?P<day>\d{1,2})"
            r"(?:[日周一二三四五六])?(?P<hour>\d{1,2}):(?P<minute>\d{2})"
        ),
        (
            r"(?P<month>\d{1,2})月(?P<day>\d{1,2})日?"
            r"(?:[日周一二三四五六])?(?P<hour>\d{1,2}):(?P<minute>\d{2})"
        ),
        (
            r"(?P<month>\d{1,2})/(?P<day>\d{1,2})"
            r"(?:[日周一二三四五六])?(?P<hour>\d{1,2}):(?P<minute>\d{2})"
        ),
        (
            r"(?P<month>\d{1,2})[-.](?P<day>\d{1,2})"
            r"(?:[日周一二三四五六])?(?P<hour>\d{1,2}):(?P<minute>\d{2})"
        ),
    )
    for pattern in patterns:
        match = re.search(pattern, value)
        if not match:
            continue
        parts = match.groupdict()
        year_number = int(parts.get("year") or default_year or datetime.now(SHANGHAI).year)
        month_number = int(parts.get("month") or default_month or 1)
        day_number = int(parts.get("day") or default_day or 1)
        try:
            return datetime(
                year_number,
                month_number,
                day_number,
                int(parts["hour"]),
                int(parts["minute"]),
                tzinfo=SHANGHAI,
            )
        except ValueError:
            return None
    return None


def _parse_range(value: str) -> tuple[int, int] | None:
    value = re.sub(r"\s+", "", value)
    matches = list(
        re.finditer(
            r"(?:\d{4}年\d{1,2}月\d{1,2}日?"
            r"|\d{4}[-/.]\d{1,2}[-/.]\d{1,2}"
            r"|\d{1,2}月\d{1,2}日?)"
            r"(?:[日周一二三四五六])?\d{1,2}:\d{2}",
            value,
        )
    )
    if len(matches) < 2:
        return None
    first = _parse_datetime(matches[0].group(0))
    second = _parse_datetime(matches[1].group(0), first.year if first else None)
    if not first or not second or second <= first:
        return None
    return int(first.timestamp()), int(second.timestamp())


def _looks_like_time(value: str) -> bool:
    return len(_extract_datetimes(value)) >= 2


def _extract_datetimes(value: str) -> list[datetime]:
    value = re.sub(r"\s+", "", value)
    patterns = (
        r"(?:\d{4}年\d{1,2}月\d{1,2}日?|\d{4}[-/.]\d{1,2}[-/.]\d{1,2})"
        r"(?:[日周一二三四五六])?\d{1,2}:\d{2}",
        r"\d{1,2}月\d{1,2}日?(?:[日周一二三四五六])?\d{1,2}:\d{2}",
        r"\d{1,2}[/.-]\d{1,2}(?:[日周一二三四五六])?\d{1,2}:\d{2}",
    )
    matches: list[re.Match[str]] = []
    for pattern in patterns:
        matches.extend(re.finditer(pattern, value))
    matches.sort(key=lambda item: item.start())
    datetimes: list[datetime] = []
    for match in matches:
        parsed = _parse_datetime(match.group(0))
        if parsed and (not datetimes or parsed != datetimes[-1]):
            datetimes.append(parsed)
    if len(datetimes) >= 2:
        return datetimes

    date_matches = list(
        re.finditer(
            r"(?:\d{4}年)?\d{1,2}(?:月|[/.-])\d{1,2}日?",
            value,
        )
    )
    time_matches = list(re.finditer(r"\d{1,2}:\d{2}", value))
    if len(date_matches) >= 2 and len(time_matches) >= 2:
        for date_match, time_match in zip(date_matches[:2], time_matches[:2]):
            parsed = _parse_datetime(
                f"{date_match.group(0)}{time_match.group(0)}"
            )
            if parsed:
                datetimes.append(parsed)
    return datetimes


def _iter_event_rows(html: str) -> Iterable[tuple[str, str]]:
    parser = _TableParser()
    parser.feed(html)
    for table in parser.tables:
        for row in table:
            row_text = " ".join(row)
            datetimes = _extract_datetimes(row_text)
            if len(datetimes) >= 2:
                time_text = row_text
            elif len(row) >= 3:
                time_text = " ".join(row[-2:])
                datetimes = _extract_datetimes(time_text)
            else:
                continue
            if len(datetimes) < 2:
                continue
            candidates = [
                cell
                for cell in row
                if not _extract_datetimes(cell) and len(cell) >= 2
            ]
            if not candidates:
                continue
            yield candidates[0], time_text


def _clean_gacha_name(value: str) -> str:
    value = re.sub(
        r"文件:[^|]*?\.(?:png|jpg|jpeg)",
        "",
        value,
        flags=re.I,
    )
    value = re.sub(r"^(?:[|:/_-]\s*)+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value


def _looks_like_date_only(value: str) -> bool:
    value = re.sub(r"\s+", "", value)
    return bool(
        re.fullmatch(
            r"(?:\d{4}年)?\d{1,2}(?:月|[/.-])\d{1,2}日?",
            value,
        )
    )


def _iter_gacha_rows(html: str) -> Iterable[tuple[str, str]]:
    parser = _TableParser()
    parser.feed(html)
    for table in parser.tables:
        for row in table:
            row_text = " ".join(row)
            if _parse_range(row_text) is None or len(row) < 2:
                continue
            datetimes = _extract_datetimes(row_text)
            if len(datetimes) < 2:
                continue

            time_index = next(
                (index for index, cell in enumerate(row) if len(_extract_datetimes(cell)) >= 2),
                None,
            )
            if time_index is not None:
                candidates = [
                    cell
                    for index, cell in enumerate(row)
                    if index != time_index
                    and len(cell) >= 2
                    and not _looks_like_date_only(cell)
                ]
            else:
                candidates = [
                    cell
                    for cell in row
                    if not _extract_datetimes(cell)
                    and not _looks_like_date_only(cell)
                    and len(cell) >= 2
                ]

            # “寻访池一览”通常是：编号、名称、时间、UP干员。
            # 第一列是内部编号，第二列才是用户可读名称。
            if not candidates:
                continue
            name = next(
                (
                    candidate
                    for candidate in candidates
                    if not re.fullmatch(r"\d+", candidate)
                    and not re.fullmatch(r"[A-Za-z0-9_.-]+", candidate)
                ),
                candidates[0],
            )
            name = _clean_gacha_name(name)
            if name and not _looks_like_date_only(name):
                yield name, row_text


def _diagnose_tables(html: str) -> str:
    parser = _TableParser()
    parser.feed(html)
    table_count = len(parser.tables)
    row_count = sum(len(table) for table in parser.tables)
    time_row_count = 0
    samples: list[str] = []
    for table in parser.tables:
        for row in table:
            row_text = _clean_text(" | ".join(row))
            if _extract_datetimes(row_text):
                time_row_count += 1
                if len(samples) < 3:
                    samples.append(row_text[:160])
    sample_text = "；".join(samples) if samples else "无"
    return f"表格 {table_count} 个，行 {row_count} 行，含时间行 {time_row_count} 行，样例：{sample_text}"


def fetch_events_with_diagnostics() -> FetchResult:
    results: list[GameEvent] = []
    diagnostics: list[str] = []
    for page, event_type in ((ACTIVITY_PAGE, "activity"),):
        html = _fetch_page_html(page)
        for name, time_text in _iter_event_rows(html):
            time_range = _parse_range(time_text)
            if not time_range:
                continue
            start_at, end_at = time_range
            results.append(
                GameEvent(
                    name=name,
                    event_type=event_type,
                    start_at=start_at,
                    end_at=end_at,
                    source_url=_page_url(page),
                )
            )
    activity_count = sum(event.event_type == "activity" for event in results)
    diagnostics.append(f"活动页 {ACTIVITY_PAGE}：解析 {activity_count} 条")
    gacha_page = GACHA_PAGE
    gacha_rows: list[tuple[str, str]] = []
    for page in (GACHA_PAGE, *GACHA_PAGE_FALLBACKS):
        try:
            html = _fetch_page_html(page)
        except Exception as exc:
            diagnostics.append(f"卡池页 {page}：请求失败 {exc}")
            continue
        gacha_rows = list(_iter_gacha_rows(html))
        diagnostics.append(
            f"卡池页 {page}：解析 {len(gacha_rows)} 条；{_diagnose_tables(html)}"
        )
        if gacha_rows:
            gacha_page = page
            break
    for name, time_text in gacha_rows:
        time_range = _parse_range(time_text)
        if not time_range:
            continue
        start_at, end_at = time_range
        if end_at - start_at > 366 * 86400:
            continue
        results.append(
            GameEvent(
                name=name,
                event_type="gacha",
                start_at=start_at,
                end_at=end_at,
                source_url=_page_url(gacha_page),
            )
        )
    unique: dict[tuple[str, str, int], GameEvent] = {}
    for event in results:
        unique[(event.event_type, event.name, event.start_at)] = event
    events = list(unique.values())
    return FetchResult(events=events, diagnostics="\n".join(diagnostics))


def fetch_events() -> list[GameEvent]:
    return fetch_events_with_diagnostics().events

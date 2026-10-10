from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .sources.bwiki import fetch_events_with_diagnostics


SYNC_INTERVAL_SECONDS = 30 * 60
SHANGHAI = ZoneInfo("Asia/Shanghai")


@register(
    "arknights_event_monitor",
    "LightBash",
    "明日方舟活动和卡池监测 Demo",
    "0.1.0",
)
class ArknightsEventMonitor(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._db: sqlite3.Connection | None = None
        self._sync_task: asyncio.Task | None = None
        self._running = True

    async def initialize(self):
        getter = getattr(self, "get_data_dir", None)
        data_dir = Path(getter()) if getter else Path("data/plugin_data/arknights_event_monitor")
        data_dir.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(data_dir / "arknights_events.sqlite3", check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_key TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                start_at INTEGER NOT NULL,
                end_at INTEGER NOT NULL,
                source_url TEXT NOT NULL,
                synced_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS sync_state (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                synced_at INTEGER,
                status TEXT NOT NULL,
                message TEXT NOT NULL
            );
            """
        )
        self._db.commit()
        self._sync_task = asyncio.create_task(self._sync_loop())

    async def terminate(self):
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass
        if self._db:
            self._db.close()

    @filter.command("方舟活动")
    async def list_activities(self, event: AstrMessageEvent):
        yield event.plain_result(self._format_events("activity", "当前活动"))

    @filter.command("方舟卡池")
    async def list_gachas(self, event: AstrMessageEvent):
        yield event.plain_result(self._format_events("gacha", "当前卡池"))

    @filter.command("方舟全部")
    async def list_all(self, event: AstrMessageEvent):
        yield event.plain_result(
            self._format_events("activity", "当前活动")
            + "\n\n"
            + self._format_events("gacha", "当前卡池")
        )

    @filter.command("方舟刷新")
    async def manual_sync(self, event: AstrMessageEvent):
        message = await self._sync_once()
        yield event.plain_result(message)

    @filter.command("方舟同步状态")
    async def sync_status(self, event: AstrMessageEvent):
        row = self._db.execute("SELECT * FROM sync_state WHERE id=1").fetchone()
        if not row:
            yield event.plain_result("还没有同步记录，请先执行 /方舟刷新。")
            return
        synced_at = self._format_time(row["synced_at"]) if row["synced_at"] else "无"
        yield event.plain_result(
            f"上次同步：{synced_at}\n状态：{row['status']}\n信息：{row['message']}"
        )

    async def _sync_loop(self):
        while self._running:
            await self._sync_once()
            await asyncio.sleep(SYNC_INTERVAL_SECONDS)

    async def _sync_once(self) -> str:
        try:
            result = await asyncio.to_thread(fetch_events_with_diagnostics)
            events = result.events
            if not events:
                raise RuntimeError("没有解析到带完整时间范围的活动或卡池")
            activity_count = sum(item.event_type == "activity" for item in events)
            gacha_count = sum(item.event_type == "gacha" for item in events)
            now = int(time.time())
            with self._db:
                self._db.execute("DELETE FROM events")
                for item in events:
                    key = f"{item.event_type}:{item.name}:{item.start_at}"
                    self._db.execute(
                        """
                        INSERT INTO events(
                            event_key, name, event_type, start_at, end_at,
                            source_url, synced_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            key,
                            item.name,
                            item.event_type,
                            item.start_at,
                            item.end_at,
                            item.source_url,
                            now,
                        ),
                    )
                self._db.execute(
                    """
                    INSERT INTO sync_state(id, synced_at, status, message)
                    VALUES (1, ?, 'success', ?)
                    ON CONFLICT(id) DO UPDATE SET
                        synced_at=excluded.synced_at,
                        status=excluded.status,
                        message=excluded.message
                    """,
                    (
                        now,
                        f"成功同步 {len(events)} 条记录"
                        f"（活动 {activity_count} 条，卡池 {gacha_count} 条）\n"
                        f"{result.diagnostics}",
                    ),
                )
            return (
                f"同步成功，共更新 {len(events)} 条记录。\n"
                f"活动：{activity_count} 条，卡池：{gacha_count} 条。\n"
                f"{result.diagnostics[:1200]}"
            )
        except Exception as exc:
            message = str(exc)[:300]
            with self._db:
                self._db.execute(
                    """
                    INSERT INTO sync_state(id, synced_at, status, message)
                    VALUES (1, ?, 'failed', ?)
                    ON CONFLICT(id) DO UPDATE SET
                        synced_at=excluded.synced_at,
                        status=excluded.status,
                        message=excluded.message
                    """,
                    (int(time.time()), message),
                )
            return f"同步失败：{message}\n已有缓存数据不会被删除。"

    def _format_events(self, event_type: str, title: str) -> str:
        now = int(time.time())
        rows = self._db.execute(
            """
            SELECT name, start_at, end_at, source_url
            FROM events
            WHERE event_type=? AND end_at > ?
            ORDER BY start_at
            """,
            (event_type, now),
        ).fetchall()
        if not rows:
            return f"{title}：暂无缓存数据，请先执行 /方舟刷新。"
        lines = [f"{title}："]
        for row in rows[:20]:
            state = "未开始" if row["start_at"] > now else "进行中"
            remaining = self._format_remaining(row["end_at"] - now)
            lines.append(f"「{row['name']}」\n状态：{state}，剩余：{remaining}")
        return "\n".join(lines)

    @staticmethod
    def _format_remaining(seconds: int) -> str:
        if seconds <= 0:
            return "已结束"
        days, remainder = divmod(seconds, 86400)
        hours, remainder = divmod(remainder, 3600)
        minutes, _ = divmod(remainder, 60)
        parts = []
        if days:
            parts.append(f"{days}天")
        if hours or days:
            parts.append(f"{hours}小时")
        parts.append(f"{minutes}分钟")
        return " ".join(parts)

    @staticmethod
    def _format_time(timestamp: int) -> str:
        return datetime.fromtimestamp(timestamp, SHANGHAI).strftime("%Y-%m-%d %H:%M:%S")

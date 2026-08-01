"""导出器测试：.ics 与 Markdown 行程单。"""

import os
import tempfile
import unittest
from datetime import date

from core.schemas import DayPlan, Place, TripTimeline
from itinerary.ics_exporter import build_ics, write_ics
from itinerary.markdown_exporter import render_markdown, write_markdown


def make_timeline() -> TripTimeline:
    return TripTimeline(
        city="北京",
        start_date=date(2026, 8, 1),
        end_date=date(2026, 8, 2),
        days=[
            DayPlan(day=1, date=date(2026, 8, 1), items=[
                Place(name="故宫", category="scenic", arrival="09:00", queue_min=20, ticket_required=True),
                Place(name="全聚德(前门店)", category="food", arrival="18:00"),
            ]),
        ],
    )


class TestIcsExporter(unittest.TestCase):
    def test_build_ics_contains_events(self) -> None:
        ics = build_ics(make_timeline())
        self.assertIn("BEGIN:VCALENDAR", ics)
        self.assertIn("BEGIN:VEVENT", ics)
        self.assertIn("故宫", ics)
        self.assertIn("END:VCALENDAR", ics)

    def test_write_ics_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "行程.ics")
            content = write_ics(make_timeline(), path)
            self.assertTrue(os.path.exists(path))
            self.assertIn("故宫", content)


class TestMarkdownExporter(unittest.TestCase):
    def test_render_contains_city_and_places(self) -> None:
        md = render_markdown(make_timeline())
        self.assertIn("北京", md)
        self.assertIn("故宫", md)
        self.assertIn("Day 1", md)
        self.assertIn("需预约", md)

    def test_write_markdown_creates_file(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "行程单.md")
            content = write_markdown(make_timeline(), path, notes=["测试备注"])
            self.assertTrue(os.path.exists(path))
            self.assertIn("测试备注", content)


if __name__ == "__main__":
    unittest.main()

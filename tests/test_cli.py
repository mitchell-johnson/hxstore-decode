"""Tests for hxdecode CLI structure and formatters.

These tests validate that the CLI module loads correctly and that the
formatters produce expected output, without requiring parser modules
or an actual HxStore.hxd file.
"""

from __future__ import annotations

from datetime import datetime, timezone

import click.testing
import pytest

from hxdecode.cli import cli
from hxdecode.formatters import (
    format_csv,
    format_json,
    format_record_detail,
    format_table,
)


# ---------------------------------------------------------------------------
# CLI structure tests
# ---------------------------------------------------------------------------


class TestCLIStructure:
    """Verify that the CLI group and commands are correctly wired."""

    def test_cli_group_exists(self) -> None:
        assert cli is not None
        assert isinstance(cli, click.Group)

    def test_cli_has_info_command(self) -> None:
        assert "info" in cli.commands

    def test_cli_has_mail_group(self) -> None:
        assert "mail" in cli.commands
        mail_cmd = cli.commands["mail"]
        assert isinstance(mail_cmd, click.Group)

    def test_mail_has_show_subcommand(self) -> None:
        mail_cmd = cli.commands["mail"]
        assert isinstance(mail_cmd, click.Group)
        assert "show" in mail_cmd.commands

    def test_cli_has_search_command(self) -> None:
        assert "search" in cli.commands

    def test_cli_has_records_command(self) -> None:
        assert "records" in cli.commands

    def test_cli_has_blob_search_command(self) -> None:
        assert "blob-search" in cli.commands

    def test_help_text(self) -> None:
        runner = click.testing.CliRunner()
        result = runner.invoke(cli, ["--help"])
        assert result.exit_code == 0
        assert "Decode and inspect" in result.output

    def test_info_help(self) -> None:
        runner = click.testing.CliRunner()
        result = runner.invoke(cli, ["info", "--help"])
        assert result.exit_code == 0
        assert "--path" in result.output

    def test_mail_help(self) -> None:
        runner = click.testing.CliRunner()
        result = runner.invoke(cli, ["mail", "--help"])
        assert result.exit_code == 0
        assert "--limit" in result.output
        assert "--format" in result.output

    def test_mail_show_help(self) -> None:
        runner = click.testing.CliRunner()
        result = runner.invoke(cli, ["mail", "show", "--help"])
        assert result.exit_code == 0
        assert "RECORD_ID" in result.output

    def test_search_help(self) -> None:
        runner = click.testing.CliRunner()
        result = runner.invoke(cli, ["search", "--help"])
        assert result.exit_code == 0
        assert "QUERY" in result.output

    def test_records_help(self) -> None:
        runner = click.testing.CliRunner()
        result = runner.invoke(cli, ["records", "--help"])
        assert result.exit_code == 0
        assert "--limit" in result.output

    def test_blob_search_help(self) -> None:
        runner = click.testing.CliRunner()
        result = runner.invoke(cli, ["blob-search", "--help"])
        assert result.exit_code == 0
        assert "QUERY" in result.output


# ---------------------------------------------------------------------------
# Formatter tests
# ---------------------------------------------------------------------------


class TestFormatTable:
    """Tests for format_table."""

    def test_empty_records(self) -> None:
        assert format_table([], ["a", "b"]) == "(no records)"

    def test_simple_table(self) -> None:
        records = [
            {"name": "Alice", "age": 30},
            {"name": "Bob", "age": 25},
        ]
        result = format_table(records, ["name", "age"])
        assert "Alice" in result
        assert "Bob" in result
        assert "name" in result
        assert "age" in result
        # Should have border characters.
        assert "+" in result
        assert "|" in result

    def test_missing_key_shows_empty(self) -> None:
        records = [{"name": "Alice"}]
        result = format_table(records, ["name", "missing_col"])
        assert "Alice" in result
        # The missing column should still appear as a header.
        assert "missing_col" in result

    def test_truncation(self) -> None:
        records = [{"val": "x" * 100}]
        result = format_table(records, ["val"], max_col_width=20)
        assert "..." in result

    def test_datetime_formatting(self) -> None:
        dt = datetime(2025, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
        records = [{"date": dt}]
        result = format_table(records, ["date"])
        assert "2025-06-15" in result

    def test_bytes_formatting(self) -> None:
        records = [{"data": b"\xde\xad\xbe\xef"}]
        result = format_table(records, ["data"])
        assert "deadbeef" in result

    def test_list_formatting(self) -> None:
        records = [{"tags": ["a", "b", "c"]}]
        result = format_table(records, ["tags"])
        assert "a, b, c" in result


class TestFormatJSON:
    """Tests for format_json."""

    def test_empty_list(self) -> None:
        result = format_json([])
        assert result == "[]"

    def test_simple_records(self) -> None:
        import json

        records = [{"id": 1, "name": "test"}]
        result = format_json(records)
        parsed = json.loads(result)
        assert len(parsed) == 1
        assert parsed[0]["name"] == "test"

    def test_datetime_serialised(self) -> None:
        import json

        dt = datetime(2025, 1, 1, tzinfo=timezone.utc)
        records = [{"ts": dt}]
        result = format_json(records)
        parsed = json.loads(result)
        assert "2025-01-01" in parsed[0]["ts"]

    def test_bytes_serialised_as_hex(self) -> None:
        import json

        records = [{"raw": b"\xab\xcd"}]
        result = format_json(records)
        parsed = json.loads(result)
        assert parsed[0]["raw"] == "abcd"


class TestFormatCSV:
    """Tests for format_csv."""

    def test_empty_records(self) -> None:
        assert format_csv([]) == ""

    def test_simple_csv(self) -> None:
        records = [{"a": "1", "b": "2"}, {"a": "3", "b": "4"}]
        result = format_csv(records, ["a", "b"])
        lines = result.strip().split("\n")
        assert len(lines) == 3  # header + 2 data rows
        assert "a,b" in lines[0]

    def test_column_filtering(self) -> None:
        records = [{"a": "1", "b": "2", "c": "3"}]
        result = format_csv(records, ["a", "c"])
        assert "b" not in result.split("\n")[0]

    def test_auto_columns(self) -> None:
        records = [{"x": "1", "y": "2"}]
        result = format_csv(records)
        assert "x" in result
        assert "y" in result


class TestFormatRecordDetail:
    """Tests for format_record_detail."""

    def test_basic_detail(self) -> None:
        record = {"record_id": 12345, "sender": "alice@example.com", "subject": "Hello"}
        result = format_record_detail(record)
        assert "12345" in result
        assert "alice@example.com" in result
        assert "Hello" in result
        assert "=" * 72 in result

    def test_priority_ordering(self) -> None:
        record = {"zebra": "last", "record_id": 1, "sender": "x@y.z"}
        result = format_record_detail(record)
        # record_id should appear before zebra.
        id_pos = result.index("Record Id")
        zebra_pos = result.index("Zebra")
        assert id_pos < zebra_pos

    def test_hex_dump_off_by_default(self) -> None:
        record = {"record_id": 1, "raw_data": b"\x00" * 64}
        result = format_record_detail(record)
        assert "Hex dump" not in result

    def test_hex_dump_on(self) -> None:
        record = {"record_id": 1, "raw_data": b"\xde\xad" * 32}
        result = format_record_detail(record, show_hex=True)
        assert "Hex dump" in result
        assert "de ad" in result

    def test_none_values(self) -> None:
        record = {"record_id": None, "subject": None}
        result = format_record_detail(record)
        # Should not crash; None renders as empty.
        assert "Record Id" in result

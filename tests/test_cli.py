"""Tests for hxdecode CLI structure, formatters, and extract helpers.

These tests validate that the CLI module loads correctly and that the
formatters produce expected output, without requiring parser modules
or an actual HxStore.hxd file.
"""

from __future__ import annotations

from datetime import datetime, timezone

import click.testing
import pytest

from hxdecode.cli import cli
from hxdecode.constants import (
    DOTNET_SENTINEL,
    DOTNET_TICKS_MAX,
    DOTNET_TICKS_MIN,
    DOTNET_TICKS_PER_SECOND,
    MSG_DATA_ID_OFFSETS,
)
from hxdecode.extract import extract_email_fields
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
        assert "--sort" in result.output

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


# ---------------------------------------------------------------------------
# Constants tests
# ---------------------------------------------------------------------------


class TestConstants:
    """Verify .NET ticks constants are correctly defined."""

    def test_dotnet_sentinel_is_8_bytes(self) -> None:
        assert len(DOTNET_SENTINEL) == 8

    def test_dotnet_sentinel_value(self) -> None:
        assert DOTNET_SENTINEL == b"\xff\x3f\x37\xf4\x75\x28\xca\x2b"

    def test_ticks_per_second(self) -> None:
        assert DOTNET_TICKS_PER_SECOND == 10_000_000

    def test_ticks_range_order(self) -> None:
        assert DOTNET_TICKS_MIN < DOTNET_TICKS_MAX

    def test_msg_data_id_offsets_is_tuple(self) -> None:
        assert isinstance(MSG_DATA_ID_OFFSETS, tuple)
        assert len(MSG_DATA_ID_OFFSETS) == 3


# ---------------------------------------------------------------------------
# Email field extraction tests
# ---------------------------------------------------------------------------


class TestExtractEmailFields:
    """Tests for the centralised extract_email_fields helper."""

    def test_no_ipm_note_returns_empty(self) -> None:
        result = extract_email_fields(["hello", "world"], 0x03B0)
        assert result == ("", "", "")

    def test_03b0_format_basic(self) -> None:
        strings = [
            "IPM.Note",
            "sender@example.com",
            "Jane Doe",
            "IPM.Note",
            "Re: Test Subject",
        ]
        sender_email, sender_name, subject = extract_email_fields(strings, 0x03B0)
        assert sender_email == "sender@example.com"
        assert sender_name == "Jane Doe"
        assert subject == "Re: Test Subject"

    def test_03b0_format_with_msgid(self) -> None:
        strings = [
            "IPM.Note",
            "sender@example.com",
            "Jane Doe",
            "IPM.Note",
            "<abc123@mail.example.com>",
            "body preview here",
            "Actual Subject",
        ]
        sender_email, sender_name, subject = extract_email_fields(strings, 0x03B0)
        assert sender_email == "sender@example.com"
        assert sender_name == "Jane Doe"
        # After skipping message-IDs, body_preview is next, then subject
        assert subject == "Actual Subject"

    def test_0191_format(self) -> None:
        strings = [
            "IPM.Note",
            "sender@example.com",
            "sender@example.com",
            "body preview",
            "Sender Name",
            "Test Subject",
        ]
        sender_email, sender_name, subject = extract_email_fields(strings, 0x0191)
        assert sender_email == "sender@example.com"
        assert sender_name == "Sender Name"
        assert subject == "Test Subject"

    def test_10013_format(self) -> None:
        strings = [
            "IPM.Note",
            "<msg-id@example.com>",
            "body preview text",
            "The Subject Line",
        ]
        sender_email, sender_name, subject = extract_email_fields(strings, 0x10013)
        assert subject == "The Subject Line"

    def test_empty_strings_list(self) -> None:
        result = extract_email_fields([], 0x03B0)
        assert result == ("", "", "")

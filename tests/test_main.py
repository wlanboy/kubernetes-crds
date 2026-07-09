from __future__ import annotations

import sys
from unittest.mock import patch

import main
from kubectl import CRDVersionedInfo, CRDVersionInfo


def _version(version: str, served: bool = True, storage: bool = True,
             instances: dict[str, int] | None = None) -> CRDVersionInfo:
    v = CRDVersionInfo(version=version, served=served, storage=storage)
    if instances:
        v.instances_by_namespace = instances
    return v


def _crd_info(name: str, group: str, kind: str, namespaced: bool,
              versions: list[CRDVersionInfo]) -> CRDVersionedInfo:
    return CRDVersionedInfo(
        name=name, group=group, kind=kind, plural=name.split(".")[0],
        namespaced=namespaced, versions=versions,
    )


class TestFormatTable:
    def test_aligns_columns_and_includes_header_separator(self):
        rows = [("a", "bb"), ("ccc", "d")]
        headers = ("H1", "H2")

        output = main._format_table(rows, headers)
        lines = output.splitlines()

        assert len(lines) == 4  # header + separator + 2 data rows
        assert lines[0].startswith("H1")
        assert set(lines[1]) <= {"-", " "}

    def test_column_width_grows_to_fit_widest_cell(self):
        rows = [("short", "x")]
        headers = ("VERYLONGHEADER", "H2")

        output = main._format_table(rows, headers)
        header_line, _, data_line = output.splitlines()

        assert header_line.startswith("VERYLONGHEADER")
        assert data_line.startswith("short         ")  # padded to header width


class TestMain:
    def test_prints_message_when_no_crds_found(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=[]):
            exit_code = main.main()

        assert exit_code == 0
        assert "No CRDs found." in capsys.readouterr().out

    def test_namespace_column_present_when_no_namespace_filter_given(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True,
                           [_version("v1", instances={"ns-a": 2})])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "NAMESPACE" in out
        row = next(line for line in out.splitlines() if "widgets.example.io" in line)
        assert "ns-a" in row
        assert row.split()[-1] == "2"

    def test_namespace_column_absent_when_namespace_filter_given(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py", "-n", "ns-a"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True,
                           [_version("v1", instances={"ns-a": 2})])]

        with patch("main.load_config"), patch("main.get_crd_versions") as get_versions:
            get_versions.return_value = crds
            main.main()

        out = capsys.readouterr().out
        assert "NAMESPACE" not in out
        get_versions.assert_called_once_with(namespace="ns-a")
        row = next(line for line in out.splitlines() if "widgets.example.io" in line)
        assert row.split()[-1] == "2"

    def test_row_shows_dash_when_version_has_no_instances(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        row = next(line for line in out.splitlines() if "widgets.example.io" in line)
        assert row.split()[-1] == "-"

    def test_each_namespace_with_instances_gets_its_own_row(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True,
                           [_version("v1", instances={"ns-a": 1, "ns-b": 3})])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        rows = [line for line in out.splitlines() if "widgets.example.io" in line]
        assert len(rows) == 2

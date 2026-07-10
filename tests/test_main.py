from __future__ import annotations

import sys
from unittest.mock import MagicMock, patch

import urllib3
from kubernetes.client.rest import ApiException
from kubernetes.config import ConfigException

import main
from kubectl import CRDVersionedInfo, CRDVersionInfo


def _version(version: str, served: bool = True, storage: bool = True,
             instances: dict[str, int] | None = None, deprecated: bool = False,
             deprecation_warning: str | None = None) -> CRDVersionInfo:
    v = CRDVersionInfo(version=version, served=served, storage=storage,
                        deprecated=deprecated, deprecation_warning=deprecation_warning)
    if instances:
        v.instances_by_namespace = instances
    return v


def _crd_info(name: str, group: str, kind: str, namespaced: bool,
              versions: list[CRDVersionInfo],
              stored_versions: list[str] | None = None,
              conversion_strategy: str = "None",
              conversion_webhook_target: str | None = None,
              conversion_webhook_ca_bundle_present: bool = False,
              established: bool = True, names_accepted: bool = True,
              established_message: str | None = None,
              names_accepted_message: str | None = None) -> CRDVersionedInfo:
    return CRDVersionedInfo(
        name=name, group=group, kind=kind, plural=name.split(".")[0],
        namespaced=namespaced, versions=versions,
        stored_versions=stored_versions if stored_versions is not None
        else [v.version for v in versions if v.storage],
        conversion_strategy=conversion_strategy,
        conversion_webhook_target=conversion_webhook_target,
        conversion_webhook_ca_bundle_present=conversion_webhook_ca_bundle_present,
        established=established, names_accepted=names_accepted,
        established_message=established_message,
        names_accepted_message=names_accepted_message,
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

    def test_unused_flag_lists_only_crds_without_instances(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py", "--unused"])
        crds = [
            _crd_info("widgets.example.io", "example.io", "Widget", True,
                      [_version("v1", instances={"ns-a": 2})]),
            _crd_info("gadgets.example.io", "example.io", "Gadget", True, [_version("v1")]),
        ]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "gadgets.example.io" in out
        assert "widgets.example.io" not in out

    def test_unused_flag_prints_message_when_none_unused(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py", "--unused"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True,
                           [_version("v1", instances={"ns-a": 2})])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            exit_code = main.main()

        assert exit_code == 0
        assert "No unused CRDs found." in capsys.readouterr().out

    def test_each_namespace_with_instances_gets_its_own_row(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True,
                           [_version("v1", instances={"ns-a": 1, "ns-b": 3})])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        rows = [line for line in out.splitlines() if "widgets.example.io" in line]
        assert len(rows) == 2

    def test_deprecated_column_shows_yes_for_deprecated_version(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True,
                           [_version("v1alpha1", storage=False, deprecated=True,
                                     deprecation_warning="use v1 instead"),
                            _version("v1")])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "DEPRECATED" in out
        alpha_row = next(line for line in out.splitlines() if "v1alpha1" in line)
        stable_row = next(line for line in out.splitlines()
                           if "v1" in line.split() and "v1alpha1" not in line)
        assert alpha_row.split()[8] == "yes"
        assert stable_row.split()[8] == "no"

    def test_conversion_column_shows_webhook_strategy(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True,
                           [_version("v1")], conversion_strategy="Webhook")]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "CONVERSION" in out
        row = next(line for line in out.splitlines() if "widgets.example.io" in line)
        assert row.split()[4] == "Webhook"

    def test_conversion_column_shows_none_by_default(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        row = next(line for line in out.splitlines() if "widgets.example.io" in line)
        assert row.split()[4] == "None"

    def test_deprecation_warning_is_printed_below_the_table(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True,
                           [_version("v1alpha1", storage=False, deprecated=True,
                                     deprecation_warning="use v1 instead"),
                            _version("v1")])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "Deprecated API versions:" in out
        assert "widgets.example.io v1alpha1: use v1 instead" in out

    def test_no_deprecation_section_when_nothing_is_deprecated(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "Deprecated API versions:" not in out

    def test_migration_candidate_is_printed_when_old_stored_version_remains(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True,
                           [_version("v1alpha1", storage=False), _version("v1")],
                           stored_versions=["v1alpha1", "v1"])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "Storage version migration candidates" in out
        assert "widgets.example.io: instances still stored as ['v1alpha1']" in out

    def test_no_migration_section_when_stored_versions_are_up_to_date(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "Storage version migration candidates" not in out

    def test_unused_view_also_reports_migration_candidates(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py", "--unused"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True,
                           [_version("v1alpha1", storage=False),
                            _version("v1", instances={"ns-a": 1})],
                           stored_versions=["v1alpha1", "v1"])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "No unused CRDs found." in out
        assert "Storage version migration candidates" in out

    def test_webhook_conversion_target_is_printed(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")],
                           conversion_strategy="Webhook",
                           conversion_webhook_target="widgets-webhook.widgets-system:8443/convert",
                           conversion_webhook_ca_bundle_present=True)]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "Webhook conversion targets (reachability not verified):" in out
        assert ("widgets.example.io: "
                "widgets-webhook.widgets-system:8443/convert") in out
        assert "no caBundle configured" not in out

    def test_webhook_conversion_target_flags_missing_ca_bundle(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")],
                           conversion_strategy="Webhook",
                           conversion_webhook_target="widgets-webhook.widgets-system:8443")]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert ("widgets.example.io: widgets-webhook.widgets-system:8443 "
                "(no caBundle configured)") in out

    def test_webhook_conversion_target_falls_back_when_no_client_config(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")],
                           conversion_strategy="Webhook")]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "widgets.example.io: no clientConfig configured" in out

    def test_no_webhook_conversion_section_when_no_webhook_strategy_present(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "Webhook conversion targets" not in out

    def test_not_established_crd_is_reported(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")],
                           established=False, established_message="not all requests are served")]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "Unhealthy CRDs (status conditions):" in out
        assert "widgets.example.io: not Established (not all requests are served)" in out

    def test_names_not_accepted_crd_is_reported(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")],
                           names_accepted=False,
                           names_accepted_message="widgets.example.io already in use")]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "Unhealthy CRDs (status conditions):" in out
        assert ("widgets.example.io: NamesAccepted=False "
                "(widgets.example.io already in use)") in out

    def test_no_unhealthy_section_when_all_crds_are_healthy(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")])]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "Unhealthy CRDs" not in out

    def test_unused_view_also_reports_unhealthy_crds(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py", "--unused"])
        crds = [_crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")],
                           established=False)]

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=crds):
            main.main()

        out = capsys.readouterr().out
        assert "widgets.example.io" in out
        assert "Unhealthy CRDs (status conditions):" in out

    def test_config_exception_is_reported_cleanly_with_exit_code_1(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py"])

        with patch("main.load_config", side_effect=ConfigException("no kubeconfig found")):
            exit_code = main.main()

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "could not load Kubernetes configuration" in err
        assert "no kubeconfig found" in err

    def test_verbose_flag_enables_debug_logging(self, monkeypatch):
        import logging

        monkeypatch.setattr(sys, "argv", ["main.py", "--verbose"])

        with patch("main.load_config"), patch("main.get_crd_versions", return_value=[]):
            main.main()

        assert logging.getLogger().level == logging.DEBUG

    def test_api_exception_while_fetching_crds_is_reported_cleanly_with_exit_code_1(
        self, capsys, monkeypatch,
    ):
        monkeypatch.setattr(sys, "argv", ["main.py"])

        with patch("main.load_config"), \
             patch("main.get_crd_versions", side_effect=ApiException(status=403, reason="Forbidden")):
            exit_code = main.main()

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "could not reach the Kubernetes API server" in err

    def test_connection_error_while_fetching_crds_is_reported_cleanly_with_exit_code_1(
        self, capsys, monkeypatch,
    ):
        monkeypatch.setattr(sys, "argv", ["main.py"])

        with patch("main.load_config"), \
             patch("main.get_crd_versions",
                   side_effect=urllib3.exceptions.MaxRetryError(pool=MagicMock(), url="/apis")):
            exit_code = main.main()

        assert exit_code == 1
        err = capsys.readouterr().err
        assert "could not reach the Kubernetes API server" in err

    def test_api_exception_while_fetching_openshift_resources_is_reported_cleanly(
        self, capsys, monkeypatch,
    ):
        monkeypatch.setattr(sys, "argv", ["main.py", "--openshift"])

        with patch("main.load_config"), \
             patch("main.get_crd_versions", return_value=[]), \
             patch("main.get_openshift_resource_versions",
                   side_effect=ApiException(status=500, reason="Internal Server Error")):
            exit_code = main.main()

        # A failed OpenShift enrichment degrades gracefully instead of aborting
        # the whole report — the caller already has (possibly empty) CRD data.
        assert exit_code == 0
        err = capsys.readouterr().err
        assert "could not fetch OpenShift resources, showing CRDs only" in err

    def test_openshift_failure_still_shows_already_fetched_crds(self, capsys, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["main.py", "--openshift"])
        crd = _crd_info("widgets.example.io", "example.io", "Widget", True, [_version("v1")])

        with patch("main.load_config"), \
             patch("main.get_crd_versions", return_value=[crd]), \
             patch("main.get_openshift_resource_versions",
                   side_effect=ApiException(status=500, reason="Internal Server Error")):
            exit_code = main.main()

        assert exit_code == 0
        out = capsys.readouterr().out
        assert "widgets.example.io" in out

    def test_connection_error_while_fetching_openshift_resources_is_reported_cleanly(
        self, capsys, monkeypatch,
    ):
        monkeypatch.setattr(sys, "argv", ["main.py", "--openshift"])

        with patch("main.load_config"), \
             patch("main.get_crd_versions", return_value=[]), \
             patch("main.get_openshift_resource_versions",
                   side_effect=urllib3.exceptions.MaxRetryError(pool=MagicMock(), url="/apis")):
            exit_code = main.main()

        assert exit_code == 0
        err = capsys.readouterr().err
        assert "could not fetch OpenShift resources, showing CRDs only" in err

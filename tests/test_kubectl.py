from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException

import kubectl


def _ns(name: str, labels: dict[str, str] | None = None) -> SimpleNamespace:
    return SimpleNamespace(metadata=SimpleNamespace(name=name, labels=labels))


def _crd(name: str, group: str, kind: str, plural: str, scope: str,
         versions: list[tuple[str, bool, bool] | tuple[str, bool, bool, bool, str | None]],
         stored_versions: list[str] | None = None,
         conversion_strategy: str | None = None,
         labels: dict[str, str] | None = None,
         annotations: dict[str, str] | None = None) -> SimpleNamespace:
    version_objs = [
        SimpleNamespace(
            name=v[0], served=v[1], storage=v[2],
            deprecated=v[3] if len(v) > 3 else False,
            deprecation_warning=v[4] if len(v) > 4 else None,
        )
        for v in versions
    ]
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels, annotations=annotations),
        spec=SimpleNamespace(
            group=group,
            scope=scope,
            names=SimpleNamespace(kind=kind, plural=plural),
            versions=version_objs,
            conversion=SimpleNamespace(strategy=conversion_strategy)
            if conversion_strategy is not None else None,
        ),
        status=SimpleNamespace(
            stored_versions=stored_versions if stored_versions is not None
            else [v.name for v in version_objs if v.storage],
        ),
    )


class TestLoadConfig:
    def test_prefers_incluster_config(self):
        with patch("kubectl.config.load_incluster_config") as incluster, \
             patch("kubectl.config.load_kube_config") as kube:
            kubectl.load_config()

        incluster.assert_called_once()
        kube.assert_not_called()

    def test_falls_back_to_kube_config_outside_cluster(self):
        with patch("kubectl.config.load_incluster_config",
                    side_effect=kubectl.config.ConfigException), \
             patch("kubectl.config.load_kube_config") as kube:
            kubectl.load_config()

        kube.assert_called_once()

    def test_verify_ssl_true_leaves_default_configuration_untouched(self):
        with patch("kubectl.config.load_incluster_config"), \
             patch("kubectl.client.Configuration.set_default") as set_default:
            kubectl.load_config(verify_ssl=True)

        set_default.assert_not_called()

    def test_verify_ssl_false_disables_certificate_verification(self):
        with patch("kubectl.config.load_incluster_config"), \
             patch("kubectl.client.Configuration.set_default") as set_default:
            kubectl.load_config(verify_ssl=False)

        set_default.assert_called_once()
        applied_config = set_default.call_args[0][0]
        assert applied_config.verify_ssl is False


class TestGetNamespaces:
    def test_returns_namespace_names(self):
        fake_v1 = MagicMock()
        fake_v1.list_namespace.return_value = SimpleNamespace(items=[
            _ns("default", {"team": "platform"}),
            _ns("kube-system", None),
        ])

        with patch("kubectl.client.CoreV1Api", return_value=fake_v1):
            result = kubectl.get_namespaces()

        assert result == ["default", "kube-system"]


class TestCustomList:
    def test_namespaced_calls_list_namespaced_custom_object(self):
        custom = MagicMock()
        kubectl._custom_list(custom, group="g", version="v1", namespace="ns1", plural="things")

        custom.list_namespaced_custom_object.assert_called_once_with(
            group="g", version="v1", namespace="ns1", plural="things",
        )
        custom.list_cluster_custom_object.assert_not_called()

    def test_cluster_scoped_calls_list_cluster_custom_object(self):
        custom = MagicMock()
        kubectl._custom_list(custom, group="g", version="v1", namespace=None, plural="things")

        custom.list_cluster_custom_object.assert_called_once_with(
            group="g", version="v1", plural="things",
        )
        custom.list_namespaced_custom_object.assert_not_called()


class TestGetCrdVersions:
    def test_namespaced_crd_scans_all_namespaces_when_no_namespace_given(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1", True, True)])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        v1 = MagicMock()
        v1.list_namespace.return_value = SimpleNamespace(items=[_ns("ns-a"), _ns("ns-b")])

        custom = MagicMock()

        def list_namespaced(group, version, namespace, plural):
            return {"items": [{}]} if namespace == "ns-a" else {"items": []}

        custom.list_namespaced_custom_object.side_effect = list_namespaced

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom), \
             patch("kubectl.client.CoreV1Api", return_value=v1):
            result = kubectl.get_crd_versions(namespace=None)

        assert len(result) == 1
        [version] = result[0].versions
        assert version.instances_by_namespace == {"ns-a": 1}

    def test_namespace_argument_restricts_scan_to_single_namespace(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1", True, True)])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": [{}, {}]}

        v1 = MagicMock()  # get_namespaces() must not be called in this mode

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom), \
             patch("kubectl.client.CoreV1Api", return_value=v1):
            result = kubectl.get_crd_versions(namespace="ns-a")

        assert result[0].versions[0].instances_by_namespace == {"ns-a": 2}
        v1.list_namespace.assert_not_called()
        custom.list_namespaced_custom_object.assert_called_once_with(
            group="example.io", version="v1", namespace="ns-a", plural="widgets",
        )

    def test_cluster_scoped_crd_included_only_without_namespace_filter(self):
        crd = _crd("clusterwidgets.example.io", "example.io", "ClusterWidget",
                    "clusterwidgets", "Cluster", [("v1", True, True)])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_cluster_custom_object.return_value = {"items": [{}]}

        v1 = MagicMock()
        v1.list_namespace.return_value = SimpleNamespace(items=[])

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom), \
             patch("kubectl.client.CoreV1Api", return_value=v1):
            without_filter = kubectl.get_crd_versions(namespace=None)
            with_filter = kubectl.get_crd_versions(namespace="ns-a")

        assert len(without_filter) == 1
        assert without_filter[0].versions[0].instances_by_namespace == {"(cluster)": 1}
        assert with_filter == []

    def test_unserved_version_is_listed_but_not_queried_for_instances(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1alpha1", False, False), ("v1", True, True)])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": [{}]}

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        [alpha, stable] = result[0].versions
        assert alpha.served is False
        assert alpha.instances_by_namespace == {}
        assert stable.instances_by_namespace == {"ns-a": 1}
        custom.list_namespaced_custom_object.assert_called_once()

    def test_api_exception_is_swallowed_and_leaves_zero_instances(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1", True, True)])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.side_effect = ApiException(status=403)

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        assert result[0].versions[0].instances_by_namespace == {}

    def test_results_sorted_by_group_then_kind(self):
        b = _crd("b.bgroup.io", "bgroup.io", "B", "bs", "Namespaced", [("v1", True, True)])
        a = _crd("a.agroup.io", "agroup.io", "A", "as", "Namespaced", [("v1", True, True)])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[b, a])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        assert [r.group for r in result] == ["agroup.io", "bgroup.io"]

    def test_deprecated_flag_and_warning_are_carried_over_from_the_crd_version(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1alpha1", True, False, True, "use v1 instead"), ("v1", True, True)])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        [alpha, stable] = result[0].versions
        assert alpha.deprecated is True
        assert alpha.deprecation_warning == "use v1 instead"
        assert stable.deprecated is False
        assert stable.deprecation_warning is None

    def test_conversion_strategy_webhook_is_carried_over_from_the_crd_spec(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1alpha1", True, False), ("v1", True, True)],
                    conversion_strategy="Webhook")
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        assert result[0].conversion_strategy == "Webhook"

    def test_conversion_strategy_defaults_to_none_when_spec_has_no_conversion(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1", True, True)])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        assert result[0].conversion_strategy == "None"

    def test_owner_defaults_to_none_when_no_ownership_markers_present(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1", True, True)])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        assert result[0].owner is None

    def test_owner_detected_from_helm_release_annotation(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1", True, True)],
                    annotations={"meta.helm.sh/release-name": "my-release"})
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        assert result[0].owner == "Helm"

    def test_owner_detected_from_generic_managed_by_label_as_fallback(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1", True, True)],
                    labels={"app.kubernetes.io/managed-by": "Terraform"})
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        assert result[0].owner == "Terraform"


class TestOwnerDetection:
    def test_tool_specific_label_takes_precedence_over_generic_managed_by(self):
        owner = kubectl._detect_owner(
            {"olm.owner": "my-csv", "app.kubernetes.io/managed-by": "Helm"}, None,
        )
        assert owner == "OLM"

    def test_no_markers_returns_none(self):
        assert kubectl._detect_owner(None, None) is None
        assert kubectl._detect_owner({}, {}) is None


class TestStorageVersionMigration:
    def test_no_pending_migration_when_stored_versions_match_storage_version(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1alpha1", True, False), ("v1", True, True)],
                    stored_versions=["v1"])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        assert result[0].storage_version == "v1"
        assert result[0].pending_migration_versions == []

    def test_old_stored_version_is_reported_as_pending_migration(self):
        crd = _crd("widgets.example.io", "example.io", "Widget", "widgets", "Namespaced",
                    [("v1alpha1", True, False), ("v1", True, True)],
                    stored_versions=["v1alpha1", "v1"])
        ext = MagicMock()
        ext.list_custom_resource_definition.return_value = SimpleNamespace(items=[crd])

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}

        with patch("kubectl.client.ApiextensionsV1Api", return_value=ext), \
             patch("kubectl.client.CustomObjectsApi", return_value=custom):
            result = kubectl.get_crd_versions(namespace="ns-a")

        assert result[0].pending_migration_versions == ["v1alpha1"]

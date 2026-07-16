from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from kubernetes.client.rest import ApiException

import oc


def _group(name: str, preferred_version: str) -> SimpleNamespace:
    return SimpleNamespace(name=name, preferred_version=SimpleNamespace(version=preferred_version))


def _resource(name: str, kind: str, namespaced: bool) -> dict:
    return {"name": name, "kind": kind, "namespaced": namespaced}


class TestListOpenshiftGroupVersions:
    def test_only_openshift_io_groups_are_returned(self):
        apis_api = MagicMock()
        apis_api.get_api_versions.return_value = SimpleNamespace(groups=[
            _group("route.openshift.io", "v1"),
            _group("apps", "v1"),
        ])

        with patch("oc.client.ApisApi", return_value=apis_api):
            result = oc._list_openshift_group_versions()

        assert result == [("route.openshift.io", "v1")]


class TestDiscoverGroupVersionResources:
    def test_subresources_are_skipped(self):
        api_client = MagicMock()
        api_client.call_api.return_value = (
            {"resources": [
                {"name": "routes", "kind": "Route", "namespaced": True},
                {"name": "routes/status", "kind": "Route", "namespaced": True},
            ]},
            200,
            {},
        )

        result = oc._discover_group_version_resources(api_client, "route.openshift.io", "v1")

        assert [r["name"] for r in result] == ["routes"]

    def test_response_types_map_uses_integer_status_code(self):
        # api_client.call_api looks up response_types_map by the *integer*
        # HTTP status code internally (response_types_map.get(response.status)).
        # A string key like {"200": ...} silently misses and yields a None body.
        api_client = MagicMock()
        api_client.call_api.return_value = ({"resources": []}, 200, {})

        oc._discover_group_version_resources(api_client, "route.openshift.io", "v1")

        kwargs = api_client.call_api.call_args.kwargs
        assert kwargs["response_types_map"] == {200: "object"}

    def test_none_body_is_treated_as_no_resources(self):
        # Mirrors what the real client returns when response_types_map doesn't
        # match the response status: body comes back as None instead of a dict.
        api_client = MagicMock()
        api_client.call_api.return_value = (None, 200, {})

        result = oc._discover_group_version_resources(api_client, "route.openshift.io", "v1")

        assert result == []

    def test_api_exception_returns_empty_list(self):
        api_client = MagicMock()
        api_client.call_api.side_effect = ApiException(status=403)

        result = oc._discover_group_version_resources(api_client, "route.openshift.io", "v1")

        assert result == []

    def test_entries_missing_kind_or_name_are_skipped(self):
        # Real discovery responses always carry both fields, but the resource
        # list is cluster-supplied — a malformed entry must not blow up with
        # a raw KeyError further down the line.
        api_client = MagicMock()
        api_client.call_api.return_value = (
            {"resources": [
                {"name": "routes", "kind": "Route", "namespaced": True},
                {"name": "broken", "namespaced": True},
                {"kind": "AlsoBroken", "namespaced": True},
            ]},
            200,
            {},
        )

        result = oc._discover_group_version_resources(api_client, "route.openshift.io", "v1")

        assert [r["name"] for r in result] == ["routes"]


class TestGetOpenshiftResourceVersions:
    def test_namespaced_resource_instances_are_counted_per_namespace(self):
        apis_api = MagicMock()
        apis_api.get_api_versions.return_value = SimpleNamespace(
            groups=[_group("route.openshift.io", "v1")],
        )

        api_client = MagicMock()
        api_client.call_api.return_value = (
            {"resources": [_resource("routes", "Route", True)]}, 200, {},
        )

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": [{}]}

        v1 = MagicMock()
        v1.list_namespace.return_value = SimpleNamespace(items=[SimpleNamespace(
            metadata=SimpleNamespace(name="ns-a", labels=None))])

        with patch("oc.client.ApisApi", return_value=apis_api), \
             patch("oc.client.ApiClient", return_value=api_client), \
             patch("oc.client.CustomObjectsApi", return_value=custom), \
             patch("kubectl.client.CoreV1Api", return_value=v1):
            result = oc.get_openshift_resource_versions(namespace=None)

        assert len(result) == 1
        info = result[0]
        assert info.name == "routes.route.openshift.io"
        assert info.kind == "Route"
        assert info.namespaced is True
        assert info.versions[0].instances_by_namespace == {"ns-a": 1}

    def test_cluster_scoped_resource_included_only_without_namespace_filter(self):
        apis_api = MagicMock()
        apis_api.get_api_versions.return_value = SimpleNamespace(
            groups=[_group("security.openshift.io", "v1")],
        )

        api_client = MagicMock()
        api_client.call_api.return_value = (
            {"resources": [_resource("securitycontextconstraints", "SecurityContextConstraints",
                                      False)]},
            200, {},
        )

        custom = MagicMock()
        custom.list_cluster_custom_object.return_value = {"items": [{}, {}]}

        v1 = MagicMock()
        v1.list_namespace.return_value = SimpleNamespace(items=[])

        with patch("oc.client.ApisApi", return_value=apis_api), \
             patch("oc.client.ApiClient", return_value=api_client), \
             patch("oc.client.CustomObjectsApi", return_value=custom), \
             patch("kubectl.client.CoreV1Api", return_value=v1):
            without_filter = oc.get_openshift_resource_versions(namespace=None)
            with_filter = oc.get_openshift_resource_versions(namespace="ns-a")

        assert len(without_filter) == 1
        assert without_filter[0].versions[0].instances_by_namespace == {"(cluster)": 2}
        assert with_filter == []

    def test_namespace_argument_restricts_scan_to_single_namespace(self):
        apis_api = MagicMock()
        apis_api.get_api_versions.return_value = SimpleNamespace(
            groups=[_group("route.openshift.io", "v1")],
        )

        api_client = MagicMock()
        api_client.call_api.return_value = (
            {"resources": [_resource("routes", "Route", True)]}, 200, {},
        )

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": [{}, {}]}

        v1 = MagicMock()  # get_namespaces() must not be called in this mode

        with patch("oc.client.ApisApi", return_value=apis_api), \
             patch("oc.client.ApiClient", return_value=api_client), \
             patch("oc.client.CustomObjectsApi", return_value=custom), \
             patch("kubectl.client.CoreV1Api", return_value=v1):
            result = oc.get_openshift_resource_versions(namespace="ns-a")

        assert result[0].versions[0].instances_by_namespace == {"ns-a": 2}
        v1.list_namespace.assert_not_called()
        custom.list_namespaced_custom_object.assert_called_once_with(
            group="route.openshift.io", version="v1", namespace="ns-a", plural="routes",
            _request_timeout=oc._REQUEST_TIMEOUT,
        )

    def test_api_exception_on_cluster_scoped_resource_is_recorded_as_fetch_error(self):
        apis_api = MagicMock()
        apis_api.get_api_versions.return_value = SimpleNamespace(
            groups=[_group("security.openshift.io", "v1")],
        )

        api_client = MagicMock()
        api_client.call_api.return_value = (
            {"resources": [_resource("securitycontextconstraints", "SecurityContextConstraints",
                                      False)]},
            200, {},
        )

        custom = MagicMock()
        custom.list_cluster_custom_object.side_effect = ApiException(status=500, reason="boom")

        v1 = MagicMock()
        v1.list_namespace.return_value = SimpleNamespace(items=[])

        with patch("oc.client.ApisApi", return_value=apis_api), \
             patch("oc.client.ApiClient", return_value=api_client), \
             patch("oc.client.CustomObjectsApi", return_value=custom), \
             patch("kubectl.client.CoreV1Api", return_value=v1):
            result = oc.get_openshift_resource_versions(namespace=None)

        assert result[0].versions[0].instances_by_namespace == {}
        assert "(cluster)" in result[0].versions[0].fetch_errors

    def test_results_sorted_by_group_then_kind(self):
        apis_api = MagicMock()
        apis_api.get_api_versions.return_value = SimpleNamespace(groups=[
            _group("build.openshift.io", "v1"),
            _group("apps.openshift.io", "v1"),
        ])

        api_client = MagicMock()
        api_client.call_api.side_effect = [
            ({"resources": [_resource("builds", "Build", True)]}, 200, {}),
            ({"resources": [_resource("deploymentconfigs", "DeploymentConfig", True)]}, 200, {}),
        ]

        custom = MagicMock()
        custom.list_namespaced_custom_object.return_value = {"items": []}

        with patch("oc.client.ApisApi", return_value=apis_api), \
             patch("oc.client.ApiClient", return_value=api_client), \
             patch("oc.client.CustomObjectsApi", return_value=custom):
            result = oc.get_openshift_resource_versions(namespace="ns-a")

        assert [r.group for r in result] == ["apps.openshift.io", "build.openshift.io"]

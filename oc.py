"""OpenShift-specific data collection — built-in aggregated APIs (Route, BuildConfig,
DeploymentConfig, ...) that live in *.openshift.io API groups rather than as
CustomResourceDefinitions, so they never show up in list_custom_resource_definition()."""
from __future__ import annotations

import logging
from typing import Any, cast

import urllib3
from kubernetes import client
from kubernetes.client import V1APIGroupList
from kubernetes.client.exceptions import ApiException

from kubectl import (
    _REQUEST_TIMEOUT,
    CRDVersionedInfo,
    CRDVersionInfo,
    GroupVersionResource,
    _count_cluster_scoped_instances,
    _count_instances_by_namespace,
    get_namespaces,
)

logger = logging.getLogger(__name__)

_OPENSHIFT_GROUP_SUFFIX = ".openshift.io"


def _list_openshift_group_versions() -> list[tuple[str, str]]:
    """Return (group, preferred_version) for every installed *.openshift.io API group."""
    apis_api = client.ApisApi()
    group_list = cast(
        V1APIGroupList, apis_api.get_api_versions(_request_timeout=_REQUEST_TIMEOUT),
    )
    return [
        (group.name, group.preferred_version.version)
        for group in (group_list.groups or [])
        if group.name.endswith(_OPENSHIFT_GROUP_SUFFIX) and group.preferred_version is not None
    ]


def _discover_group_version_resources(api_client: client.ApiClient, group: str,
                                       version: str) -> list[dict[str, Any]]:
    """Raw discovery call for /apis/{group}/{version} — the resource list (Kind, plural
    name, namespaced) isn't modeled by the generated client since it's cluster-specific."""
    try:
        request = api_client.param_serialize(
            "GET", f"/apis/{group}/{version}", auth_settings=["BearerToken"],
        )
        response = api_client.call_api(*request, _request_timeout=_REQUEST_TIMEOUT)
        response.read()
        # response_deserialize raises ApiException for non-2xx statuses.
        body = cast(
            "dict[str, Any] | None",
            api_client.response_deserialize(response, {"200": "object"}).data,
        )
    except (ApiException, urllib3.exceptions.HTTPError) as e:
        logger.debug("Discovery failed for %s/%s: %s", group, version, e)
        return []
    if body is None:
        return []
    # Skip subresources such as "routes/status", and any entry missing the
    # fields we rely on downstream (name, kind are required by the
    # APIResourceList schema but discovery responses are cluster-supplied).
    return [
        r for r in body.get("resources", [])
        if r.get("name") and "/" not in r["name"] and r.get("kind")
    ]


def get_openshift_resource_versions(namespace: str | None = None) -> list[CRDVersionedInfo]:
    """Like kubectl.get_crd_versions, but for built-in OpenShift API resources."""
    custom = client.CustomObjectsApi()
    api_client = client.ApiClient()

    namespaces_to_scan = [namespace] if namespace is not None else get_namespaces()

    result: list[CRDVersionedInfo] = []

    for group, version in _list_openshift_group_versions():
        for res in _discover_group_version_resources(api_client, group, version):
            is_namespaced = bool(res.get("namespaced", False))

            if not is_namespaced and namespace is not None:
                continue

            plural = res["name"]
            info = CRDVersionedInfo(
                name=f"{plural}.{group}",
                group=group,
                kind=res["kind"],
                plural=plural,
                namespaced=is_namespaced,
            )
            vinfo = CRDVersionInfo(version=version, served=True, storage=True)

            gvr = GroupVersionResource(group=group, version=version, plural=plural)
            if is_namespaced:
                vinfo.instances_by_namespace, vinfo.fetch_errors = _count_instances_by_namespace(
                    custom, gvr, namespaces=namespaces_to_scan,
                )
            else:
                vinfo.instances_by_namespace, vinfo.fetch_errors = _count_cluster_scoped_instances(
                    custom, gvr,
                )

            info.versions.append(vinfo)
            result.append(info)

    return sorted(result, key=lambda i: (i.group, i.kind))
